"""Show that a confirmed supersession record demotes an outdated incident."""
import argparse
import json
import os
import re
import sys
import time
import uuid
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

OUTPUT_PATH = ROOT / "eval" / "results" / "supersession_proof.json"
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 120
PROOF_CASE_ID = "TEST-02"


def selected_memory(result: dict) -> dict | None:
    """Return the recalled incident cited as the diagnosis best match."""
    match = re.search(
        r"(?im)^Matched past incident:\s*(?:No closely matching past incident\.\s*Closest:\s*)?(INC-\d{3,})\b",
        result.get("answer", ""),
    )
    return next((memory for memory in result.get("memories", []) if match and memory.get("id") == match.group(1)), None)


def verify_seeded_incident(client: Hindsight, bank_id: str, incident: dict) -> None:
    """Confirm a seeded incident recalls with its root cause and fix extracted."""
    response = client.recall(bank_id=bank_id, query=f"Incident ID: {incident['id']}", max_tokens=8192)
    fragments = [
        core.recalled_memory(item, log_missing=False)
        for item in core.memory_field(response, "results", [])
        if core.memory_text(item)
    ]
    recalled = next((
        memory for memory in core.deduplicate_incidents(fragments, limit=len(fragments))
        if memory.get("id") == incident["id"]
    ), None)
    if (not recalled or recalled.get("root_cause") != incident["root_cause"]
            or recalled.get("fix") != incident["resolution_steps"]):
        raise SystemExit(
            f"Seed verification failed for {incident['id']} in bank {bank_id}: "
            "recall did not yield the expected root_cause and fix."
        )
    print(f"Seed verification passed for {incident['id']}.", flush=True)


def wait_for_incident(client: Hindsight, bank_id: str, incident: dict) -> float | None:
    """Poll until the newly retained incident parses with its root cause and fix."""
    started = time.monotonic()
    while True:
        response = client.recall(
            bank_id=bank_id,
            query=f"Incident ID: {incident['id']}",
            max_tokens=8192,
        )
        facts = [
            core.recalled_memory(item, log_missing=False)
            for item in core.memory_field(response, "results", [])
            if core.memory_text(item)
        ]
        match = next((fact for fact in facts if fact.get("id") == incident["id"]), None)
        waited = time.monotonic() - started
        if match and match.get("root_cause") == incident["root_cause"] and match.get("fix") == incident["resolution_steps"]:
            return waited
        if waited >= POLL_TIMEOUT_SECONDS:
            return None
        time.sleep(min(POLL_INTERVAL_SECONDS, POLL_TIMEOUT_SECONDS - waited))


def wait_for_supersession(client: Hindsight, bank_id: str, alert: str, old_id: str, new_id: str) -> float | None:
    """Poll using the alert query until its supersession record is parsed from recall."""
    started = time.monotonic()
    while True:
        response = client.recall(bank_id=bank_id, query=alert)
        records = [
            record for item in core.memory_field(response, "results", [])
            if (record := core.parse_supersession(item))
        ]
        waited = time.monotonic() - started
        if any(record.get("old_id") == old_id and record.get("new_id") == new_id for record in records):
            return waited
        if waited >= POLL_TIMEOUT_SECONDS:
            return None
        time.sleep(min(POLL_INTERVAL_SECONDS, POLL_TIMEOUT_SECONDS - waited))


def diagnose_with_diagnostics(alert: str, bank_id: str) -> tuple[dict, dict, float]:
    """Run diagnosis while recording its candidate ordering and validator decision."""
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
    started = time.monotonic()
    try:
        result = core.diagnose_with_memory(alert, bank_id=bank_id)
    finally:
        diagnose_module.validate_memory_answer = original_validator
        diagnose_module.rank_feedback_candidates = original_ranker
    captured.setdefault("validator", {
        "accepted_llm_answer": None,
        "used_template": None,
        "rejection_reasons": ["validator was not reached"],
    })
    captured.setdefault("selection_candidate_ids_in_order", [])
    return result, captured, time.monotonic() - started


def stage_diagnostics(result: dict, captured: dict, seconds_waited: float) -> dict:
    """Report recalled supersessions, demotion flags, candidate order, and validation."""
    memories = result.get("memories", [])
    raw_supersessions = [
        record for record in result.get("feedback", [])
        if record.get("record_type") == "supersession"
    ]
    unique_supersessions = {
        (record.get("old_id"), record.get("new_id"), record.get("relation"), record.get("reason")): record
        for record in raw_supersessions
    }
    associated = core.associate_supersessions(memories, list(unique_supersessions.values()))
    outdated_ids = {record["old_id"] for record in associated}
    candidate_ids = captured.get("selection_candidate_ids_in_order", [])
    memory_by_id = {memory.get("id"): memory for memory in memories}
    ordered_ids = list(dict.fromkeys(candidate_ids + [memory.get("id") for memory in memories]))
    badge_texts = [core.supersession_badge_text(record) for record in associated]
    cited_best = selected_memory(result)
    return {
        "best_match_id": cited_best.get("id") if cited_best else None,
        "supersession_records_recalled_count": len(unique_supersessions),
        "superseded_ids_detected": sorted(outdated_ids),
        "candidate_ids_in_order": candidate_ids,
        "candidate_list_in_order": [
            {
                "incident_id": incident_id,
                "eligible": incident_id not in outdated_ids,
                "outdated": incident_id in outdated_ids,
            }
            for incident_id in ordered_ids if incident_id in memory_by_id
        ],
        "outdated_badge_shown": any(text.startswith("Outdated: superseded by ") for text in badge_texts),
        "outdated_badges": badge_texts,
        "outdated_notice_in_answer": bool(associated and "Outdated:" in result.get("answer", "")),
        "validator": captured["validator"],
        "seconds_waited": round(seconds_waited, 1),
        "diagnosis_seconds": round(captured.get("diagnosis_seconds", 0), 2),
        "error": result.get("error", ""),
    }


def save_report(report: dict) -> None:
    """Write and print the proof result."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


def main() -> None:
    """Run baseline, new-incident-only, and confirmed-supersession stages."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", help="Use this Hindsight bank instead of a fresh timestamped bank")
    args = parser.parse_args()

    hindsight_url = os.getenv("HINDSIGHT_URL", "").strip()
    api_key = os.getenv("HINDSIGHT_API_KEY", "").strip()
    if not hindsight_url:
        raise SystemExit("Set HINDSIGHT_URL before running the supersession proof.")
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    bank_id = args.bank or f"oncall-supersede-demo-{run_stamp}-{uuid.uuid4().hex[:6]}"
    print(f"Supersession proof bank: {bank_id}", flush=True)
    client = Hindsight(base_url=hindsight_url, api_key=api_key) if api_key else Hindsight(base_url=hindsight_url)

    with (ROOT / "data" / "incidents.json").open(encoding="utf-8") as handle:
        incidents = json.load(handle)
    for incident in incidents:
        content, metadata = build_incident_document(incident)
        client.retain(
            bank_id=bank_id, content=content,
            context="resolved production incident postmortem",
            timestamp=incident["timestamp"], document_id=incident["id"], metadata=metadata,
        )
    if not incidents:
        raise SystemExit("Seed verification failed: data/incidents.json contains no incidents.")
    verify_seeded_incident(client, bank_id, incidents[0])

    with (ROOT / "data" / "test_incidents.json").open(encoding="utf-8") as handle:
        test_case = next(row for row in json.load(handle) if row["id"] == PROOF_CASE_ID)
    alert = test_case["alert_log"]
    baseline_result, baseline_capture, baseline_elapsed = diagnose_with_diagnostics(alert, bank_id)
    baseline_match = selected_memory(baseline_result)
    if not baseline_match:
        raise RuntimeError(f"Stage A failed: no recalled incident was selected for {PROOF_CASE_ID}.")
    baseline = stage_diagnostics(baseline_result, baseline_capture, 0)
    baseline["diagnosis_seconds"] = round(baseline_elapsed, 2)

    now = datetime.now(timezone.utc)
    timestamp = now.isoformat().replace("+00:00", "Z")
    new_id = f"INC-{now.strftime('%Y%m%d%H%M%S%f')}"
    # This correction shares the auth/Redis domain but omits the alert's exact IP and
    # connection-refused wording. It asserts an ACL cause, contradicting the old
    # resolver-cache diagnosis, while leaving the old incident a strong Stage B match.
    old_root_cause = baseline_match.get("root_cause", "the prior resolver diagnosis")
    new_incident = {
        "id": new_id,
        "date": now.date().isoformat(),
        "timestamp": timestamp,
        "service": "auth-service",
        "severity": "SEV-2",
        "alert_log": "Authentication traffic could not reach the replacement cache node during a post-maintenance network reachability check.",
        "root_cause": (
            "A network ACL blocked auth-service hosts from reaching the replacement Redis primary; "
            f"resolver answers were current, so {baseline_match['id']}'s recorded "
            f"diagnosis ({old_root_cause}) was incorrect."
        ),
        "root_cause_category": "network_acl_after_failover",
        "resolution_steps": (
            "Allow the auth-service subnet to reach the active Redis primary on TCP/6379, "
            "then verify the active endpoint from an auth worker."
        ),
        "mttr_minutes": 21,
        "runbook": "redis-failover-network-access",
    }
    new_text, new_metadata = build_incident_document(new_incident)
    client.retain(
        bank_id=bank_id, content=new_text,
        context="resolved production incident postmortem",
        timestamp=timestamp, document_id=new_id, metadata=new_metadata,
    )
    stage_b_wait = wait_for_incident(client, bank_id, new_incident)
    if stage_b_wait is None:
        error = f"Stage B timed out after {POLL_TIMEOUT_SECONDS}s waiting for {new_id} to be recalled."
        save_report({
            "bank_id": bank_id,
            "pair_selection": {"test_case_id": PROOF_CASE_ID, "old_incident_id": baseline_match["id"], "new_incident_id": new_id},
            "stage_A_baseline": baseline,
            "stage_B_new_incident_only": {
                "best_match_id": None, "supersession_records_recalled_count": 0,
                "superseded_ids_detected": [], "candidate_ids_in_order": [],
                "candidate_list_in_order": [], "outdated_badge_shown": False,
                "outdated_badges": [], "validator": {"accepted_llm_answer": None, "used_template": None, "rejection_reasons": ["diagnosis not run"]},
                "seconds_waited": POLL_TIMEOUT_SECONDS, "error": error,
            },
            "passed": False,
        })
        raise SystemExit(error)
    stage_b_result, stage_b_capture, stage_b_elapsed = diagnose_with_diagnostics(alert, bank_id)
    stage_b = stage_diagnostics(stage_b_result, stage_b_capture, stage_b_wait)
    stage_b["diagnosis_seconds"] = round(stage_b_elapsed, 2)

    old_id = baseline_match["id"]
    stage_b_candidates = {entry["incident_id"]: entry for entry in stage_b["candidate_list_in_order"]}
    if old_id not in stage_b_candidates:
        report = {
            "bank_id": bank_id,
            "pair_selection": {
                "test_case_id": PROOF_CASE_ID,
                "old_incident_id": old_id,
                "new_incident_id": new_id,
                "explanation": "The new ACL correction uses lower-overlap symptoms so the old resolver-cache incident should remain a Stage B candidate.",
                "suitable_for_causal_comparison": False,
                "reason": "The old baseline incident was absent from Stage B's candidate list, so Stage C cannot isolate its demotion.",
            },
            "stage_A_baseline": baseline,
            "stage_B_new_incident_only": stage_b,
            "passed": False,
        }
        save_report(report)
        raise SystemExit(report["pair_selection"]["reason"])

    reason = (
        f"The new postmortem corrects {old_id}: the resolver was current and an ACL blocked "
        "the auth-service subnet from the replacement Redis primary."
    )
    supersession_text = (
        "Record type: supersession\n"
        f"Old incident: {old_id}\nNew incident: {new_id}\n"
        "Relation: contradicts\n"
        f"Reason: {reason}\nAlert/error log: {alert}\nDate: {now.date().isoformat()}"
    )
    supersession_metadata = {
        "record_type": "supersession", "old_id": old_id, "new_id": new_id,
        "relation": "contradicts", "reason": reason, "date": now.date().isoformat(),
        "alert": alert,
    }
    client.retain(
        bank_id=bank_id, content=supersession_text,
        context="confirmed incident supersession record",
        timestamp=timestamp, document_id=f"supersede-{old_id}-{new_id}",
        metadata=supersession_metadata,
    )
    stage_c_wait = wait_for_supersession(client, bank_id, alert, old_id, new_id)
    if stage_c_wait is None:
        report = {
            "bank_id": bank_id,
            "pair_selection": {
                "test_case_id": PROOF_CASE_ID,
                "old_incident_id": old_id,
                "new_incident_id": new_id,
                "explanation": "Lower-overlap correction preserves the old incident in Stage B for an isolated demotion check.",
                "suitable_for_causal_comparison": True,
            },
            "stage_A_baseline": baseline,
            "stage_B_new_incident_only": stage_b,
            "stage_C_confirmed_supersession": {
                "best_match_id": None,
                "supersession_records_recalled_count": 0,
                "superseded_ids_detected": [],
                "candidate_ids_in_order": [],
                "candidate_list_in_order": [],
                "outdated_badge_shown": False,
                "outdated_badges": [],
                "validator": {"accepted_llm_answer": None, "used_template": None, "rejection_reasons": ["diagnosis not run"]},
                "seconds_waited": POLL_TIMEOUT_SECONDS,
                "diagnosis_seconds": 0,
                "error": f"Supersession record was not recalled within {POLL_TIMEOUT_SECONDS}s.",
            },
            "passed": False,
        }
        save_report(report)
        raise SystemExit(report["stage_C_confirmed_supersession"]["error"])

    diagnosis_poll_started = time.monotonic()
    while True:
        stage_c_result, stage_c_capture, stage_c_elapsed = diagnose_with_diagnostics(alert, bank_id)
        recalled_records = [
            record for record in stage_c_result.get("feedback", [])
            if record.get("record_type") == "supersession"
            and record.get("old_id") == old_id and record.get("new_id") == new_id
        ]
        associated_records = core.associate_supersessions(
            stage_c_result.get("memories", []), recalled_records
        )
        diagnosis_wait = time.monotonic() - diagnosis_poll_started
        if associated_records or stage_c_wait + diagnosis_wait >= POLL_TIMEOUT_SECONDS:
            break
        time.sleep(min(POLL_INTERVAL_SECONDS, POLL_TIMEOUT_SECONDS - stage_c_wait - diagnosis_wait))
    stage_c_wait += diagnosis_wait
    stage_c = stage_diagnostics(stage_c_result, stage_c_capture, stage_c_wait)
    stage_c["diagnosis_seconds"] = round(stage_c_elapsed, 2)
    stage_c_old = next((item for item in stage_c["candidate_list_in_order"] if item["incident_id"] == old_id), None)
    stage_b_old = stage_b_candidates[old_id]
    expected_badge_prefix = f"Outdated: superseded by {new_id} "
    target_badge_shown = any(badge.startswith(expected_badge_prefix) for badge in stage_c["outdated_badges"])
    newer_is_candidate = new_id in stage_c["candidate_ids_in_order"]
    passed = bool(
        stage_b["supersession_records_recalled_count"] == 0
        and stage_b_old["eligible"]
        and stage_b["best_match_id"] == old_id
        and stage_c["supersession_records_recalled_count"] >= 1
        and old_id in stage_c["superseded_ids_detected"]
        and stage_c_old and stage_c_old["outdated"] and not stage_c_old["eligible"]
        and target_badge_shown
        and stage_c["best_match_id"] != old_id
        and (not newer_is_candidate or stage_c["best_match_id"] == new_id)
    )
    pair_selection = {
        "test_case_id": PROOF_CASE_ID,
        "old_incident_id": old_id,
        "new_incident_id": new_id,
        "explanation": (
            "TEST-02 baseline matches the stale-primary history. The new incident describes a distinct ACL failure "
            "without repeating the alert's exact endpoint/error, so Stage B can retain the old match; Stage C adds "
            "only the explicit old-to-new contradiction record."
        ),
        "stage_B_old_incident_was_candidate": old_id in stage_b_candidates,
        "stage_B_old_incident_was_best_match": stage_b["best_match_id"] == old_id,
        "suitable_for_causal_comparison": bool(stage_b_old["eligible"] and stage_b["best_match_id"] == old_id),
    }
    report = {
        "bank_id": bank_id,
        "pair_selection": pair_selection,
        "stage_A_baseline": baseline,
        "stage_B_new_incident_only": stage_b,
        "stage_C_confirmed_supersession": stage_c,
        "effect_from_supersession_record": {
            "old_incident_was_eligible_in_B": stage_b_old["eligible"],
            "old_incident_is_outdated_in_C": bool(stage_c_old and stage_c_old["outdated"]),
            "badge_appears_only_in_C": not stage_b["outdated_badge_shown"] and stage_c["outdated_badge_shown"],
            "target_supersession_badge_shown": target_badge_shown,
            "new_incident_was_candidate": newer_is_candidate,
            "new_incident_selected_if_recalled": not newer_is_candidate or stage_c["best_match_id"] == new_id,
            "best_match_changed_away_from_old": stage_c["best_match_id"] != old_id,
        },
        "passed": passed,
    }
    save_report(report)
    if not passed:
        raise SystemExit("Supersession proof was inconclusive; inspect the saved stage diagnostics and pair selection.")


if __name__ == "__main__":
    main()
