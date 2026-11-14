import json
import os
import argparse
import time
from pathlib import Path

from dotenv import load_dotenv
from hindsight_client import Hindsight


load_dotenv()

HINDSIGHT_URL = os.getenv("HINDSIGHT_URL", "").strip()
HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY", "").strip()
BANK_ID = "oncall-v3"
INCIDENTS_PATH = Path(__file__).parent / "data" / "incidents.json"


from core import BANK_ID, HINDSIGHT_API_KEY, HINDSIGHT_URL, build_incident_document

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Retain only the first N incidents")
    args = parser.parse_args()

    if not HINDSIGHT_URL:
        raise SystemExit("Set HINDSIGHT_URL in .env before seeding incidents.")

    if HINDSIGHT_API_KEY:
        client = Hindsight(base_url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY)
    else:
        client = Hindsight(base_url=HINDSIGHT_URL)

    with INCIDENTS_PATH.open(encoding="utf-8") as incidents_file:
        incidents = json.load(incidents_file)

    if args.limit is not None:
        if args.limit < 0:
            parser.error("--limit must be non-negative")
        incidents = incidents[:args.limit]

    total_started = time.perf_counter()
    retained_count = 0
    failed_ids = []
    for index, incident in enumerate(incidents, start=1):
        incident_started = time.perf_counter()
        print(f"[{index}/{len(incidents)}] retaining {incident['id']}...", flush=True)
        content, metadata = build_incident_document(incident)
        try:
            client.retain(
                bank_id=BANK_ID,
                content=content,
                context="resolved production incident postmortem",
                timestamp=incident["timestamp"],
                document_id=incident["id"],
                metadata=metadata,
            )
            retained_count += 1
            print(
                f"[{index}/{len(incidents)}] retained {incident['id']} "
                f"in {time.perf_counter() - incident_started:.2f}s",
                flush=True,
            )
        except Exception as error:
            failed_ids.append(incident["id"])
            print(
                f"[{index}/{len(incidents)}] failed {incident['id']} "
                f"in {time.perf_counter() - incident_started:.2f}s: {error}",
                flush=True,
            )

    total_time = time.perf_counter() - total_started
    print(
        f"Summary: retained {retained_count}/{len(incidents)}; "
        f"failed IDs: {', '.join(failed_ids) if failed_ids else 'none'}; "
        f"total time: {total_time:.2f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
