import logging
import os
import time
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
BANK_ID = "oncall"
PRIMARY_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "qwen/qwen3-32b"


def get_hindsight_client() -> Hindsight:
    if not HINDSIGHT_URL:
        raise RuntimeError("Set HINDSIGHT_URL in your .env file to connect to Hindsight.")
    if HINDSIGHT_API_KEY:
        return Hindsight(base_url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY)
    return Hindsight(base_url=HINDSIGHT_URL)


def memory_text(value: Any) -> str:
    return str(getattr(value, "text", value)).strip()


def recall_incidents(client: Hindsight, incident: str) -> tuple[list[str], str]:
    recalled = client.recall(bank_id=BANK_ID, query=incident)
    memories = [
        memory_text(item)
        for item in getattr(recalled, "results", [])
        if memory_text(item)
    ]

    reflection = client.reflect(bank_id=BANK_ID, query=incident)
    reflection_text = memory_text(reflection)
    return memories, reflection_text


def ask_groq(incident: str, memory_context: str = "") -> str:
    if not GROQ_API_KEY:
        return "Groq is not configured. Add GROQ_API_KEY to your .env file and try again."

    messages = [
        {
            "role": "system",
            "content": (
                "You are OnCall Memory, a practical incident-response assistant. "
                "Analyze the supplied alert and logs. Give a concise likely diagnosis, "
                "evidence, safe verification steps, and a mitigation. Clearly label "
                "uncertainty; never claim an action was performed."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Incident alert and logs:\n{incident}\n\n"
                f"Relevant historical incident context:\n"
                f"{memory_context or 'No historical context provided.'}"
            ),
        },
    ]
    client = Groq(api_key=GROQ_API_KEY)
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=PRIMARY_MODEL,
                messages=messages,
                temperature=0.2,
            )
            answer = response.choices[0].message.content
            if not answer:
                raise RuntimeError(f"{PRIMARY_MODEL} returned an empty response.")
            return answer
        except Exception as exc:
            logger.warning("Primary Groq attempt %s failed: %s", attempt + 1, exc)
            if attempt == 0:
                time.sleep(0.5)

    try:
        response = client.chat.completions.create(
            model=FALLBACK_MODEL,
            messages=messages,
            temperature=0.2,
        )
        answer = response.choices[0].message.content
        if not answer:
            raise RuntimeError(f"{FALLBACK_MODEL} returned an empty response.")
        return answer
    except Exception as exc:
        logger.error("Fallback Groq model failed: %s", exc)

    return (
        "I couldn't generate a diagnosis after retrying the primary Groq model and "
        "trying the fallback model. Check your API key, model availability, and "
        "network connection, then retry."
    )


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

if st.button("Diagnose", type="primary"):
    if not incident.strip():
        st.warning("Paste an alert and log snippet before diagnosing.")
    else:
        st.session_state["current_incident"] = incident.strip()
        memories: list[str] = []
        memory_context = ""
        reflection_context = ""
        memory_error = ""

        if memory_enabled:
            try:
                with st.spinner("Recalling relevant incidents..."):
                    hindsight = get_hindsight_client()
                    memories, reflection_context = recall_incidents(
                        hindsight, incident.strip()
                    )
                memory_context = "\n\n".join(memories)
                if reflection_context:
                    memory_context = (
                        f"{memory_context}\n\nHindsight reflection:\n{reflection_context}"
                    ).strip()
            except Exception as exc:
                memory_error = str(exc)
                st.warning(f"Hindsight memory could not be loaded: {memory_error}")

        with st.spinner("Analyzing the incident..."):
            baseline = ask_groq(incident.strip())
            contextual = (
                ask_groq(incident.strip(), memory_context)
                if memory_enabled
                else None
            )

        st.session_state["diagnoses"] = {
            "baseline": baseline,
            "contextual": contextual,
            "memories": memories,
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
            if diagnoses["contextual"]:
                st.markdown(diagnoses["contextual"])
            else:
                st.info("Memory is enabled, but no memory-enriched diagnosis is available.")
        else:
            st.info("Memory is off. Turn it on and diagnose again to use past incidents.")

    with st.expander("Recalled memories"):
        if diagnoses["memories"]:
            for index, memory in enumerate(diagnoses["memories"], start=1):
                st.markdown(f"**{index}.** {memory}")
        elif diagnoses["memory_enabled"]:
            st.caption("No matching memories were returned.")
        else:
            st.caption("Memory was off; no recall was performed.")

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
        postmortem = (
            "Resolved incident postmortem\n"
            f"Incident alert and logs:\n{original_incident}\n"
            f"Root cause: {root_cause.strip()}\n"
            f"Fix applied: {fix_applied.strip()}"
        )
        try:
            get_hindsight_client().retain(bank_id=BANK_ID, content=postmortem)
        except Exception as exc:
            st.error(f"Could not save the postmortem to Hindsight: {exc}")
        else:
            st.success("Postmortem retained in Hindsight.")
