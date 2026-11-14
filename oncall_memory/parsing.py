import logging
import re
from typing import Any
logger = logging.getLogger(__name__)

def memory_text(value: Any) -> str:
    """Memory text."""
    if isinstance(value, dict):
        return str(value.get("text", value)).strip()
    return str(getattr(value, "text", value)).strip()

def memory_field(value: Any, key: str, default: Any = "") -> Any:
    """Memory field."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

def recalled_memory(item: Any) -> dict[str, str]:
    """Recalled memory."""
    text = memory_text(item)
    metadata = memory_field(item, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    incident_id = str(metadata.get("incident_id") or "")
    if not incident_id:
        match = re.search(r"\bINC-\d{3,}\b", text)
        incident_id = match.group(0) if match else ""

    incident_date = str(metadata.get("date") or "")
    if not incident_date:
        match = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text)
        incident_date = match.group(0) if match else ""

    service = str(metadata.get("service") or "")
    if not service:
        match = re.search(r"Service:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
        service = match.group(1).strip() if match else ""

    exact_fix = str(metadata.get("fix") or metadata.get("exact_fix") or "")
    if not exact_fix:
        match = re.search(r"(?:Fix applied|Exact fix|Fix|Resolution steps):\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
        exact_fix = match.group(1).strip() if match else ""

    root_cause = str(metadata.get("root_cause") or "")
    if not root_cause:
        match = re.search(r"Root cause:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
        root_cause = match.group(1).strip() if match else ""

    mttr = str(metadata.get("mttr") or metadata.get("mttr_minutes") or "")
    if not mttr:
        match = re.search(r"MTTR:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
        mttr = match.group(1).strip() if match else ""

    parsed = {
        "id": incident_id,
        "date": incident_date,
        "service": service,
        "root_cause": root_cause,
        "fix": exact_fix,
        "mttr": mttr,
        "text": text,
    }
    missing = [field for field in ("id", "date", "service", "root_cause", "fix", "mttr") if not parsed[field]]
    if missing:
        logger.warning("Recalled incident %s is missing fields: %s", incident_id or "unknown", ", ".join(missing))
    return parsed

def deduplicate_incidents(
    facts: list[dict[str, str]], limit: int = 5
) -> list[dict[str, str]]:
    """Merge duplicate recalled incident facts while preserving relevance order."""
    # Hindsight recall results are relevance-ranked; preserve their order when merging facts.
    incidents: dict[str, dict[str, str]] = {}
    for fact in facts:
        incident_id = fact["id"]
        if not incident_id:
            continue
        incident = incidents.setdefault(
            incident_id,
            {
                "id": incident_id,
                "date": "",
                "service": "",
                "root_cause": "",
                "fix": "",
                "mttr": "",
                "text": "",
            },
        )
        for field in ("date", "service", "root_cause", "fix", "mttr"):
            if not incident[field] and fact[field]:
                incident[field] = fact[field]
        if fact["text"]:
            incident["text"] = "\n".join(
                filter(None, (incident["text"], fact["text"]))
            )
    return list(incidents.values())[:limit]

def parse_feedback(item: Any) -> dict[str, str] | None:
    """Parse feedback."""
    text = memory_text(item)
    outcome = re.search(r"(?im)^Outcome:\s*(worked|did not work)\s*$", text)
    if not outcome:
        return None
    return {
        "text": text,
        "outcome": outcome.group(1).lower(),
        "fix": (re.search(r"(?im)^Suggested fix:\s*(.+)$", text) or [None, ""])[1].strip(),
        "incident_id": (re.search(r"(?im)^Cited incident:\s*(INC-\d{3,}|none)\s*$", text) or [None, "none"])[1],
    }

def recalled_mttr_label(
    incident_id: str,
    raw_facts: list[dict[str, str]],
    fallback: str = "",
) -> str:
    """Format an incident's MTTR from its recalled fact text."""
    minutes_pattern = re.compile(
        r"\b(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|min)\b",
        re.IGNORECASE,
    )
    for fact in raw_facts:
        text = fact.get("text", "")
        for sentence in re.split(r"(?<=[.!?])\s+|\r?\n+", text):
            if (
                incident_id in sentence
                and re.search(r"\b(?:MTTR|Mean Time To Repair)\b", sentence, re.IGNORECASE)
            ):
                match = minutes_pattern.search(sentence)
                if match:
                    return f"{match.group(1)} min"

    if fallback:
        match = minutes_pattern.search(fallback)
        if match:
            return f"{match.group(1)} min"
        return fallback
    return "unavailable"
