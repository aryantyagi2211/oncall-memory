"""Demonstrate that retained failed-fix feedback changes the next diagnosis."""
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from hindsight_client import Hindsight

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import core
import oncall_memory.diagnose as diagnose_module
from oncall_memory.validation import memory_answer_validation_reasons
from seed import build_incident_document

BANK_ID = "oncall-feedback-demo"
OUTPUT_PATH = ROOT / "eval" / "results" / "feedback_proof.json"
FEEDBACK_POLL_INTERVAL_SECONDS = 3
FEEDBACK_POLL_TIMEOUT_SECONDS = 120


def selected_memory(result: dict) -> dict | None:
    """Return the recalled incident cited by a diagnosis, if one exists."""
    match = re.search(r"(?im)^Matched past incident:\s*(INC-\d{3,})\b", result.get("answer", ""))
    return next((memory for memory in result.get("memories", []) if match and memory.get("id") == match.group(1)), None)


def verify_seeded_incident(client: Hindsight, incident: dict) -> None:
    """Confirm a seeded incident recalls with its root cause and fix extracted."""
    response = client.recall(
        bank_id=BANK_ID,
        query=f"Incident ID: {incident['id']}",
        max_tokens=8192,
    )
    fragments = [
        core.recalled_memory(item, log_missing=False)
        for item in core.memory_field(response, "results", [])
        if core.memory_text(item)
    ]
    parsed = core.deduplicate_incidents(fragments, limit=len(fragments))
    recalled = next((item for item in parsed if item.get("id") == incident["id"]), None)
    if (not recalled or recalled.get("root_cause") != incident["root_cause"]
            or recalled.get("fix") != incident["resolution_steps"]):
        raise SystemExit(
            f"Seed verification failed for {incident['id']}: Hindsight recall did not yield "
            "the expected root_cause and fix fields. Check the bank and indexing status."
        )
    print(f"Seed verification passed for {incident['id']} (root cause and fix extracted).", flush=True)


def wait_for_feedback(client: Hindsight, feedback_id: str, incident_id: str, fix: str) -> tuple[float, dict | None]:
    """Poll Hindsight until the complete failed-fix feedback record is recalled."""
    started = time.monotonic()
    normalized_fix = " ".join(fix.lower().split())
    while True:
        response = client.recall(
            bank_id=BANK_ID,
            query=f"Feedback ID: {feedback_id}\nCited incident: {incident_id}\nSuggested fix: {fix}",
            max_tokens=8192,
        )
        records = [
            parsed for item in core.memory_field(response, "results", [])
            if (parsed := core.parse_feedback(item))
        ]
        recalled = next((
            record for record in records
            if record.get("outcome") == "did not work"
            and record.get("incident_id") == incident_id
            and " ".join(record.get("fix", "").lower().split()) == normalized_fix
            and (not record.get("feedback_id") or record["feedback_id"] == feedback_id)
        ), None)
        waited = time.monotonic() - started
        if recalled:
            print(f"Feedback record recalled after {waited:.1f}s.", flush=True)
            return waited, recalled
        if waited >= FEEDBACK_POLL_TIMEOUT_SECONDS:
            print(f"Feedback record was not recalled after {waited:.1f}s.", flush=True)
            return waited, None
        time.sleep(min(FEEDBACK_POLL_INTERVAL_SECONDS, FEEDBACK_POLL_TIMEOUT_SECONDS - waited))


def diagnose_with_diagnostics(alert: str) -> tuple[dict, dict]:
    """Run normal diagnosis while capturing candidate and validation decisions."""
    captured: dict = {}
    original_validator = diagnose_module.validate_memory_answer
    original_ranker = diagnose_module.rank_feedback_candidates

    def capture_validator(answer, memories, incident_text="", feedback=None):
        reasons = memory_answer_validation_reasons(answer, memories, incident_text, feedback)
        captured["validator"] = {
            "accepted_llm_answer": not reasons,
            "used_template": bool(reasons),
            "rejection_reasons": reasons,
        }
        return original_validator(answer, memories, incident_text, feedback)

    def capture_ranker(memories, feedback):
        ranked = original_ranker(memories, feedback)
        captured["selection_candidate_ids_in_order"] = [item.get("id") for item in ranked]
        return ranked

    diagnose_module.validate_memory_answer = capture_validator
    diagnose_module.rank_feedback_candidates = capture_ranker
    try:
        result = core.diagnose_with_memory(alert, bank_id=BANK_ID)
    finally:
        diagnose_module.validate_memory_answer = original_validator
        diagnose_module.rank_feedback_candidates = original_ranker
    captured.setdefault("validator", {
        "accepted_llm_answer": None,
        "used_template": None,
        "rejection_reasons": ["validator was not reached"],
    })
    captured.setdefault("selection_candidate_ids_in_order", [])
    return result, captured


def feedback_diagnostics(result: dict, captured: dict) -> dict:
    """Summarize recalled feedback, candidate order, and source validation."""
    memories = result.get("memories", [])
    raw_feedback = [
        record for record in result.get("feedback", [])
        if record.get("outcome") in {"worked", "did not work"}
    ]
    # Hindsight may return repeated fragments for one retained feedback document.
    unique_feedback = {}
    for index, record in enumerate(raw_feedback):
        key = record.get("feedback_id") or (
            record.get("outcome"), record.get("incident_id"), record.get("fix"),
            record.get("date"), record.get("text", str(index)),
        )
        unique_feedback[key] = record
    associated = core.associate_feedback(memories, list(unique_feedback.values()))
    failed_counts = Counter()
    worked_counts = Counter()
    for record in associated:
        counts = failed_counts if record.get("outcome") == "did not work" else worked_counts
        for incident_id in record.get("matched_incident_ids", []):
            counts[incident_id] += 1
    failed_ids = sorted(failed_counts)
    selectable_ids = set(captured.get("selection_candidate_ids_in_order", []))
    return {
        "recalled_feedback_records_count": len(unique_feedback),
        "failed_incident_ids_detected": failed_ids,
        "selection_candidate_ids_in_order": captured.get("selection_candidate_ids_in_order", []),
        "candidate_list_in_order": [
            {
                "incident_id": memory.get("id"),
                "failed_count": failed_counts[memory.get("id")],
                "worked_count": worked_counts[memory.get("id")],
                "eligible_for_best_match": memory.get("id") in selectable_ids,
            }
            for memory in memories
        ],
        "validator": captured["validator"],
    }


def save_report(report: dict) -> None:
    """Persist and print the feedback proof report."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


def main() -> None:
    """Seed the demo bank, verify recall, record failed feedback, and compare diagnoses."""
    hindsight_url = os.getenv("HINDSIGHT_URL", "").strip()
    api_key = os.getenv("HINDSIGHT_API_KEY", "").strip()
    if not hindsight_url:
        raise SystemExit("Set HINDSIGHT_URL before running the feedback proof.")
    client = Hindsight(base_url=hindsight_url, api_key=api_key) if api_key else Hindsight(base_url=hindsight_url)
    with (ROOT / "data" / "incidents.json").open(encoding="utf-8") as handle:
        incidents = json.load(handle)
    for incident in incidents:
        content, metadata = build_incident_document(incident)
        client.retain(bank_id=BANK_ID, content=content,
                      context="resolved production incident postmortem",
                      timestamp=incident["timestamp"], document_id=incident["id"], metadata=metadata)
    if not incidents:
        raise SystemExit("Seed verification failed: data/incidents.json contains no incidents.")
    verify_seeded_incident(client, incidents[0])

    with (ROOT / "data" / "test_incidents.json").open(encoding="utf-8") as handle:
        alert = next(item["alert_log"] for item in json.load(handle) if item["id"] == "TEST-02")
    before_result, before_capture = diagnose_with_diagnostics(alert)
    before_memory = selected_memory(before_result)
    if not before_memory or not before_memory.get("fix"):
        raise RuntimeError("Initial diagnosis did not cite a recalled incident with a fix; proof cannot continue.")

    now = datetime.now(timezone.utc)
    timestamp = now.isoformat().replace("+00:00", "Z")
    feedback_id = f"feedback-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    note = "Feedback proof demonstration."
    feedback = (
        f"Feedback ID: {feedback_id}\nNew alert summary: {alert}\n"
        f"Suggested fix: {before_memory['fix']}\nCited incident: {before_memory['id']}\n"
        "Outcome: did not work\nNote: Feedback proof demonstration."
    )
    client.retain(
        bank_id=BANK_ID, content=feedback,
        context="feedback on incident-response recommendation",
        timestamp=timestamp, document_id=feedback_id,
        metadata={"record_type": "feedback", "feedback_id": feedback_id,
                  "outcome": "did not work", "incident_id": before_memory["id"],
                  "suggested_fix": before_memory["fix"], "note": note, "timestamp": timestamp},
    )

    waited, recalled_feedback = wait_for_feedback(client, feedback_id, before_memory["id"], before_memory["fix"])
    if not recalled_feedback:
        report = {
            "bank_id": BANK_ID,
            "alert": alert,
            "before": {"best_match_id": before_memory["id"], "fix": before_memory["fix"], "diagnostics": before_capture},
            "feedback_document_id": feedback_id,
            "feedback_poll": {"recalled": False, "seconds_waited": round(waited, 1)},
            "error": "Feedback did not become recallable before the 120 second timeout; second diagnosis was skipped.",
        }
        save_report(report)
        raise SystemExit(report["error"])

    after_result, after_capture = diagnose_with_diagnostics(alert)
    after_memory = selected_memory(after_result)
    report = {
        "bank_id": BANK_ID,
        "alert": alert,
        "before": {
            "best_match_id": before_memory["id"], "fix": before_memory["fix"],
            "diagnostics": before_capture,
        },
        "feedback_document_id": feedback_id,
        "feedback_poll": {"recalled": True, "seconds_waited": round(waited, 1)},
        "after": {
            "best_match_id": after_memory["id"] if after_memory else None,
            "fix": after_memory["fix"] if after_memory else None,
            "diagnostics": feedback_diagnostics(after_result, after_capture),
        },
        "failed_fix_avoided": not after_memory or after_memory["id"] != before_memory["id"],
    }
    save_report(report)


if __name__ == "__main__":
    main()
