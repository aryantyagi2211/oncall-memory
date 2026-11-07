import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import streamlit as st
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

    exact_fix = str(metadata.get("exact_fix") or "")
    if not exact_fix:
        match = re.search(r"(?:Exact )?Fix:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
        exact_fix = match.group(1).strip() if match else ""

    root_cause = str(metadata.get("root_cause") or "")
    if not root_cause:
        match = re.search(r"Root cause:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
        root_cause = match.group(1).strip() if match else ""

    mttr = str(metadata.get("mttr") or "")
    if not mttr:
        match = re.search(r"MTTR:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
        mttr = match.group(1).strip() if match else ""

    return {
        "id": incident_id,
        "date": incident_date,
        "service": service,
        "root_cause": root_cause,
        "fix": exact_fix,
        "mttr": mttr,
        "text": text,
    }


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
    client: Hindsight, incident: str
) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    recalled = client.recall(bank_id=BANK_ID, query=incident)
    facts = [
        recalled_memory(item)
        for item in memory_field(recalled, "results", [])
        if memory_text(item)
    ]

    reflection = client.reflect(bank_id=BANK_ID, query=incident)
    reflection_text = memory_text(reflection)
    return deduplicate_incidents(facts), facts, reflection_text


def build_memory_context(memories: list[dict[str, str]]) -> str:
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
    errors = []
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            return complete_with_model(client, model, messages)
        except Exception as exc:
            status, message = groq_error_details(exc)
            errors.append(f"{model} (status={status}): {message}")
            logger.error("Groq model %s exhausted: status=%s message=%s", model, status, message)
    raise GroqCompletionError("; ".join(errors))


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
    reflection: str, memories: list[dict[str, str]]
) -> str:
    context = build_memory_context(memories)
    messages = [
        {
            "role": "system",
            "content": (
                "Format Hindsight's reflection as a concise incident-response answer of "
                "150 words or fewer, using exactly:\n"
                "Diagnosis: one or two lines\n"
                "Matched past incident: <ID> + <date>, or No relevant history\n"
                "Fix that worked before: <exact fix with numbers>, or No relevant history\n"
                "Next 3 steps:\n- <step>\n- <step>\n- <step>\n\n"
                "Use ONLY the reflect answer and recalled memories supplied by the user. "
                "Do not infer facts from the current incident or outside knowledge. Cite "
                "only incident IDs, dates, and fixes present in those sources. If no recalled "
                "incident matches, say No relevant history in both history fields."
            ),
        },
        {
            "role": "user",
            "content": (
                f"HINDSIGHT REFLECT ANSWER:\n{reflection or 'No reflect answer returned.'}"
                f"\n\nRECALLED MEMORIES:\n{context}"
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
            "Matched past incident: No relevant history\n"
            "Fix that worked before: No relevant history\n"
            "Next 3 steps:\n"
            "- Verify the alert and current service health.\n"
            "- Check recent changes and dependency metrics.\n"
            "- Follow the service runbook and monitor recovery."
        )

    memory = memories[0]
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


def validate_memory_answer(answer: str, memories: list[dict[str, str]]) -> str:
    recalled_text = "\n".join(memory["text"] for memory in memories)
    allowed_ids = set(re.findall(r"\bINC-\d{3,}\b", recalled_text))
    allowed_ids.update(
        memory["id"]
        for memory in memories
        if re.fullmatch(r"INC-\d{3,}", memory["id"])
    )
    cited_ids = set(re.findall(r"\bINC-\d{3,}\b", answer))
    if cited_ids - allowed_ids:
        logger.warning("Memory answer cited an incident absent from recall results.")
        return (
            "The memory answer cited an incident ID that was not present in the "
            "recalled memories."
        )
    return answer


st.set_page_config(page_title="OnCall Memory", page_icon="🛠️", layout="wide")
st.title("OnCall Memory")
st.caption("Incident response that learns from resolved incidents.")

with st.sidebar:
    st.header("Memory")
    memory_enabled = st.toggle(
        "Memory ON/OFF",
        value=bool(HINDSIGHT_URL),
        help="When on, matching incident memories are recalled before diagnosis.",
    )
    if memory_enabled and not HINDSIGHT_URL:
        st.info("Set HINDSIGHT_URL in .env to enable incident recall.")

incident = st.text_area(
    "Paste a new incident (alert and log snippet)",
    height=180,
    placeholder="Alert: elevated 5xx on payments-api\nLogs: ...",
    key="incident_input",
)
if incident.strip():
    st.session_state["current_incident"] = incident.strip()

if st.button("Diagnose", type="primary"):
    if not incident.strip():
        st.warning("Paste an alert and log snippet before diagnosing.")
    else:
        st.session_state["current_incident"] = incident.strip()
        memories: list[dict[str, str]] = []
        raw_facts: list[dict[str, str]] = []
        reflection_context = ""
        memory_error = ""

        if memory_enabled:
            try:
                with st.spinner("Recalling relevant incidents..."):
                    hindsight = get_hindsight_client()
                    memories, raw_facts, reflection_context = recall_incidents(
                        hindsight, incident.strip()
                    )
            except Exception as exc:
                memory_error = str(exc)
                st.warning(f"Hindsight memory could not be loaded: {memory_error}")

        with st.spinner("Analyzing the incident..."):
            try:
                baseline = ask_groq(incident.strip())
            except GroqCompletionError as exc:
                logger.error("Without-memory diagnosis unavailable: %s", exc)
                baseline = no_memory_fallback(incident.strip())
                st.warning("Groq was unavailable; showing a complete generic diagnosis.")

            contextual = None
            if memory_enabled:
                try:
                    contextual = validate_memory_answer(
                        compress_reflection(reflection_context, memories),
                        raw_facts,
                    )
                except GroqCompletionError as exc:
                    logger.error("Memory diagnosis unavailable: %s", exc)
                    contextual = recalled_memory_fallback(memories)

        st.session_state["diagnoses"] = {
            "baseline": baseline,
            "contextual": contextual,
            "memories": memories,
            "raw_facts": raw_facts,
            "reflection": reflection_context,
            "memory_error": memory_error,
            "memory_enabled": memory_enabled,
        }

diagnoses = st.session_state.get("diagnoses")
if diagnoses:
    without_col, with_col = st.columns(2)
    with without_col:
        st.subheader("Without memory")
        st.markdown(diagnoses["baseline"])
    with with_col:
        st.subheader("With memory")
        if diagnoses["memory_enabled"]:
            match = re.search(
                r"(?im)^Matched past incident:\s*(INC-\d{3,})\b",
                diagnoses["contextual"] or "",
            )
            matched_memory = next(
                (
                    memory
                    for memory in diagnoses["memories"]
                    if match and memory["id"] == match.group(1)
                ),
                None,
            )
            if matched_memory:
                date_label = matched_memory["date"] or "date unavailable"
                mttr_label = recalled_mttr_label(
                    matched_memory["id"],
                    diagnoses["raw_facts"],
                    matched_memory["mttr"],
                )
                st.success(
                    f"**Memory match: {matched_memory['id']} ({date_label}) · "
                    f"MTTR {mttr_label}**"
                )
            if diagnoses["contextual"]:
                st.markdown(diagnoses["contextual"])
            else:
                st.info("Memory is enabled, but no memory-enriched diagnosis is available.")
        else:
            st.info("Memory is off. Turn it on and diagnose again to use past incidents.")

        if diagnoses["memory_error"]:
            st.caption("Memory could not be loaded; the result contains no recalled history.")
else:
    diagnoses = {
        "memories": [],
        "raw_facts": [],
        "memory_enabled": memory_enabled,
        "memory_error": "",
    }

with st.expander("Recalled memories", expanded=True):
    if diagnoses["memories"]:
        for memory in diagnoses["memories"][:5]:
            columns = st.columns([1, 1, 1.5, 5])
            columns[0].markdown(f"**{memory['id']}**")
            columns[1].write(memory["date"] or "Date unavailable")
            columns[2].write(memory["service"] or "Service unavailable")
            cause_fix = " · ".join(
                filter(None, (memory["root_cause"], memory["fix"]))
            )
            columns[3].write(cause_fix or "Root cause and fix unavailable")
    elif diagnoses["memory_enabled"] and diagnoses["memory_error"]:
        st.caption("Memory could not be loaded; no recalled records are available.")
    elif diagnoses["memory_enabled"]:
        st.caption("No matching memories were returned.")
    else:
        st.caption("Memory is off; no recall was performed.")

with st.expander("Raw recalled facts", expanded=False):
    if diagnoses["raw_facts"]:
        for fact in diagnoses["raw_facts"]:
            st.write(fact["text"])
    else:
        st.caption("No raw recalled facts are available.")

st.divider()
st.subheader("Mark resolved")
st.caption("Save a concise postmortem so future diagnoses can learn from it.")
with st.form("resolved_form"):
    root_cause = st.text_area("Root cause", placeholder="What caused the incident?")
    fix_applied = st.text_area("Fix applied", placeholder="What resolved it?")
    resolved = st.form_submit_button("Save postmortem")

if resolved:
    original_incident = st.session_state.get("current_incident", "").strip()
    if not original_incident:
        st.warning("Diagnose or paste an incident before saving its postmortem.")
    elif not root_cause.strip() or not fix_applied.strip():
        st.warning("Enter both the root cause and the fix applied.")
    else:
        incident_number = st.session_state.get("next_incident_number", 16)
        incident_id = f"INC-{incident_number:03d}"
        resolved_at = datetime.now(timezone.utc).replace(microsecond=0)
        incident_date = resolved_at.date().isoformat()
        resolved_timestamp = resolved_at.isoformat().replace("+00:00", "Z")
        postmortem = (
            f"Incident ID: {incident_id}\n"
            f"Date: {incident_date}\n"
            "Resolved incident postmortem\n"
            f"Incident alert and logs:\n{original_incident}\n"
            f"Root cause: {root_cause.strip()}\n"
            f"Exact fix: {fix_applied.strip()}"
        )
        try:
            get_hindsight_client().retain(
                bank_id=BANK_ID,
                content=postmortem,
                context="resolved production incident postmortem",
                timestamp=resolved_timestamp,
                metadata={
                    "incident_id": incident_id,
                    "date": incident_date,
                    "exact_fix": fix_applied.strip(),
                },
            )
        except Exception as exc:
            st.error(f"Could not save the postmortem to Hindsight: {exc}")
        else:
            st.session_state["next_incident_number"] = incident_number + 1
            st.success(f"Saved to memory as {incident_id}")
