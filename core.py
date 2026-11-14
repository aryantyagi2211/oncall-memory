import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any

from dotenv import load_dotenv
from groq import Groq
from hindsight_client import Hindsight

logger = logging.getLogger(__name__)
load_dotenv()
HINDSIGHT_URL = os.getenv("HINDSIGHT_URL", "").strip()
HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
BANK_ID = "oncall-v3"
PRIMARY_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "openai/gpt-oss-20b"
HINDSIGHT_TIMEOUT_SECONDS = float(os.getenv("HINDSIGHT_TIMEOUT", "15"))
GROQ_TOTAL_DEADLINE_SECONDS = 60
_executor = ThreadPoolExecutor(max_workers=4)

def get_hindsight_client() -> Hindsight:
    if not HINDSIGHT_URL:
        raise RuntimeError("Set HINDSIGHT_URL in your .env file to connect to Hindsight.")
    if HINDSIGHT_API_KEY:
        return Hindsight(base_url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY)
    return Hindsight(base_url=HINDSIGHT_URL)


def memory_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text", value)).strip()
    return str(getattr(value, "text", value)).strip()


def memory_field(value: Any, key: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def recalled_memory(item: Any) -> dict[str, str]:
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


def recall_incidents(
    client: Hindsight, incident: str, bank_id: str = BANK_ID
) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    recalled = client.recall(bank_id=bank_id, query=incident)
    facts = [
        recalled_memory(item)
        for item in memory_field(recalled, "results", [])
        if memory_text(item)
    ]

    reflection = client.reflect(bank_id=bank_id, query=incident)
    reflection_text = memory_text(reflection)
    return deduplicate_incidents(facts), facts, reflection_text


def build_memory_context(memories: list[dict[str, str]]) -> str:
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


def parse_feedback(item: Any) -> dict[str, str] | None:
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


class GroqCompletionError(RuntimeError):
    pass


def groq_error_details(exc: Exception) -> tuple[Any, str]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status, str(exc)


def is_groq_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


def is_reasoning_effort_rejection(exc: Exception) -> bool:
    status, message = groq_error_details(exc)
    text = message.lower()
    return status == 400 and "reasoning_effort" in text and any(
        phrase in text
        for phrase in ("unsupported", "not supported", "unknown", "unrecognized", "invalid")
    )


def is_transient_groq_error(exc: Exception) -> bool:
    status, _ = groq_error_details(exc)
    return status == 429 or is_groq_timeout(exc)


def complete_with_model(client: Groq, model: str, messages: list[dict[str, str]]) -> str:
    transient_retries = 0
    empty_retries = 0
    include_reasoning_effort = True

    while True:
        options: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": 2000,
        }
        if include_reasoning_effort:
            options["reasoning_effort"] = "low"
        try:
            response = client.chat.completions.create(**options)
        except Exception as exc:
            if include_reasoning_effort and is_reasoning_effort_rejection(exc):
                status, message = groq_error_details(exc)
                logger.warning(
                    "Groq model %s rejected reasoning_effort (status=%s): %s; retrying without it",
                    model,
                    status,
                    message,
                )
                include_reasoning_effort = False
                continue

            status, message = groq_error_details(exc)
            logger.error(
                "Groq model %s failed (status=%s): %s",
                model,
                status,
                message,
                exc_info=True,
            )
            if is_transient_groq_error(exc) and transient_retries < 3:
                transient_retries += 1
                time.sleep(2)
                continue
            raise

        answer = (
            response.choices[0].message.content
            if response.choices
            else None
        )
        if isinstance(answer, str) and answer.strip():
            return answer.strip()

        status = getattr(response, "status_code", None)
        logger.error(
            "Groq model %s returned empty or whitespace content (status=%s): %r",
            model,
            status,
            answer,
        )
        if empty_retries < 1:
            empty_retries += 1
            continue
        raise RuntimeError(f"Groq model {model} returned empty content twice.")


def groq_completion(messages: list[dict[str, str]]) -> str:
    if not GROQ_API_KEY:
        exc = GroqCompletionError("GROQ_API_KEY is not configured.")
        logger.error("Groq request failed (status=not available): %s", exc)
        raise exc

    try:
        client = Groq(api_key=GROQ_API_KEY)
    except Exception as exc:
        status, message = groq_error_details(exc)
        logger.error(
            "Groq client initialization failed (status=%s): %s",
            status,
            message,
            exc_info=True,
        )
        raise GroqCompletionError(f"Groq client initialization failed: {message}") from exc
    def complete_all() -> str:
        errors = []
        for model in (PRIMARY_MODEL, FALLBACK_MODEL):
            try:
                return complete_with_model(client, model, messages)
            except Exception as exc:
                status, message = groq_error_details(exc)
                errors.append(f"{model} (status={status}): {message}")
                logger.error("Groq model %s exhausted: status=%s message=%s", model, status, message)
        raise GroqCompletionError("; ".join(errors))

    future = _executor.submit(complete_all)
    try:
        return future.result(timeout=GROQ_TOTAL_DEADLINE_SECONDS)
    except FutureTimeout as exc:
        future.cancel()
        raise GroqCompletionError(
            f"Groq request exceeded the {GROQ_TOTAL_DEADLINE_SECONDS}s total deadline."
        ) from exc


def ask_groq(incident: str) -> str:
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
    complete_memories = [m for m in memories if m.get("root_cause") and m.get("fix")]
    context = build_memory_context(complete_memories)
    messages = [
        {
            "role": "system",
            "content": (
                "Answer the new alert by comparing symptoms and root cause against every "
                "recalled incident. Pick the single best match, not the first result. Return "
                "150 words or fewer, using exactly:\n"
                "Diagnosis: one or two lines\n"
                "Matched past incident: INC-xxx — <one line explaining symptom/root-cause match>\n"
                "Fix that worked before: <the exact fix text from that incident>\n"
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


def no_memory_fallback(incident: str) -> str:
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


def recalled_mttr_label(
    incident_id: str,
    raw_facts: list[dict[str, str]],
    fallback: str = "",
) -> str:
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


def _recall_with_timeout(client: Hindsight, incident: str, bank_id: str):
    def call():
        recalled = client.recall(bank_id=bank_id, query=incident)
        items = [item for item in memory_field(recalled, "results", []) if memory_text(item)]
        feedback = [record for item in items if (record := parse_feedback(item))]
        facts = [
            recalled_memory(item)
            for item in items
            if memory_text(item) and not parse_feedback(item)
        ]
        reflection = client.reflect(bank_id=bank_id, query=incident)
        return deduplicate_incidents(facts), facts, memory_text(reflection), feedback

    last_error = None
    for attempt in range(2):
        future = _executor.submit(call)
        try:
            return future.result(timeout=HINDSIGHT_TIMEOUT_SECONDS), ""
        except FutureTimeout:
            future.cancel()
            last_error = f"Hindsight recall/reflect timed out after {HINDSIGHT_TIMEOUT_SECONDS}s."
        except Exception as exc:
            last_error = f"Hindsight recall/reflect failed: {exc}"
        if attempt == 0:
            logger.warning("%s Retrying once.", last_error)
    return ([], [], "", []), last_error


def diagnose_without_memory(incident_text: str) -> str:
    try:
        return ask_groq(incident_text)
    except Exception as exc:
        logger.error("Without-memory diagnosis unavailable: %s", exc)
        return no_memory_fallback(incident_text)


def diagnose_with_memory(incident_text: str, bank_id: str | None = None) -> dict:
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


def reflect_now(incident_text: str, bank_id: str = BANK_ID) -> str:
    client = get_hindsight_client()
    last_error = "Hindsight reflect failed."
    for attempt in range(2):
        future = _executor.submit(client.reflect, bank_id=bank_id, query=incident_text)
        try:
            return memory_text(future.result(timeout=HINDSIGHT_TIMEOUT_SECONDS))
        except FutureTimeout:
            future.cancel()
            last_error = f"Hindsight reflect timed out after {HINDSIGHT_TIMEOUT_SECONDS}s."
        except Exception as exc:
            last_error = f"Hindsight reflect failed: {exc}"
        if attempt == 0:
            time.sleep(1)
    return last_error


