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

def recalled_memory(item: Any, log_missing: bool = True) -> dict[str, str]:
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
    if missing and log_missing:
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
    metadata = memory_field(item, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    outcome_match = re.search(r"(?im)^Outcome:\s*(worked|did not work)\s*$", text)
    outcome = (outcome_match.group(1) if outcome_match else str(metadata.get("outcome") or "")).lower()
    if outcome not in {"worked", "did not work"}:
        return None
    stamp = memory_field(item, "timestamp", "") or metadata.get("timestamp", "")
    date_match = re.search(r"20\d{2}-\d{2}-\d{2}", str(stamp)) or re.search(r"(?im)^Date:\s*(20\d{2}-\d{2}-\d{2})", text)
    note_match = re.search(r"(?im)^Note:\s*(.*)$", text)
    fix_match = re.search(r"(?im)^Suggested fix:\s*(.+)$", text)
    incident_match = re.search(r"(?im)^Cited incident:\s*(INC-\d{3,}|none)\s*$", text)
    feedback_id_match = re.search(r"(?im)^Feedback ID:\s*(\S+)\s*$", text)
    return {
        "text": text,
        "feedback_id": feedback_id_match.group(1) if feedback_id_match else str(metadata.get("feedback_id") or ""),
        "outcome": outcome,
        "fix": (fix_match.group(1).strip() if fix_match else str(metadata.get("suggested_fix") or metadata.get("fix") or "").strip()),
        "incident_id": (incident_match.group(1) if incident_match else str(metadata.get("incident_id") or metadata.get("cited_incident_id") or "none")),
        "date": date_match.group(0) if date_match else str(metadata.get("date") or "date unavailable"),
        "note": note_match.group(1).strip() if note_match else str(metadata.get("note") or "none"),
    }


def parse_supersession(item: Any) -> dict[str, str] | None:
    """Parse a confirmed supersession record from recalled Hindsight content."""
    text = memory_text(item)
    metadata = memory_field(item, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    text_record_type = re.search(r"(?im)^Record type:\s*supersession\s*$", text)
    record_type = str(metadata.get("record_type") or ("supersession" if text_record_type else "")).lower()
    if record_type != "supersession":
        return None
    values = {}
    for key, label in (("old_id", "Old incident"), ("new_id", "New incident"),
                       ("relation", "Relation"), ("reason", "Reason"), ("date", "Date")):
        match = re.search(rf"(?im)^{re.escape(label)}:\s*(.+)$", text)
        metadata_key = {"old_id": "old_id", "new_id": "new_id"}.get(key, key)
        values[key] = str(metadata.get(metadata_key) or (match.group(1) if match else "")).strip()
    values["relation"] = values["relation"].lower()
    if not values["date"]:
        stamp = memory_field(item, "timestamp", "") or metadata.get("timestamp", "")
        date_match = re.search(r"20\d{2}-\d{2}-\d{2}", str(stamp))
        values["date"] = date_match.group(0) if date_match else ""
    values["text"] = text
    values["record_type"] = "supersession"
    if not re.fullmatch(r"INC-\d{3,}", values["old_id"]) or not re.fullmatch(r"INC-\d{3,}", values["new_id"]):
        return None
    if values["relation"] not in {"supersedes", "contradicts"} or not values["reason"]:
        return None
    return values


def supersession_badge_text(record: dict[str, str]) -> str:
    """Format the UI badge for one confirmed supersession record."""
    return f"Outdated: superseded by {record['new_id']} — {record['old_id']} ({record.get('reason', 'reason not recorded')})"


def associate_feedback(memories: list[dict[str, str]], feedback: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Associate feedback with recalled incidents by cited ID or exact fix."""
    associated = []
    for record in feedback:
        if record.get("record_type") == "supersession" or record.get("outcome") not in {"worked", "did not work"}:
            continue
        record_fix = " ".join(record.get("fix", "").lower().split())
        ids = [memory["id"] for memory in memories if memory.get("id") == record.get("incident_id")]
        if record_fix:
            ids.extend(memory["id"] for memory in memories
                       if " ".join(memory.get("fix", "").lower().split()) == record_fix)
        ids = list(dict.fromkeys(ids))
        if ids:
            associated.append({**record, "matched_incident_ids": ids})
    return associated


def associate_supersessions(memories: list[dict[str, str]], records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep confirmed supersessions whose older incident was recalled."""
    recalled_ids = {memory.get("id") for memory in memories}
    return [record for record in records
            if record.get("record_type") == "supersession" and record.get("old_id") in recalled_ids]


def rank_feedback_candidates(memories: list[dict[str, str]], feedback: list[dict[str, str]]) -> list[dict[str, str]]:
    """Rank incidents by recall order adjusted for recorded Worked and failed feedback."""
    worked = {memory.get("id"): 0 for memory in memories}
    failed = {memory.get("id"): 0 for memory in memories}
    supersession_targets = set()
    for record in feedback:
        if record.get("record_type") == "supersession":
            if record.get("old_id") in failed:
                failed[record["old_id"]] += 1
            if record.get("new_id") in worked:
                worked[record["new_id"]] += 1
                supersession_targets.add(record["new_id"])
            continue
        counts = worked if record.get("outcome") == "worked" else failed if record.get("outcome") == "did not work" else None
        if counts is not None:
            for incident_id in record.get("matched_incident_ids", []):
                if incident_id in counts:
                    counts[incident_id] += 1
    total = len(memories)
    return sorted(
        memories,
        key=lambda memory: (
            memory.get("id") in supersession_targets,
            float(memory.get("recall_order", total - memories.index(memory)))
            + worked.get(memory.get("id"), 0)
            - failed.get(memory.get("id"), 0)
        ),
        reverse=True,
    )

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
