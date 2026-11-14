"""Demonstrate that retained failed-fix feedback changes the next diagnosis."""
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from hindsight_client import Hindsight

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import core
from seed import build_incident_document

BANK_ID = "oncall-feedback-demo"
OUTPUT_PATH = ROOT / "eval" / "results" / "feedback_proof.json"


def selected_memory(result: dict) -> dict | None:
    """Return the recalled incident cited by a diagnosis, if one exists."""
    match = re.search(r"(?im)^Matched past incident:\s*(INC-\d{3,})\b", result.get("answer", ""))
    return next((memory for memory in result.get("memories", []) if match and memory.get("id") == match.group(1)), None)


def main() -> None:
    """Seed the demo bank, record a failed fix, and compare diagnoses."""
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

    with (ROOT / "data" / "test_incidents.json").open(encoding="utf-8") as handle:
        alert = next(item["alert_log"] for item in json.load(handle) if item["id"] == "TEST-02")
    before_result = core.diagnose_with_memory(alert, bank_id=BANK_ID)
    before_memory = selected_memory(before_result)
    if not before_memory or not before_memory.get("fix"):
        raise RuntimeError("Initial diagnosis did not cite a recalled incident with a fix; proof cannot continue.")

    now = datetime.now(timezone.utc)
    timestamp = now.isoformat().replace("+00:00", "Z")
    feedback_id = f"feedback-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    feedback = (
        f"Feedback ID: {feedback_id}\nNew alert summary: {alert}\n"
        f"Suggested fix: {before_memory['fix']}\nCited incident: {before_memory['id']}\n"
        "Outcome: did not work\nNote: Feedback proof demonstration."
    )
    client.retain(bank_id=BANK_ID, content=feedback,
                  context="feedback on incident-response recommendation",
                  timestamp=timestamp, document_id=feedback_id,
                  metadata={"record_type": "feedback", "outcome": "did not work",
                            "incident_id": before_memory["id"]})

    after_result = core.diagnose_with_memory(alert, bank_id=BANK_ID)
    after_memory = selected_memory(after_result)
    report = {
        "bank_id": BANK_ID,
        "alert": alert,
        "before": {"best_match_id": before_memory["id"], "fix": before_memory["fix"]},
        "feedback_document_id": feedback_id,
        "after": {"best_match_id": after_memory["id"] if after_memory else None,
                  "fix": after_memory["fix"] if after_memory else None},
        "failed_fix_avoided": not after_memory or after_memory["id"] != before_memory["id"],
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
