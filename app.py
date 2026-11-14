import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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


from core import (BANK_ID, HINDSIGHT_URL, diagnose_with_memory, diagnose_without_memory, get_hindsight_client, recalled_mttr_label, reflect_now)

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
        alert_text = incident.strip()
        cached = st.session_state.setdefault("diagnosis_cache", {}).setdefault(alert_text, {})
        need_baseline = "baseline" not in cached
        need_memory = memory_enabled and "memory" not in cached
        with ThreadPoolExecutor(max_workers=2) as pool:
            baseline_future = pool.submit(diagnose_without_memory, alert_text) if need_baseline else None
            memory_future = pool.submit(diagnose_with_memory, alert_text) if need_memory else None
            with st.status("Analyzing incident...", expanded=True) as status:
                status.write("Recalling past incidents..." if need_memory else "Using cached memory answer..." if memory_enabled else "Memory is off.")
                if memory_future:
                    memory_result = memory_future.result()
                    status.write("Reflecting...")
                    status.write("Writing answer...")
                    cached["memory"] = memory_result
                if baseline_future:
                    cached["baseline"] = baseline_future.result()
                status.update(label="Diagnosis ready", state="complete", expanded=False)
        if need_memory and cached["memory"]["error"]:
            st.warning(f"Hindsight memory could not be loaded: {cached['memory']['error']}")
        memory_result = cached.get("memory", {
            "answer": None, "recalled_incidents": [], "reflection": "", "error": "",
            "memories": [], "raw_facts": [], "feedback": [],
        })
        st.session_state["diagnoses"] = {
            "baseline": cached.get("baseline", ""),
            "contextual": memory_result["answer"],
            "memories": memory_result["memories"],
            "raw_facts": memory_result["raw_facts"],
            "feedback": memory_result.get("feedback", []),
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
            cited_id = match.group(1) if match else None
            valid_citation = bool(
                matched_memory and matched_memory.get("root_cause") and matched_memory.get("fix")
                and not re.search(r"(?i)no closely matching past incident", diagnoses["contextual"] or "")
            )
            is_top = bool(valid_citation and diagnoses["memories"] and diagnoses["memories"][0]["id"] == cited_id)
            confidence = "High" if is_top else "Medium" if valid_citation else "Low"
            with st.container(border=True):
                st.markdown(f"**Best match · {confidence} confidence**")
                if valid_citation:
                    st.markdown(f"**{matched_memory['id']}** · {matched_memory['service'] or 'Service unavailable'}")
                    st.write(f"Root cause: {matched_memory['root_cause']}")
                    st.write(f"Fix: {matched_memory['fix']}")
                else:
                    st.write("No valid incident citation or close match was found.")
                st.caption("Confidence is a heuristic based on citation validation and recall order.")
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
        if diagnoses["memory_enabled"] and diagnoses["contextual"]:
            note = st.text_input("Optional feedback note", key="feedback_note")
            worked_col, failed_col = st.columns(2)
            outcome = "worked" if worked_col.button("Worked", key="feedback_worked") else None
            if failed_col.button("Didn't work", key="feedback_failed"):
                outcome = "did not work"
            if outcome:
                suggested = re.search(r"(?im)^Fix that worked before:\s*(.+)$", diagnoses["contextual"])
                cited = re.search(r"\bINC-\d{3,}\b", diagnoses["contextual"])
                feedback_id = f"feedback-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
                feedback_text = (
                    f"Feedback ID: {feedback_id}\n"
                    f"New alert summary: {' '.join(st.session_state.get('current_incident', '').split())[:500]}\n"
                    f"Suggested fix: {suggested.group(1).strip() if suggested else 'No specific fix was suggested.'}\n"
                    f"Cited incident: {cited.group(0) if cited else 'none'}\n"
                    f"Outcome: {outcome}\n"
                    f"Note: {note.strip() or 'none'}"
                )
                try:
                    get_hindsight_client().retain(
                        bank_id=BANK_ID, content=feedback_text,
                        context="feedback on incident-response recommendation",
                        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        document_id=feedback_id,
                        metadata={"record_type": "feedback", "outcome": outcome, "incident_id": cited.group(0) if cited else "none"},
                    )
                except Exception as exc:
                    st.error(f"Could not save feedback: {exc}")
                else:
                    st.caption("Feedback saved.")
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

with st.expander("What the agent learned", expanded=False):
    if st.button("Reflect now", key="reflect_now"):
        reflect_query = st.session_state.get("current_incident", "").strip()
        if not reflect_query:
            st.info("Paste or diagnose an incident before asking for reflection.")
        else:
            try:
                with st.spinner("Asking Hindsight to reflect..."):
                    reflection_result = reflect_now(reflect_query, bank_id=BANK_ID)
                st.write(reflection_result or "Hindsight returned no reflection.")
            except Exception as exc:
                st.error(f"Hindsight reflection failed: {exc}")

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
        resolved_at = datetime.now(timezone.utc)
        incident_date = resolved_at.date().isoformat()
        resolved_timestamp = resolved_at.isoformat().replace("+00:00", "Z")
        incident_id = f"INC-{resolved_at.strftime('%Y%m%d%H%M%S%f')}{uuid.uuid4().int % 1000000:06d}"
        postmortem = (
            f"Incident ID: {incident_id}\n"
            f"Service: Unknown\n"
            "Severity: Unknown\n"
            f"Date: {incident_date}\n"
            f"Alert/error log: {original_incident}\n"
            f"Root cause: {root_cause.strip()}\n"
            f"Fix applied: {fix_applied.strip()}\n"
            "MTTR: Unknown\n"
            "Runbook: Unknown"
        )
        try:
            get_hindsight_client().retain(
                bank_id=BANK_ID,
                content=postmortem,
                context="resolved production incident postmortem",
                timestamp=resolved_timestamp,
                document_id=incident_id,
                metadata={
                    "incident_id": incident_id,
                    "date": incident_date,
                    "service": "Unknown",
                    "severity": "Unknown",
                    "root_cause": root_cause.strip(),
                    "fix": fix_applied.strip(),
                    "exact_fix": fix_applied.strip(),
                    "root_cause_category": "unknown",
                    "mttr_minutes": "Unknown",
                },
            )
        except Exception as exc:
            st.error(f"Could not save the postmortem to Hindsight: {exc}")
        else:
            st.success(f"Saved to memory as {incident_id}")
