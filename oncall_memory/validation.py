import logging
import re
from .config import FEEDBACK_OUTCOME_FAILED
logger = logging.getLogger(__name__)

def _memory_answer_fallback(incident_text: str, memories: list[dict[str, str]]) -> str:
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
    cited_ids = set(re.findall(r"\bINC-\d{3,}\b", answer))
    matched_line = re.search(r"(?im)^Matched past incident:\s*(.*)$", answer)
    fix_line = re.search(r"(?im)^Fix that worked before:\s*(.*)$", answer)
    match_text = matched_line.group(1) if matched_line else ""
    fix_text = fix_line.group(1).strip() if fix_line else ""
    no_match = "No closely matching past incident." in match_text
    match_id = re.search(r"\bINC-\d{3,}\b", match_text)
    selected = next((m for m in memories if match_id and m.get("id") == match_id.group(0)), None)
    valid = bool(matched_line and fix_line and not re.search(r"Relevant recalled history is available|placeholder|<ID>|<exact fix>", answer, re.I))
    if no_match:
        valid = valid and bool(selected) and fix_text == "No proven fix for this alert."
    else:
        valid = valid and bool(selected and selected.get("root_cause") and selected.get("fix"))
        if selected:
            valid = valid and fix_text == selected.get("fix", "").strip()
    valid = valid and not (cited_ids - allowed_ids)
    if not valid:
        logger.warning("Memory answer failed source validation; using recalled-data template.")
        answer = _memory_answer_fallback(incident_text, memories)
    failed_fixes = [item.get("fix", "").strip() for item in (feedback or []) if item.get("outcome") == "did not work" and item.get("fix")]
    for failed_fix in failed_fixes:
        if failed_fix.lower() in answer.lower():
            answer = re.sub(r"(?im)^Fix that worked before:.*$", "Fix that worked before: No proven fix for this alert.", answer)
        if f"Previously failed: {failed_fix}" not in answer:
            answer += f"\nPreviously failed: {failed_fix}"
    return answer
