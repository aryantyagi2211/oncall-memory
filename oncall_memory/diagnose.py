import logging
from .config import BANK_ID
from .memory import get_hindsight_client, _recall_with_timeout
from .parsing import memory_text
from .prompts import ask_groq, compress_reflection, no_memory_fallback, recalled_memory_fallback
from .validation import validate_memory_answer
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

    try:
        answer = validate_memory_answer(compress_reflection(reflection, memories, feedback), memories, incident_text, feedback)
    except Exception as exc:
        logger.error("Memory diagnosis unavailable: %s", exc)
        answer = recalled_memory_fallback(memories)
        for item in feedback:
            if item.get("outcome") == "did not work" and item.get("fix"):
                answer += f"\nPreviously failed: {item['fix']}"
    return {
        "answer": answer,
        "recalled_incidents": [memory["id"] for memory in memories if memory["id"]],
        "reflection": reflection,
        "error": error,
        "memories": memories,
        "raw_facts": raw_facts,
        "feedback": feedback,
    }
