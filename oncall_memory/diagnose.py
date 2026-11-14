import logging
import re
from collections import Counter
from .config import BANK_ID
from .memory import get_hindsight_client, _recall_with_timeout
from .parsing import associate_feedback, rank_feedback_candidates
from .prompts import ask_groq, answer_memory_question, compress_reflection, no_memory_fallback, recalled_memory_fallback
from .validation import validate_memory_answer, validate_memory_question_answer, _memory_answer_fallback
logger = logging.getLogger(__name__)

def diagnose_without_memory(incident_text: str) -> str:
    """Diagnose without memory."""
    try:
        return ask_groq(incident_text)
    except Exception as exc:
        logger.error("Without-memory diagnosis unavailable: %s", exc)
        return no_memory_fallback(incident_text)

def diagnose_with_memory(incident_text: str, bank_id: str | None = None) -> dict:
    """Diagnose with memory."""
    target_bank = bank_id or BANK_ID
    memories, raw_facts, reflection, feedback, error = [], [], "", [], ""
    try:
        client = get_hindsight_client()
        (memories, raw_facts, reflection, feedback), error = _recall_with_timeout(
            client, incident_text, target_bank
        )
    except Exception as exc:
        error = f"Hindsight client unavailable: {exc}"

    associated_feedback = associate_feedback(memories, feedback)
    failed_feedback = [item for item in associated_feedback if item.get("outcome") == "did not work"]
    failed_ids = {incident_id for item in failed_feedback for incident_id in item.get("matched_incident_ids", [])}
    candidates = [memory for memory in memories if memory.get("id") not in failed_ids]
    candidates = rank_feedback_candidates(candidates, associated_feedback)
    ranked_memories = candidates + [memory for memory in memories if memory.get("id") in failed_ids]
    try:
        if memories and not candidates and failed_feedback:
            answer = validate_memory_answer(
                _memory_answer_fallback(incident_text, ranked_memories, failed_ids),
                ranked_memories, incident_text, associated_feedback,
            )
        else:
            answer = validate_memory_answer(
                compress_reflection(reflection, candidates, associated_feedback),
                ranked_memories, incident_text, associated_feedback,
            )
    except Exception as exc:
        logger.error("Memory diagnosis unavailable: %s", exc)
        answer = recalled_memory_fallback(ranked_memories)
        for item in associated_feedback:
            if item.get("outcome") == "did not work" and item.get("fix"):
                answer += f"\nPreviously failed: {item['fix']}"
    return {
        "answer": answer,
        "recalled_incidents": [memory["id"] for memory in ranked_memories if memory["id"]],
        "reflection": reflection,
        "error": error,
        "memories": ranked_memories,
        "raw_facts": raw_facts,
        "feedback": feedback,
    }


def ask_memory(question: str, bank_id: str | None = None) -> str:
    """Recall up to twenty incidents and answer a question with validated citations."""
    target_bank = bank_id or BANK_ID
    try:
        client = get_hindsight_client()
        (memories, _raw_facts, reflection, _feedback), error = _recall_with_timeout(
            client, question, target_bank, limit=20
        )
    except Exception as exc:
        return f"Memory lookup failed: {exc}"
    if not memories:
        if error:
            return f"Memory lookup timed out or failed: {error}"
        return "No relevant history was recalled for this question."
    try:
        answer = answer_memory_question(question, memories, reflection)
    except Exception as exc:
        logger.error("Ask the Memory answer failed: %s", exc)
        return f"Memory answer unavailable: {exc}"
    return validate_memory_question_answer(answer, memories)


def compute_recurring_insights(memories: list[dict[str, str]]) -> dict[str, object] | None:
    """Compute service, root-cause, and MTTR summaries from recalled fields."""
    usable = [memory for memory in memories if memory.get("service")]
    if not usable:
        return None
    services = Counter(memory["service"] for memory in usable)
    service = services.most_common(1)[0][0]
    matching = [memory for memory in usable if memory["service"] == service]
    causes = Counter(memory.get("root_cause", "").strip() for memory in matching if memory.get("root_cause", "").strip())
    mttr_values = []
    for memory in matching:
        match = re.search(r"\d+(?:\.\d+)?", str(memory.get("mttr", "")))
        if match:
            mttr_values.append(float(match.group(0)))
    return {
        "service": service,
        "service_count": len(matching),
        "recalled_count": len(memories),
        "most_common_root_cause": causes.most_common(1)[0][0] if causes else "Not recorded",
        "average_mttr_minutes": sum(mttr_values) / len(mttr_values) if mttr_values else None,
        "mttr_sample_count": len(mttr_values),
    }
