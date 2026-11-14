import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any
from hindsight_client import Hindsight
from .config import BANK_ID, HINDSIGHT_URL, HINDSIGHT_API_KEY, HINDSIGHT_TIMEOUT_SECONDS, HINDSIGHT_RETRIES, HINDSIGHT_RETRY_DELAY_SECONDS
from .parsing import memory_text, memory_field, recalled_memory, deduplicate_incidents, parse_feedback, parse_supersession
logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4)
_REQUIRED_INCIDENT_FIELDS = ("id", "date", "service", "root_cause", "fix", "mttr")


def _parse_incident_fragments(items: list[Any]) -> list[dict[str, str]]:
    """Parse recalled incident fragments and emit one completeness summary."""
    facts = [recalled_memory(item, log_missing=False) for item in items if memory_text(item)]
    merged = deduplicate_incidents(facts, limit=len(facts))
    complete = [
        incident for incident in merged
        if all(incident.get(field) for field in _REQUIRED_INCIDENT_FIELDS)
    ]
    partial = [
        incident for incident in merged
        if not all(incident.get(field) for field in _REQUIRED_INCIDENT_FIELDS)
    ]
    unknown_id_fragments = [fact for fact in facts if not fact.get("id")]
    details = [
        f"{incident.get('id') or 'unknown'} missing "
        + ",".join(field for field in _REQUIRED_INCIDENT_FIELDS if not incident.get(field))
        for incident in partial
    ]
    if unknown_id_fragments:
        details.append(f"{len(unknown_id_fragments)} fragment(s) without an incident ID")
    message = (
        "Hindsight recall parsed %d fragments, %d complete incidents, %d partial incidents%s"
        % (len(facts), len(complete), len(partial) + len(unknown_id_fragments),
           f"; details: {'; '.join(details)}" if details else "")
    )
    (logger.warning if details else logger.info)(message)
    return facts


def _deduplicate_feedback_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse repeated recall fragments for the same retained feedback record."""
    unique = {}
    for record in records:
        key = record.get("feedback_id") or (
            record.get("outcome"), record.get("incident_id"), record.get("fix"),
            record.get("date"), record.get("note"), record.get("text"),
        )
        unique[key] = record
    return list(unique.values())


def _deduplicate_supersession_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse repeated recall fragments for the same supersession record."""
    unique = {
        (record.get("old_id"), record.get("new_id"), record.get("relation"), record.get("date"), record.get("reason")): record
        for record in records
    }
    return list(unique.values())

def get_hindsight_client() -> Hindsight:
    """Get hindsight client."""
    if not HINDSIGHT_URL:
        raise RuntimeError("Set HINDSIGHT_URL in your .env file to connect to Hindsight.")
    if HINDSIGHT_API_KEY:
        return Hindsight(base_url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY)
    return Hindsight(base_url=HINDSIGHT_URL)

def recall_incidents(
    client: Hindsight, incident: str, bank_id: str = BANK_ID
) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    """Recall and reflect on incidents relevant to the alert."""
    recalled = client.recall(bank_id=bank_id, query=incident)
    facts = _parse_incident_fragments(memory_field(recalled, "results", []))

    reflection = client.reflect(bank_id=bank_id, query=incident)
    reflection_text = memory_text(reflection)
    return deduplicate_incidents(facts), facts, reflection_text

def _recall_with_timeout(client: Hindsight, incident: str, bank_id: str, limit: int = 5):
    def call():
        recall_kwargs = {"bank_id": bank_id, "query": incident}
        if limit > 5:
            # max_tokens is the supported way to give a larger recall more room;
            # the requested incident count is still enforced when deduplicating.
            recall_kwargs["max_tokens"] = 8192
        recalled = client.recall(**recall_kwargs)
        items = [item for item in memory_field(recalled, "results", []) if memory_text(item)]
        feedback = _deduplicate_feedback_records([record for item in items if (record := parse_feedback(item))])
        supersessions = _deduplicate_supersession_records([record for item in items if (record := parse_supersession(item))])
        incident_items = [item for item in items if not parse_feedback(item) and not parse_supersession(item)]
        facts = _parse_incident_fragments(incident_items)
        reflection = client.reflect(bank_id=bank_id, query=incident)
        return deduplicate_incidents(facts, limit=limit), facts, memory_text(reflection), feedback + supersessions

    last_error = None
    for attempt in range(HINDSIGHT_RETRIES + 1):
        future = _executor.submit(call)
        try:
            return future.result(timeout=HINDSIGHT_TIMEOUT_SECONDS), ""
        except FutureTimeout:
            future.cancel()
            last_error = f"Hindsight recall/reflect timed out after {HINDSIGHT_TIMEOUT_SECONDS}s."
        except Exception as exc:
            last_error = f"Hindsight recall/reflect failed: {exc}"
        if attempt < HINDSIGHT_RETRIES:
            time.sleep(HINDSIGHT_RETRY_DELAY_SECONDS)
            logger.warning("%s Retrying once.", last_error)
    return ([], [], "", []), last_error

def reflect_now(incident_text: str, bank_id: str = BANK_ID) -> str:
    """Reflect now."""
    client = get_hindsight_client()
    last_error = "Hindsight reflect failed."
    for attempt in range(HINDSIGHT_RETRIES + 1):
        future = _executor.submit(client.reflect, bank_id=bank_id, query=incident_text)
        try:
            return memory_text(future.result(timeout=HINDSIGHT_TIMEOUT_SECONDS))
        except FutureTimeout:
            future.cancel()
            last_error = f"Hindsight reflect timed out after {HINDSIGHT_TIMEOUT_SECONDS}s."
        except Exception as exc:
            last_error = f"Hindsight reflect failed: {exc}"
        if attempt < HINDSIGHT_RETRIES:
            time.sleep(HINDSIGHT_RETRY_DELAY_SECONDS)
    return last_error


def build_incident_document(incident: dict) -> tuple[str, dict[str, str]]:
    """Render a seed incident as Hindsight text and string metadata."""
    incident_date = str(incident.get("date") or incident["timestamp"][:10])
    root_cause = str(incident["root_cause"])
    fix = str(incident["resolution_steps"])
    side_effects = str(incident.get("side_effects") or "").strip()
    content_lines = [
        f"Incident ID: {incident['id']}", f"Service: {incident['service']}",
        f"Severity: {incident['severity']}", f"Date: {incident_date}",
        f"Alert/error log: {incident['alert_log']}", f"Root cause: {root_cause}",
        f"Fix applied: {fix}", f"MTTR: {incident['mttr_minutes']} minutes",
        f"Runbook: {incident['runbook']}",
    ]
    if side_effects:
        content_lines.append(f"Side effects: {side_effects}")
    content = "\n".join(content_lines)
    metadata = {"service": str(incident["service"]), "severity": str(incident["severity"]),
        "date": incident_date, "root_cause": root_cause, "fix": fix, "exact_fix": fix,
        "incident_id": str(incident["id"]), "mttr_minutes": str(incident["mttr_minutes"]),
        "mttr": f"{incident['mttr_minutes']} minutes", "root_cause_category": str(incident["root_cause_category"]),
        "side_effects": side_effects}
    return content, metadata
