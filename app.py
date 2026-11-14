import logging
import json
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


from core import (BANK_ID, HINDSIGHT_URL, ask_memory, associate_feedback, associate_supersessions, compute_recurring_insights, diagnose_with_memory, diagnose_without_memory, get_hindsight_client, recalled_mttr_label, reflect_now, suggest_supersession)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
with open(os.path.join(DATA_DIR, "test_incidents.json"), encoding="utf-8") as test_file:
    EXAMPLE_CASES = json.load(test_file)
EXAMPLES = {
    "TEST-02": next(case for case in EXAMPLE_CASES if case["id"] == "TEST-02"),
    "TEST-04": next(case for case in EXAMPLE_CASES if case["id"] == "TEST-04"),
    "Novel incident (no history)": next(case for case in EXAMPLE_CASES if not case["relevant_incident_ids"]),
}


def timed_call(function, *args):
    started = time.perf_counter()
    result = function(*args)
    return result, time.perf_counter() - started

st.set_page_config(page_title="OnCall Memory", page_icon="🛠️", layout="wide")
st.title("OnCall Memory")
st.caption("Incident response that learns from resolved incidents.")

with st.expander("How to try it", expanded=False):
    st.markdown("1. Pick an example or paste an alert.\n2. Click **Diagnose**.\n3. Compare the Memory OFF and Memory ON answers.")

with st.sidebar:
    st.header("Memory")
    memory_enabled = st.toggle(
        "Memory ON/OFF",
        value=bool(HINDSIGHT_URL),
        help="When on, matching incident memories are recalled before diagnosis.",
    )
    if memory_enabled and not HINDSIGHT_URL:
        st.info("Set HINDSIGHT_URL in .env to enable incident recall.")

example_choice = st.selectbox(
    "Try an example",
    ["Choose an example...", "TEST-02", "TEST-04", "Novel incident (no history)"],
)
if example_choice != st.session_state.get("last_example_choice"):
    if example_choice in EXAMPLES:
        st.session_state["incident_input"] = EXAMPLES[example_choice]["alert_log"]
    st.session_state["last_example_choice"] = example_choice

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
            baseline_future = pool.submit(timed_call, diagnose_without_memory, alert_text) if need_baseline else None
            memory_future = pool.submit(timed_call, diagnose_with_memory, alert_text) if need_memory else None
            with st.status("Analyzing incident...", expanded=True) as status:
                status.write("Recalling past incidents..." if need_memory else "Using cached memory answer..." if memory_enabled else "Memory is off.")
                if memory_future:
                    memory_result, memory_elapsed = memory_future.result()
                    status.write("Reflecting...")
                    status.write("Writing answer...")
                    cached["memory"] = memory_result
                    cached["memory_elapsed"] = memory_elapsed
                if baseline_future:
                    cached["baseline"], cached["baseline_elapsed"] = baseline_future.result()
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
            "baseline_elapsed": cached.get("baseline_elapsed"),
            "memory_elapsed": cached.get("memory_elapsed"),
        }

diagnoses = st.session_state.get("diagnoses")
if diagnoses:
    without_col, with_col = st.columns(2)
    with without_col:
        st.subheader("Without memory")
        st.markdown(diagnoses["baseline"])
        if diagnoses.get("baseline_elapsed") is not None:
            st.caption(f"Elapsed: {diagnoses['baseline_elapsed']:.2f}s")
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
            associated_feedback = associate_feedback(diagnoses["memories"], diagnoses.get("feedback", []))
            demoted = [record for record in associated_feedback if record.get("outcome") == "did not work"]
            if valid_citation and any(record.get("outcome") == "worked" and cited_id in record.get("matched_incident_ids", []) for record in associated_feedback):
                confidence = {"Low": "Medium", "Medium": "High", "High": "High"}[confidence]
            with st.container(border=True):
                st.markdown(f"**Best match · {confidence} confidence**")
                if valid_citation:
                    st.markdown(f"**{matched_memory['id']}** · {matched_memory['service'] or 'Service unavailable'}")
                    st.write(f"Root cause: {matched_memory['root_cause']}")
                    st.write(f"Fix: {matched_memory['fix']}")
                    worked_count = sum(
                        1 for record in associated_feedback
                        if record.get("outcome") == "worked"
                        and cited_id in record.get("matched_incident_ids", [])
                    )
                    if worked_count:
                        st.caption(f"Confirmed to work {worked_count} times")
                    reason = re.search(r"(?im)^Matched past incident:\s*INC-\d{3,}\s*[—-]\s*(.+)$", diagnoses["contextual"] or "")
                    if reason:
                        st.caption(reason.group(1).strip())
                else:
                    st.write("No valid incident citation or close match was found.")
            supersessions = associate_supersessions(diagnoses["memories"], diagnoses.get("feedback", []))
            for record in supersessions:
                st.warning(f"Outdated: {record['old_id']} superseded by {record['new_id']} ({record['reason']})")
            if demoted:
                st.markdown("**Previously failed**")
                for record in demoted:
                    for incident_id in record.get("matched_incident_ids", []):
                        st.caption(f"{incident_id} · {record.get('fix') or 'fix unavailable'} · {record.get('date', 'date unavailable')}")
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
        if diagnoses.get("memory_elapsed") is not None:
            st.caption(f"Elapsed: {diagnoses['memory_elapsed']:.2f}s")
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
                suggested_fix = suggested.group(1).strip() if suggested else "No specific fix was suggested."
                cited_incident = cited.group(0) if cited else "none"
                feedback_timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                feedback_text = (
                    f"Feedback ID: {feedback_id}\n"
                    f"New alert summary: {' '.join(st.session_state.get('current_incident', '').split())[:500]}\n"
                    f"Suggested fix: {suggested_fix}\n"
                    f"Cited incident: {cited_incident}\n"
                    f"Outcome: {outcome}\n"
                    f"Note: {note.strip() or 'none'}"
                )
                try:
                    get_hindsight_client().retain(
                        bank_id=BANK_ID, content=feedback_text,
                        context="feedback on incident-response recommendation",
                        timestamp=feedback_timestamp,
                        document_id=feedback_id,
                        metadata={"record_type": "feedback", "feedback_id": feedback_id,
                                  "outcome": outcome, "incident_id": cited_incident,
                                  "suggested_fix": suggested_fix, "note": note.strip() or "none",
                                  "timestamp": feedback_timestamp},
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

st.subheader("Memory Insights")
insights = compute_recurring_insights(diagnoses.get("memories", []))
if insights:
    count = insights["service_count"]
    suffix = "th" if 10 <= count % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(count % 10, "th")
    avg = insights["average_mttr_minutes"]
    avg_label = f"{avg:.1f} min across {insights['mttr_sample_count']} records" if avg is not None else "not recorded"
    with st.container(border=True):
        st.markdown(f"**{count}{suffix} incident on {insights['service']} in memory**")
        st.write(f"Most common cause: {insights['most_common_root_cause']}")
        st.write(f"Average MTTR: {avg_label}")
        st.caption(f"Counts are among {insights['recalled_count']} recalled incidents; statistics are computed from recalled fields.")
elif diagnoses.get("memory_error"):
    st.info(f"Recurring insights are unavailable: {diagnoses['memory_error']}")
else:
    st.caption("No recalled incident fields are available for recurring insights yet.")

memory_question = st.text_input("Ask the Memory", key="memory_question", placeholder="What fixes worked for Redis connection issues?")
if st.button("Ask", key="ask_memory"):
    if not memory_question.strip():
        st.info("Enter a question about past incidents.")
    else:
        with st.spinner("Recalling and reflecting on incident history..."):
            memory_answer = ask_memory(memory_question.strip(), bank_id=BANK_ID)
        st.write(memory_answer)

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
            st.session_state.pop("pending_supersession", None)
            try:
                suggestion = suggest_supersession(postmortem, bank_id=BANK_ID)
            except Exception as exc:
                st.warning(f"Could not check for superseded incidents: {exc}")
            else:
                if suggestion:
                    st.session_state["pending_supersession"] = {
                        **suggestion,
                        "new_id": incident_id,
                        "alert": original_incident,
                    }

pending = st.session_state.get("pending_supersession")
if pending:
    st.info(f"Possible {pending['relation']} relationship: {pending['old_id']} may be outdated. {pending['reason']}")
    confirm_col, dismiss_col = st.columns(2)
    if confirm_col.button("Confirm outdated", key="confirm_supersession"):
        record_id = f"supersede-{pending['old_id']}-{pending['new_id']}"
        record_date = datetime.now(timezone.utc).date().isoformat()
        record_text = (
            "Record type: supersession\n"
            f"Old incident: {pending['old_id']}\n"
            f"New incident: {pending['new_id']}\n"
            f"Relation: {pending['relation']}\n"
            f"Reason: {pending['reason']}\n"
            f"Alert/error log: {pending.get('alert', '')}\n"
            f"Date: {record_date}"
        )
        try:
            get_hindsight_client().retain(
                bank_id=BANK_ID, content=record_text,
                context="confirmed incident supersession record",
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                document_id=record_id,
                metadata={"record_type": "supersession", "old_id": pending["old_id"],
                          "new_id": pending["new_id"], "relation": pending["relation"],
                          "reason": pending["reason"], "date": record_date,
                          "alert": pending.get("alert", "")},
            )
        except Exception as exc:
            st.error(f"Could not retain supersession record: {exc}")
        else:
            st.session_state.pop("pending_supersession", None)
            st.success(f"Marked {pending['old_id']} as outdated by {pending['new_id']}.")
    if dismiss_col.button("Dismiss", key="dismiss_supersession"):
        st.session_state.pop("pending_supersession", None)
