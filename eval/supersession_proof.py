"""Demonstrate confirmed supersession changing a diagnosis in an isolated bank."""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from hindsight_client import Hindsight

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import core
from seed import build_incident_document

BANK_ID = "oncall-supersede-demo"
OUTPUT_PATH = ROOT / "eval" / "results" / "supersession_proof.json"


def get_best_match(result):
    """Resolve the answer's cited incident against recalled incident fields."""
    match = re.search(r"(?im)^Matched past incident:\s*(INC-\d{3,})\b", result.get("answer", ""))
    return next((item for item in result.get("memories", []) if match and item.get("id") == match.group(1)), None)


def main():
    """Seed forty incidents, confirm a supersession, and save before/after evidence."""
    hindsight_url = os.getenv("HINDSIGHT_URL", "").strip()
    api_key = os.getenv("HINDSIGHT_API_KEY", "").strip()
    if not hindsight_url:
        raise SystemExit("Set HINDSIGHT_URL before running the supersession proof.")
    client = Hindsight(base_url=hindsight_url, api_key=api_key) if api_key else Hindsight(base_url=hindsight_url)
    with (ROOT / "data" / "incidents.json").open(encoding="utf-8") as handle:
        incidents = json.load(handle)
    for incident in incidents:
        text, metadata = build_incident_document(incident)
        client.retain(bank_id=BANK_ID, content=text, context="resolved production incident postmortem",
                      timestamp=incident["timestamp"], document_id=incident["id"], metadata=metadata)

    with (ROOT / "data" / "test_incidents.json").open(encoding="utf-8") as handle:
        alert = next(row["alert_log"] for row in json.load(handle) if row["id"] == "TEST-02")
    before_result = core.diagnose_with_memory(alert, bank_id=BANK_ID)
    before = get_best_match(before_result)
    if not before:
        raise RuntimeError("The before run did not select a recalled incident; proof cannot continue.")

    new_id = "INC-900000000001"
    now = datetime.now(timezone.utc)
    new_incident = {
        "id": new_id,
        "date": now.date().isoformat(),
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "service": before.get("service") or "auth-service",
        "severity": "SEV-2",
        "alert_log": alert,
        "root_cause": f"A stale client endpoint configuration sent requests to the retired primary; the earlier diagnosis ({before.get('root_cause', 'unknown')}) was incorrect.",
        "root_cause_category": "superseding_correction",
        "resolution_steps": "Correct the client endpoint configuration and verify requests reach the active primary.",
        "mttr_minutes": 18,
        "runbook": "redis-failover-stale-connections",
    }
    new_text, new_metadata = build_incident_document(new_incident)
    client.retain(bank_id=BANK_ID, content=new_text, context="resolved production incident postmortem",
                  timestamp=new_incident["timestamp"], document_id=new_id, metadata=new_metadata)
    reason = f"The newer postmortem corrects the old diagnosis for the same alert: {new_incident['root_cause']}"
    sup_text = ("Record type: supersession\n"
                f"Old incident: {before['id']}\nNew incident: {new_id}\n"
                "Relation: contradicts\n"
                f"Reason: {reason}\nAlert/error log: {alert}\nDate: {now.date().isoformat()}")
    client.retain(bank_id=BANK_ID, content=sup_text, context="confirmed incident supersession record",
                  timestamp=now.isoformat().replace("+00:00", "Z"),
                  document_id=f"supersede-{before['id']}-{new_id}",
                  metadata={"record_type": "supersession", "old_id": before["id"], "new_id": new_id,
                            "relation": "contradicts", "reason": reason, "date": now.date().isoformat()})

    after_result = core.diagnose_with_memory(alert, bank_id=BANK_ID)
    after = get_best_match(after_result)
    recalled_supersessions = core.associate_supersessions(after_result.get("memories", []), after_result.get("feedback", []))
    report = {
        "bank_id": BANK_ID,
        "before": {"best_match_id": before["id"]},
        "after": {"best_match_id": after["id"] if after else None,
                  "outdated_badge_shown": any(row["old_id"] == before["id"] and row["new_id"] == new_id for row in recalled_supersessions)},
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
