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


from core import (BANK_ID, HINDSIGHT_URL, diagnose_with_memory, diagnose_without_memory, get_hindsight_client, recalled_mttr_label)

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
        with st.spinner("Analyzing the incident..."):
            baseline = diagnose_without_memory(incident.strip())
            memory_result = diagnose_with_memory(incident.strip()) if memory_enabled else {
                "answer": None, "recalled_incidents": [], "reflection": "", "error": "",
                "memories": [], "raw_facts": [],
            }
        if memory_result["error"]:
            st.warning(f"Hindsight memory could not be loaded: {memory_result['error']}")
        st.session_state["diagnoses"] = {
            "baseline": baseline,
            "contextual": memory_result["answer"],
            "memories": memory_result["memories"],
            "raw_facts": memory_result["raw_facts"],
            "reflection": memory_result["reflection"],
            "memory_error": memory_result["error"],
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
