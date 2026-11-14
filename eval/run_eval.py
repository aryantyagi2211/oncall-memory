import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from hindsight_client import Hindsight

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import core
from seed import BANK_ID, HINDSIGHT_API_KEY, HINDSIGHT_URL, build_incident_document

load_dotenv(ROOT / ".env")
RESULTS_DIR = ROOT / "eval" / "results"
RESULTS_PATH = RESULTS_DIR / "results.json"
INCIDENTS_PATH = ROOT / "data" / "incidents.json"
TESTS_PATH = ROOT / "data" / "test_incidents.json"
logger = logging.getLogger("eval")


def load_data(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_results(results):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = RESULTS_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    temp_path.replace(RESULTS_PATH)


def retry_call(fn, label, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                delay = min(2 ** attempt, 4) + random.random()
                logger.warning("%s failed (%s); retrying in %.1fs", label, exc, delay)
                time.sleep(delay)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last_error}")


def judge(client, prompt, label):
    def request():
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            temperature=0,
            max_completion_tokens=16,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": label},
            ],
        )
        return response.choices[0].message.content.strip()

    time.sleep(1.5)
    return retry_call(request, "Groq judge")


ROOT_CAUSE_RUBRIC = "Does the answer's stated root cause match the correct root cause in meaning? Reply only YES or NO."
FIX_RUBRIC = "Score the answer's proposed fix from 1 to 5 against the correct fix. 1=unrelated or harmful, 3=partially correct, 5=meaningfully equivalent and actionable. Reply only with one integer 1-5."


def score_answer(client, answer, test_case):
    root_prompt = ROOT_CAUSE_RUBRIC + " If the answer does not state a root cause, reply NO."
    root_result = judge(
        client,
        root_prompt,
        f"ANSWER:\n{answer}\n\nCORRECT ROOT CAUSE:\n{test_case['correct_root_cause']}",
    )
    fix_result = judge(
        client,
        FIX_RUBRIC,
        f"ANSWER:\n{answer}\n\nCORRECT FIX:\n{test_case['correct_fix']}",
    )
    root_match = root_result.strip().upper().startswith("YES")
    import re
    numeric = re.search(r"\b([1-5])\b", fix_result)
    return root_match, int(numeric.group(1)) if numeric else None


def build_hindsight():
    if not HINDSIGHT_URL:
        raise RuntimeError("Set HINDSIGHT_URL in .env to run memory-enabled evaluations.")
    if HINDSIGHT_API_KEY:
        return Hindsight(base_url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY)
    return Hindsight(base_url=HINDSIGHT_URL)


def seed_eval_banks(incidents):
    client = build_hindsight()
    for count in (10, 20):
        bank = f"oncall-eval-{count}"
        for incident in incidents[:count]:
            content, metadata = build_incident_document(incident)
            retry_call(
                lambda i=incident, c=content, m=metadata, b=bank: client.retain(
                    bank_id=b,
                    content=c,
                    context="resolved production incident postmortem",
                    timestamp=i["timestamp"],
                    document_id=i["id"],
                    metadata=m,
                ),
                f"retain {incident['id']} in {bank}",
            )
            time.sleep(1.5)
    logger.info("Eval banks seeded; N=0 is intentionally empty and N=40 uses %s.", BANK_ID)


def memory_run(test_case, bank_id):
    start = time.perf_counter()
    result = retry_call(
        lambda: core.diagnose_with_memory(test_case["alert_log"], bank_id=bank_id),
        f"memory diagnosis {test_case['id']} bank={bank_id}",
    )
    return result, time.perf_counter() - start


def retrieval_hit(result, test_case):
    relevant = set(test_case["relevant_incident_ids"])
    recalled = set(result.get("recalled_incidents", []))
    if relevant:
        return bool(relevant & recalled)
    return "no closely matching past incident" in result.get("answer", "").lower()


def eval_one(client, test_case, memory_enabled, bank_id=None):
    started = time.perf_counter()
    errors = []
    try:
        if memory_enabled:
            output, _ = memory_run(test_case, bank_id)
            answer = output.get("answer", "")
            hit = retrieval_hit(output, test_case)
            if output.get("error"):
                errors.append(output["error"])
        else:
            answer = retry_call(
                lambda: core.diagnose_without_memory(test_case["alert_log"]),
                f"baseline diagnosis {test_case['id']}",
            )
            hit = None
        time.sleep(1.5)
        root_ok, fix_score = score_answer(client, answer, test_case)
        return {
            "test_id": test_case["id"],
            "memory": memory_enabled,
            "bank_id": bank_id,
            "answer": answer,
            "retrieval_hit": hit,
            "root_cause_correct": root_ok,
            "fix_score": fix_score,
            "latency_seconds": round(time.perf_counter() - started, 3),
            "errors": errors,
        }
    except Exception as exc:
        logger.exception("Evaluation failed for %s (memory=%s)", test_case["id"], memory_enabled)
        return {
            "test_id": test_case["id"], "memory": memory_enabled,
            "bank_id": bank_id, "answer": "", "retrieval_hit": False if memory_enabled else None,
            "root_cause_correct": None, "fix_score": None,
            "latency_seconds": round(time.perf_counter() - started, 3), "errors": errors + [str(exc)],
        }


def chart_results(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def mean(rows, key):
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float, bool))]
        return sum(vals) / len(vals) if vals else 0

    off = [r for r in results["runs"] if r["memory"] is False]
    on = [r for r in results["runs"] if r["memory"] is True]
    labels = ["Root cause correct", "Fix score", "Retrieval hit"]
    off_values = [mean(off, "root_cause_correct"), mean(off, "fix_score") / 5, 0]
    on_values = [mean(on, "root_cause_correct"), mean(on, "fix_score") / 5, mean(on, "retrieval_hit")]
    x = range(len(labels))
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar([i - .2 for i in x], off_values, width=.4, label="Memory OFF")
    ax.bar([i + .2 for i in x], on_values, width=.4, label="Memory ON")
    ax.set_xticks(list(x), labels)
    ax.set_ylim(0, 1)
    ax.set_ylabel("Normalized score")
    ax.legend()
    fig.tight_layout()
    fig.savefig(RESULTS_DIR / "on_vs_off.png", dpi=150)
    plt.close(fig)

    curve = results["learning_curve"]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for key, label in (("root_cause_correct", "Root cause correct"), ("fix_score", "Fix score / 5"), ("retrieval_hit", "Retrieval hit")):
        ax.plot([row["N"] for row in curve], [row.get(key, 0) for row in curve], marker="o", label=label)
    ax.set_xticks([0, 10, 20, 40])
    ax.set_ylim(0, 1)
    ax.set_xlabel("Seeded incidents (N)")
    ax.set_ylabel("Normalized score")
    ax.legend()
    fig.tight_layout()
    fig.savefig(RESULTS_DIR / "learning_curve.png", dpi=150)
    plt.close(fig)


def print_summary(results):
    print("Run                    N   Root-cause   Fix/5   Retrieval   Latency(s)")
    for label, rows in (("Memory OFF", [r for r in results["runs"] if not r["memory"]]),
                        ("Memory ON", [r for r in results["runs"] if r["memory"]])):
        if rows:
            valid = [r for r in rows if r["root_cause_correct"] is not None]
            print(f"{label:<22} -   {sum(r['root_cause_correct'] for r in valid)/len(valid) if valid else 0:.2f}         "
                  f"{sum(r['fix_score'] for r in rows if r['fix_score'] is not None)/max(1,sum(r['fix_score'] is not None for r in rows)):.2f}    "
                  f"{sum(bool(r['retrieval_hit']) for r in rows if r['retrieval_hit'] is not None)/max(1,sum(r['retrieval_hit'] is not None for r in rows)):.2f}        "
                  f"{sum(r['latency_seconds'] for r in rows)/len(rows):.2f}")
    for row in results["learning_curve"]:
        print(f"Learning curve N={row['N']:<2}  root={row.get('root_cause_correct',0):.2f} fix={row.get('fix_score',0):.2f} retrieval={row.get('retrieval_hit',0):.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Evaluate only the first N held-out incidents")
    parser.add_argument("--seed", action="store_true", help="Create/fill oncall-eval-10 and oncall-eval-20")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    incidents = load_data(INCIDENTS_PATH)
    tests = load_data(TESTS_PATH)
    if args.limit is not None:
        if args.limit < 0:
            parser.error("--limit must be non-negative")
        tests = tests[:args.limit]
    if args.seed:
        seed_eval_banks(incidents)
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key:
        raise SystemExit("Set GROQ_API_KEY in .env to run LLM-judged evaluations.")
    client = Groq(api_key=groq_key)
    results = {"runs": [], "learning_curve": [], "errors": []}
    for test_case in tests:
        for enabled, bank in ((False, None), (True, BANK_ID)):
            row = eval_one(client, test_case, enabled, bank)
            results["runs"].append(row)
            if row["errors"]:
                results["errors"].append({"test_id": test_case["id"], "mode": "memory" if enabled else "baseline", "errors": row["errors"]})
            save_results(results)
            time.sleep(1.5)
    for count in (0, 10, 20, 40):
        bank_id = BANK_ID if count == 40 else f"oncall-eval-{count}"
        rows = [eval_one(client, test_case, True, bank_id) for test_case in tests]
        results["learning_curve"].append({
            "N": count,
            "root_cause_correct": sum(bool(r["root_cause_correct"]) for r in rows if r["root_cause_correct"] is not None) / max(1, sum(r["root_cause_correct"] is not None for r in rows)),
            "fix_score": sum(r["fix_score"] or 0 for r in rows) / max(1, sum(r["fix_score"] is not None for r in rows)) / 5,
            "retrieval_hit": sum(bool(r["retrieval_hit"]) for r in rows) / max(1, len(rows)),
            "latency_seconds": sum(r["latency_seconds"] for r in rows) / max(1, len(rows)),
            "errors": [error for r in rows for error in r["errors"]],
        })
        save_results(results)
        time.sleep(1.5)
    chart_results(results)
    save_results(results)
    print_summary(results)


if __name__ == "__main__":
    main()
