import logging
import re
from .config import FEEDBACK_OUTCOME_FAILED
logger = logging.getLogger(__name__)

def _memory_answer_fallback(incident_text: str, memories: list[dict[str, str]], failed_ids: set[str] | None = None) -> str:
    if not memories:
        return (
            "Diagnosis: No recalled incident is available to compare with this alert.\n"
            "Matched past incident: No closely matching past incident. Closest: none (no incidents were recalled).\n"
            "Fix that worked before: No proven fix for this alert.\n"
            "Next 3 steps:\n- Verify the active error and affected dependency.\n"
            "- Check recent changes and current capacity.\n"
            "- Apply the service runbook mitigation and verify recovery."
        )

    alert_terms = set(re.findall(r"[a-z0-9]+", incident_text.lower()))
    ranked = []
    for memory in memories:
        if memory.get("id") in (failed_ids or set()):
            continue
        source = " ".join((memory.get("service", ""), memory.get("root_cause", ""), memory.get("text", "")))
        terms = set(re.findall(r"[a-z0-9]+", source.lower()))
        overlap = len(alert_terms & terms)
        cause = memory.get("root_cause", "").lower()
        # Shared technical symptoms and causal concepts should outweigh incidental wording.
        for cue in ("connection pool", "too many connections", "deadlock", "serialization", "failover", "timeout", "kafka", "oom", "index", "redis"):
            if cue in incident_text.lower() and cue in source.lower():
                overlap += 5
        ranked.append((overlap, memory))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked:
        return (
            "Diagnosis: Every recalled candidate is outdated or has a fix marked unsuccessful. No current fix can be recommended from this history.\n"
            "Matched past incident: No closely matching current incident; recalled candidates are outdated or have failed fixes.\n"
            "Fix that worked before: No proven fix for this alert.\n"
            "Next 3 steps:\n- Recheck the current symptoms and affected dependency.\n"
            "- Investigate a new mitigation using the service runbook.\n"
            "- Verify recovery before recording another outcome."
        )
    score, best = ranked[0]
    incident_id = best.get("id") or ""
    root = best.get("root_cause", "")
    fix = best.get("fix", "")
    close = score >= 4 and bool(root and fix and incident_id)
    if close:
        return (
            f"Diagnosis: {root}\n"
            f"Matched past incident: {incident_id} — Its recorded cause and symptoms are the closest match to this alert.\n"
            f"Fix that worked before: {fix}\n"
            "Next 3 steps:\n"
            f"- Compare current errors with the recalled incident details [{incident_id}].\n"
            f"- Verify the dependency and capacity involved in the recorded cause [{incident_id}].\n"
            f"- Apply the recorded fix only if the cause is confirmed [{incident_id}]."
        )
    return (
        "Diagnosis: Recalled history does not establish the cause of this alert.\n"
        f"Matched past incident: No closely matching past incident. Closest: {incident_id} (its recorded symptoms or root cause differ).\n"
        "Fix that worked before: No proven fix for this alert.\n"
        "Next 3 steps:\n- Verify the active error and affected dependency.\n"
        "- Check recent changes and current capacity.\n"
        "- Follow the relevant service runbook and verify recovery."
    )

def validate_memory_answer(answer: str, memories: list[dict[str, str]], incident_text: str = "", feedback: list[dict[str, str]] | None = None) -> str:
    """Validate memory answer."""
    allowed_ids = {m.get("id", "") for m in memories if m.get("id")}
    supersession_records = [record for record in (feedback or []) if record.get("record_type") == "supersession"]
    for record in supersession_records:
        allowed_ids.update(i for i in (record.get("old_id"), record.get("new_id")) if i)
    cited_ids = set(re.findall(r"\bINC-\d{3,}\b", answer))
    matched_line = re.search(r"(?im)^Matched past incident:\s*(.*)$", answer)
    fix_line = re.search(r"(?im)^Fix that worked before:\s*(.*)$", answer)
    match_text = matched_line.group(1) if matched_line else ""
    fix_text = fix_line.group(1).strip() if fix_line else ""
    no_match = "No closely matching past incident." in match_text
    match_id = re.search(r"\bINC-\d{3,}\b", match_text)
    selected = next((m for m in memories if match_id and m.get("id") == match_id.group(0)), None)
    failed_records = [record for record in (feedback or []) if record.get("outcome") == FEEDBACK_OUTCOME_FAILED]
    failed_ids = {incident_id for record in failed_records for incident_id in record.get("matched_incident_ids", [])}
    recalled_ids = {memory.get("id") for memory in memories}
    superseded_ids = {record["old_id"] for record in supersession_records}
    superseded_selection = bool(selected and selected.get("id") in superseded_ids)
    valid = bool(matched_line and fix_line and not re.search(r"Relevant recalled history is available|placeholder|<ID>|<exact fix>", answer, re.I))
    if no_match:
        valid = valid and bool(selected) and fix_text == "No proven fix for this alert."
    else:
        valid = valid and bool(selected and selected.get("root_cause") and selected.get("fix") and selected.get("id") not in failed_ids and not superseded_selection)
        if selected:
            valid = valid and fix_text == selected.get("fix", "").strip()
    valid = valid and not (cited_ids - allowed_ids)
    if not valid:
        logger.warning("Memory answer failed source validation; using recalled-data template.")
        answer = _memory_answer_fallback(incident_text, memories, failed_ids | superseded_ids)
    # Remove ungrounded model-generated failure claims; only recalled feedback may create notices.
    answer = re.sub(r"(?im)^.*was marked ['\"]Didn't work['\"].*$\n?", "", answer)
    selected_id = re.search(r"(?im)^Matched past incident:\s*(INC-\d{3,})\b", answer)
    failed_record = next((record for record in failed_records if any(i in {m.get("id") for m in memories} for i in record.get("matched_incident_ids", []))), None)
    if failed_record and selected_id and selected_id.group(1) not in failed_ids:
        failed_id = failed_record.get("matched_incident_ids", [""])[0]
        short_fix = failed_record.get("fix", "").strip()[:100]
        notice = (f"{failed_id} fix ({short_fix}) was marked 'Didn't work' on "
                  f"{failed_record.get('date', 'date unavailable')}. Note: {failed_record.get('note', 'none')}. "
                  f"Trying {selected_id.group(1)} instead.")
        answer += f"\n{notice}"
    elif failed_records:
        record = failed_records[0]
        failed_id = next(iter(record.get("matched_incident_ids", [])), "")
        if failed_id:
            answer += (f"\n{failed_id} fix ({record.get('fix', '')[:100]}) was marked 'Didn't work' on "
                       f"{record.get('date', 'date unavailable')}. Note: {record.get('note', 'none')}. "
                       "No remaining recalled incident is available to try.")
    # Supersession claims are reconstructed only from recalled supersession records.
    answer = re.sub(r"(?im)^.*(?:superseded by|outdated:).*$\n?", "", answer)
    for record in supersession_records:
        old_id, new_id = record.get("old_id", ""), record.get("new_id", "")
        if old_id in recalled_ids:
            answer += f"\nOutdated: {old_id} was superseded by {new_id} ({record.get('reason', 'reason not recorded')})."
    return answer


def validate_memory_question_answer(answer: str, memories: list[dict[str, str]]) -> str:
    """Reject Ask-the-Memory answers with unsupported or missing incident citations."""
    allowed_ids = {memory.get("id") for memory in memories if memory.get("id")}
    cited_ids = set(re.findall(r"\bINC-\d{3,}\b", answer))
    if not memories:
        return "No relevant history was recalled for this question."
    if cited_ids - allowed_ids:
        logger.warning("Memory question answer cited IDs absent from recall results: %s", sorted(cited_ids - allowed_ids))
        return "I can't support that answer: it cited incident IDs that were not in the recalled facts."
    if not cited_ids:
        citations = ", ".join(sorted(allowed_ids))
        return f"The recalled records do not support a specific answer. Relevant incident IDs: {citations}."
    return answer
