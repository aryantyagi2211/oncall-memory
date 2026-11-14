import logging
import re
from .llm import groq_completion
logger = logging.getLogger(__name__)

def build_memory_context(memories: list[dict[str, str]]) -> str:
    """Build memory context."""
    memories = [m for m in memories if m.get("root_cause") and m.get("fix")]
    if not memories:
        return "No recalled memories were returned."
    entries = []
    for memory in memories[:5]:
        entries.append(
            "\n".join(
                (
                    f"Incident ID: {memory['id']}",
                    f"Date: {memory['date']}",
                    f"Service: {memory['service']}",
                    f"Root cause: {memory['root_cause']}",
                    f"Exact fix: {memory['fix']}",
                    f"MTTR: {memory['mttr']}",
                )
            )
        )
    return "\n\n---\n\n".join(entries)

def ask_groq(incident: str) -> str:
    """Ask groq."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are OnCall Memory, a practical incident-response assistant. "
                "Return 150 words or fewer using exactly this format:\n"
                "Diagnosis: one or two concise, generic lines based only on the supplied "
                "alert and logs\n"
                "Next 3 steps:\n- <short verification step>\n- <short mitigation step>\n"
                "- <short follow-up step>\n\n"
                "Do not refer to historical incidents, memory, or specific past fixes. "
                "Clearly label uncertainty and never claim an action was performed."
            ),
        },
        {"role": "user", "content": f"Incident alert and logs:\n{incident}"},
    ]
    return groq_completion(messages)

def compress_reflection(
    reflection: str, memories: list[dict[str, str]], feedback: list[dict[str, str]] | None = None
) -> str:
    """Build the memory-informed answer from reflection and recalled records."""
    complete_memories = [m for m in memories if m.get("root_cause") and m.get("fix")]
    context = build_memory_context(complete_memories)
    messages = [
        {
            "role": "system",
            "content": (
                "Answer the new alert by comparing symptoms and root cause against every "
                "recalled incident. Pick the single best match; use recall relevance order, "
                "and prefer an otherwise-equal incident with more Worked feedback. Return "
                "150 words or fewer, using exactly:\n"
                "Diagnosis: one or two lines\n"
                "Matched past incident: INC-xxx — <one line explaining symptom/root-cause match>\n"
                "Fix that worked before: <copy the recorded fix verbatim, preserving its numbers and identifiers>\n"
                "Next 3 steps:\n- <step>\n- <step>\n- <step>\n\n"
                "If none truly matches, use exactly: 'Matched past incident: No closely "
                "matching past incident. Closest: INC-xxx (why it differs).' In that case "
                "the fix line must say 'No proven fix for this alert.' Never present the "
                "closest incident's fix as proven. Use only recalled fields. Each next step "
                "based on history must end with its source ID in brackets, e.g. [INC-004]. "
                "Never use placeholders such as 'Relevant recalled history is available'. "
                "If feedback says a fix did not work, explicitly include 'Previously failed: <fix>' "
                "and do not describe that fix as proven or recommend it."
            ),
        },
        {
            "role": "user",
            "content": (
                f"HINDSIGHT REFLECT ANSWER:\n{reflection or 'No reflect answer returned.'}"
                f"\n\nRECALLED MEMORIES:\n{context}"
                f"\n\nRECALLED FEEDBACK:\n{chr(10).join(f['text'] for f in (feedback or [])) or 'No feedback records.'}"
            ),
        },
    ]
    return groq_completion(messages)


def answer_memory_question(question: str, memories: list[dict[str, str]], reflection: str) -> str:
    """Answer a memory question using only recalled records and cite incident IDs."""
    context = build_memory_context(memories)
    return groq_completion([
        {
            "role": "system",
            "content": (
                "Answer the user's question using only the recalled incident records and "
                "Hindsight reflection below. Cite every factual claim with one or more exact "
                "incident IDs from the records. Do not invent details. If the records do not "
                "answer the question, say that clearly and cite the closest recalled IDs."
            ),
        },
        {"role": "user", "content": f"QUESTION:\n{question}\n\nRECALLED INCIDENTS:\n{context}\n\nHINDSIGHT REFLECTION:\n{reflection or 'No reflection returned.'}"},
    ])


def detect_supersession(new_postmortem: str, memories: list[dict[str, str]], reflection: str) -> str:
    """Classify a resolved postmortem against recalled incidents as strict JSON."""
    context = build_memory_context(memories)
    return groq_completion([
        {"role": "system", "content": (
            "Compare the new resolved postmortem with the recalled incidents. Select at most "
            "one recalled old incident. Output JSON only with exactly these keys: old_id, "
            "relation, reason. relation must be supersedes, contradicts, or consistent. "
            "old_id must be copied from a recalled incident ID. If no incident is relevant, "
            "use old_id null and relation consistent. Do not infer that a fix is outdated "
            "unless the new postmortem supports that conclusion."
        )},
        {"role": "user", "content": f"NEW POSTMORTEM:\n{new_postmortem}\n\nRECALLED INCIDENTS:\n{context}\n\nHINDSIGHT REFLECTION:\n{reflection}"},
    ])

def no_memory_fallback(incident: str) -> str:
    """No memory fallback."""
    evidence = " ".join(incident.split())
    diagnosis = (
        f"Diagnosis: LLM unavailable; review this incident evidence: {evidence[:180]}"
        if evidence
        else "Diagnosis: LLM unavailable; cause cannot be determined from the provided incident."
    )
    return (
        f"{diagnosis}\n"
        "Next 3 steps:\n"
        "- Check current service health and confirm the alert is still active.\n"
        "- Inspect recent changes and the failing dependency's metrics/logs.\n"
        "- Apply the service runbook mitigation and verify recovery."
    )

def recalled_memory_fallback(memories: list[dict[str, str]]) -> str:
    """Recalled memory fallback."""
    if not memories:
        return (
            "Diagnosis: LLM unavailable; no recalled incident is available to guide diagnosis.\n"
            "Matched past incident: No closely matching past incident. Closest: none (no incidents were recalled).\n"
            "Fix that worked before: No proven fix for this alert.\n"
            "Next 3 steps:\n"
            "- Verify the alert and current service health.\n"
            "- Check recent changes and dependency metrics.\n"
            "- Follow the service runbook and monitor recovery."
        )

    usable_memories = [memory for memory in memories if memory.get("root_cause") and memory.get("fix")]
    if not usable_memories:
        return (
            "Diagnosis: Recalled incidents exist, but their root cause and fix are incomplete; "
            "the history cannot support a specific diagnosis.\n"
            "Matched past incident: Recalled records need complete supporting details\n"
            "Fix that worked before: Recalled records need complete supporting details\n"
            "Next 3 steps:\n"
            "- Verify the current alert and service health.\n"
            "- Check recent changes and dependency metrics.\n"
            "- Follow the service runbook and monitor recovery."
        )
    memory = usable_memories[0]
    return (
        f"Diagnosis: {memory['root_cause'] or 'A similar past incident may be relevant; verify against current evidence.'}\n"
        f"Matched past incident: {memory['id']} + {memory['date'] or 'date unavailable'}\n"
        f"Fix that worked before: {memory['fix'] or 'No exact fix was recorded.'}\n"
        "Next 3 steps:\n"
        "- Compare current symptoms with the recalled incident before applying its fix.\n"
        "- Verify the affected service and dependency health.\n"
        "- Apply the confirmed mitigation and monitor recovery."
    )
