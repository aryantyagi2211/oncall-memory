import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from hindsight_client import Hindsight

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["HINDSIGHT_TIMEOUT"] = "90"
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


def save_csv(results):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with (RESULTS_DIR / "per_incident.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["test_id", "mode", "retrieved_ids", "root_cause_correct", "fix_score", "judge_reason", "error"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in results.get("runs", []):
            writer.writerow({
                "test_id": row.get("test_id", ""),
                "mode": row.get("mode", ""),
                "retrieved_ids": ",".join(row.get("retrieved_ids", [])),
                "root_cause_correct": row.get("root_cause_correct"),
                "fix_score": row.get("fix_score"),
                "judge_reason": row.get("judge_reason", ""),
                "error": "; ".join(row.get("errors", []) + row.get("judge_errors", [])),
            })


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
        response = None
        options = {
            "model": "openai/gpt-oss-20b",
            "temperature": 0,
            "max_completion_tokens": 256,
            "messages": [
                {"role": "system", "content": prompt + "\nReturn exactly: VERDICT: YES|NO (or 1-5) / REASON: <one short sentence>"},
                {"role": "user", "content": label},
            ],
        }
        try:
            try:
                response = client.chat.completions.create(**options, reasoning_effort="low")
            except Exception as exc:
                if "reasoning_effort" not in str(exc).lower():
                    raise
                response = client.chat.completions.create(**options)
            if not response.choices:
                raise ValueError("Groq judge returned no choices")
            message = response.choices[0].message
            content = (getattr(message, "content", None) or "").strip()
            reasoning = (getattr(message, "reasoning", None) or "").strip()
            text = content or reasoning
            if not text:
                logger.error("Groq judge returned empty content and reasoning; raw response=%r", response)
            return {"text": text, "raw_response": repr(response)}
        except Exception as exc:
            raw_response = response if response is not None else getattr(exc, "response", None)
            logger.exception("Groq judge request failed; raw response=%r", raw_response)
            raise

    time.sleep(1.5)
    return retry_call(request, "Groq judge")


ROOT_CAUSE_RUBRIC = "Does the answer's stated root cause match the correct root cause in meaning? Verdict must be YES or NO and include one short reason."
FIX_RUBRIC = "Score the answer's proposed fix from 1 to 5 against the correct fix. 1=unrelated or harmful, 3=partially correct, 5=meaningfully equivalent and actionable. Give one integer score and one short reason."


def score_answer(client, answer, test_case):
    import re
    root_match, fix_score = None, None
    reasons, errors = [], []
    try:
        root_result = judge(client, ROOT_CAUSE_RUBRIC + " If no root cause is stated, verdict NO.",
                            f"ANSWER:\n{answer}\n\nCORRECT ROOT CAUSE:\n{test_case['correct_root_cause']}")
        verdict = re.search(r"\b(YES|NO)\b", root_result["text"], re.I)
        if not verdict:
            logger.error("Could not parse root-cause verdict; raw response=%s", root_result["raw_response"])
            raise ValueError("Could not parse YES/NO from judge response")
        root_match = verdict.group(1).upper() == "YES"
        reason_match = re.search(r"REASON:\s*(.+)", root_result["text"], re.I)
        reasons.append("Root cause: " + (reason_match.group(1).strip() if reason_match else root_result["text"].strip()))
    except Exception as exc:
        errors.append(f"root_cause_correct: {exc}")
    try:
        fix_result = judge(client, FIX_RUBRIC,
                           f"ANSWER:\n{answer}\n\nCORRECT FIX:\n{test_case['correct_fix']}")
        numeric = re.search(r"(?:VERDICT:\s*)?([1-5])\b", fix_result["text"], re.I)
        if not numeric:
            logger.error("Could not parse fix score; raw response=%s", fix_result["raw_response"])
            raise ValueError("Could not parse 1-5 fix score from judge response")
        fix_score = int(numeric.group(1))
        reason_match = re.search(r"REASON:\s*(.+)", fix_result["text"], re.I)
        reasons.append("Fix: " + (reason_match.group(1).strip() if reason_match else fix_result["text"].strip()))
    except Exception as exc:
        errors.append(f"fix_score: {exc}")
    return root_match, fix_score, " | ".join(reasons), errors


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
        check = retry_call(lambda: client.recall(bank_id=bank, query=f"Incident ID: {incidents[0]['id']}"), f"verify {bank}")
        extracted = [core.recalled_memory(item) for item in core.memory_field(check, "results", [])]
        verified = next((item for item in extracted if item.get("id") == incidents[0]["id"]), None)
        if not verified or not verified.get("root_cause") or not verified.get("fix"):
            raise RuntimeError(f"Seed verification failed for {bank}: {incidents[0]['id']} did not recall with extracted root_cause and fix.")
    logger.info("Eval banks seeded; N=0 is intentionally empty and N=40 uses %s.", BANK_ID)


def memory_run(test_case, bank_id):
    start = time.perf_counter()
    result = retry_call(
        lambda: core.diagnose_with_memory(test_case["alert_log"], bank_id=bank_id),
        f"memory diagnosis {test_case['id']} bank={bank_id}",
    )
    error = str(result.get("error") or "")
    if bank_id.endswith("-0") and ("not found" in error.lower() or not result.get("recalled_incidents")):
        result["error"] = ""
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
        if errors or "LLM unavailable" in answer or "Groq request exceeded" in answer:
            return {"test_id": test_case["id"], "memory": memory_enabled, "bank_id": bank_id,
                    "mode": "ON" if memory_enabled else "OFF", "answer": answer,
                    "retrieved_ids": output.get("recalled_incidents", []) if memory_enabled else [],
                    "retrieval_hit": hit, "root_cause_correct": None, "fix_score": None,
                    "judge_reason": "Not scored because the diagnosis had an upstream error.",
                    "errored": True, "latency_seconds": round(time.perf_counter()-started, 3), "errors": errors or ["diagnosis returned an LLM-unavailable fallback"]}
        root_ok, fix_score, judge_reason, judge_errors = score_answer(client, answer, test_case)
        return {
            "test_id": test_case["id"],
            "memory": memory_enabled,
            "mode": "ON" if memory_enabled else "OFF",
            "bank_id": bank_id,
            "answer": answer,
            "retrieved_ids": output.get("recalled_incidents", []) if memory_enabled else [],
            "retrieval_hit": hit,
            "root_cause_correct": root_ok,
            "fix_score": fix_score,
            "judge_reason": judge_reason,
            "judge_errors": judge_errors,
            "errored": False,
            "latency_seconds": round(time.perf_counter() - started, 3),
            "errors": errors,
        }
    except Exception as exc:
        logger.exception("Evaluation failed for %s (memory=%s)", test_case["id"], memory_enabled)
        return {
            "test_id": test_case["id"], "memory": memory_enabled,
            "mode": "ON" if memory_enabled else "OFF", "bank_id": bank_id, "answer": "", "retrieved_ids": [], "retrieval_hit": None,
            "root_cause_correct": None, "fix_score": None,
            "judge_reason": "Not scored because the run errored.", "errored": True,
            "latency_seconds": round(time.perf_counter() - started, 3), "errors": errors + [str(exc)],
        }


def chart_results(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def mean(rows, key):
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float, bool))]
        return sum(vals) / len(vals) if vals else 0

    off = [r for r in results["runs"] if r["memory"] is False and not r.get("errored")]
    on = [r for r in results["runs"] if r["memory"] is True and not r.get("errored")]
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
    ax.set_title(f"ON vs OFF (n={len(off)} OFF, n={len(on)} ON) — synthetic data, 10 held-out incidents")
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
    ax.set_title("Learning curve — synthetic data, 10 held-out incidents")
    ax.legend()
    fig.tight_layout()
    fig.savefig(RESULTS_DIR / "learning_curve.png", dpi=150)
    plt.close(fig)


def print_summary(results):
    print("Run                    Root cause     Fix score      Retrieval      Errors")
    for label, rows in (("Memory OFF", [r for r in results["runs"] if not r["memory"]]),
                        ("Memory ON", [r for r in results["runs"] if r["memory"]])):
        if rows:
            valid = [r for r in rows if not r.get("errored")]
            root_rows = [r for r in valid if r.get("root_cause_correct") is not None]
            fix_rows = [r for r in valid if r.get("fix_score") is not None]
            retrieval_rows = [r for r in valid if r.get("retrieval_hit") is not None]
            print(f"{label:<22} {sum(bool(r['root_cause_correct']) for r in root_rows)}/{len(root_rows)}          "
                  f"{sum(r['fix_score'] or 0 for r in fix_rows)}/{5*len(fix_rows)}         "
                  f"{sum(bool(r['retrieval_hit']) for r in retrieval_rows)}/{len(retrieval_rows)}          "
                  f"{sum(r.get('errored', False) for r in rows)}/{len(rows)}")
    for row in results["learning_curve"]:
        print(f"Learning curve N={row['N']:<2}  root={row.get('root_cause_correct',0):.2f} fix={row.get('fix_score',0):.2f} retrieval={row.get('retrieval_hit',0):.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Evaluate only the first N held-out incidents")
    parser.add_argument("--seed", action="store_true", help="Create/fill oncall-eval-10 and oncall-eval-20")
    parser.add_argument("--full", action="store_true", help="Also evaluate N=10 and N=20 (requires --seed)")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent held-out incidents (default 2)")
    parser.add_argument("--rejudge", action="store_true", help="Rejudge saved OFF/ON answers without rerunning diagnoses")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.full and not args.seed:
        parser.error("--full requires --seed")
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
    if args.rejudge:
        if not RESULTS_PATH.exists():
            raise SystemExit(f"No saved evaluation results at {RESULTS_PATH}")
        results = load_data(RESULTS_PATH)
        test_by_id = {case["id"]: case for case in load_data(TESTS_PATH)}
        for row in results.get("runs", []):
            case = test_by_id.get(row.get("test_id"))
            if not case or not row.get("answer"):
                continue
            root_ok, fix_score, reason, judge_errors = score_answer(client, row["answer"], case)
            row.update({"root_cause_correct": root_ok, "fix_score": fix_score,
                        "judge_reason": reason, "judge_errors": judge_errors})
            if row.get("memory"):
                recalled = set(row.get("retrieved_ids", []))
                relevant = set(case["relevant_incident_ids"])
                row["retrieval_hit"] = bool(recalled & relevant) if relevant else "no closely matching past incident" in row["answer"].lower()
        save_results(results)
        save_csv(results)
        chart_results(results)
        print_summary(results)
        return
    results = {"runs": [], "learning_curve": [], "errors": []}
    jobs = [(case, mode, bank) for case in tests for mode, bank in ((False, None), (True, BANK_ID))]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(eval_one, client, case, mode, bank) for case, mode, bank in jobs]
        for future in as_completed(futures):
            row = future.result()
            results["runs"].append(row)
            if row["errored"]:
                results["errors"].append({"test_id": row["test_id"], "mode": row["mode"], "error": row["errors"]})
            save_results(results)
            save_csv(results)
    counts = (0, 10, 20, 40) if args.full else (0, 40)
    for count in counts:
        bank_id = BANK_ID if count == 40 else f"oncall-eval-{count}"
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(lambda case: eval_one(client, case, True, bank_id), tests))
        valid_rows = [r for r in rows if not r.get("errored")]
        results["learning_curve"].append({
            "N": count,
            "root_cause_correct": sum(bool(r["root_cause_correct"]) for r in valid_rows if r["root_cause_correct"] is not None) / max(1, sum(r["root_cause_correct"] is not None for r in valid_rows)),
            "root_cause_count": f"{sum(bool(r['root_cause_correct']) for r in valid_rows if r['root_cause_correct'] is not None)}/{sum(r['root_cause_correct'] is not None for r in valid_rows)}",
            "fix_score": sum(r["fix_score"] or 0 for r in valid_rows) / max(1, sum(r["fix_score"] is not None for r in valid_rows)) / 5,
            "retrieval_hit": sum(bool(r["retrieval_hit"]) for r in valid_rows if r["retrieval_hit"] is not None) / max(1, sum(r["retrieval_hit"] is not None for r in valid_rows)),
            "errored_runs": sum(r["errored"] for r in rows),
            "latency_seconds": sum(r["latency_seconds"] for r in rows) / max(1, len(rows)),
            "errors": [error for r in rows for error in r["errors"]],
        })
        save_results(results)
        save_csv(results)
        time.sleep(1.5)
    chart_results(results)
    save_results(results)
    save_csv(results)
    print_summary(results)


if __name__ == "__main__":
    main()
