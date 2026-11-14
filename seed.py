import json
import os
from pathlib import Path

from dotenv import load_dotenv
from hindsight_client import Hindsight


load_dotenv()

HINDSIGHT_URL = os.getenv("HINDSIGHT_URL", "").strip()
HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY", "").strip()
BANK_ID = "oncall-v3"
INCIDENTS_PATH = Path(__file__).parent / "data" / "incidents.json"


def main() -> None:
    if not HINDSIGHT_URL:
        raise SystemExit("Set HINDSIGHT_URL in .env before seeding incidents.")

    if HINDSIGHT_API_KEY:
        client = Hindsight(base_url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY)
    else:
        client = Hindsight(base_url=HINDSIGHT_URL)

    with INCIDENTS_PATH.open(encoding="utf-8") as incidents_file:
        incidents = json.load(incidents_file)

    for index, incident in enumerate(incidents, start=1):
        content = (
            f"Incident ID: {incident['id']}\n"
            f"Timestamp: {incident['timestamp']}\n"
            f"Service: {incident['service']}\n"
            f"Severity: {incident['severity']}\n"
            f"Alert/error log: {incident['alert_log']}\n"
            f"Root cause: {incident['root_cause']}\n"
            f"Resolution steps: {incident['resolution_steps']}\n"
            f"MTTR: {incident['mttr_minutes']} minutes\n"
            f"Runbook: {incident['runbook']}"
        )
        client.retain(
            bank_id=BANK_ID,
            content=content,
            context="resolved production incident postmortem",
            timestamp=incident["timestamp"],
            document_id=incident["id"],
            metadata={
                "service": str(incident["service"]),
                "severity": str(incident["severity"]),
                "mttr_minutes": str(incident["mttr_minutes"]),
                "root_cause_category": str(incident["root_cause_category"]),
            },
        )
        print(f"Retained {incident['id']} ({index}/{len(incidents)})")


if __name__ == "__main__":
    main()
