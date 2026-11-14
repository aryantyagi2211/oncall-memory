import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import oncall_memory.diagnose as diagnose_module


def test_extraction_prefers_metadata():
    parsed = core.recalled_memory({
        "text": "Incident ID: INC-101\nService: Text service\nRoot cause: Text cause\nFix applied: Text fix",
        "metadata": {"incident_id": "INC-102", "service": "Metadata service", "root_cause": "Metadata cause", "fix": "Metadata fix"},
    })
    assert parsed["id"] == "INC-102"
    assert parsed["service"] == "Metadata service"
    assert parsed["root_cause"] == "Metadata cause"
    assert parsed["fix"] == "Metadata fix"


def test_extraction_falls_back_to_template_text():
    parsed = core.recalled_memory({"text": "Incident ID: INC-111\nService: payments\nDate: 2026-01-02\nRoot cause: pool exhausted\nFix applied: raise pool limit\nMTTR: 12 minutes"})
    assert parsed["id"] == "INC-111"
    assert parsed["root_cause"] == "pool exhausted"
    assert parsed["fix"] == "raise pool limit"


def test_validator_rejects_id_not_recalled():
    memories = [{"id": "INC-001", "root_cause": "connection pool full exhausted", "fix": "cap pool", "text": ""}]
    answer = "Matched past incident: INC-999 — similar\nFix that worked before: cap pool"
    checked = core.validate_memory_answer(answer, memories, "connection pool full exhausted")
    assert "INC-999" not in checked
    assert "INC-001" in checked


def test_validator_rejects_fix_not_from_cited_incident():
    memories = [{"id": "INC-001", "root_cause": "connection pool full exhausted", "fix": "cap pool", "text": ""}]
    answer = "Matched past incident: INC-001 — same pool issue\nFix that worked before: restart everything"
    checked = core.validate_memory_answer(answer, memories, "connection pool full exhausted")
    assert "Fix that worked before: cap pool" in checked


def test_empty_recall_never_claims_relevant_history(monkeypatch):
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: (([], [], "", []), ""))
    monkeypatch.setattr(diagnose_module, "compress_reflection", lambda *args: "Matched past incident: No relevant history\nFix that worked before: No relevant history")
    result = core.diagnose_with_memory("new alert")
    assert "no relevant history" not in result["answer"].lower()
    assert "No closely matching past incident" in result["answer"]


def _memory(incident_id, fix):
    return {"id": incident_id, "date": "2026-11-01", "service": "payments", "root_cause": "pool exhaustion", "fix": fix, "mttr": "10 minutes", "text": ""}


def test_failed_fix_is_demoted_and_next_incident_chosen(monkeypatch):
    memories = [_memory("INC-101", "restart Redis"), _memory("INC-102", "cap connection pool")]
    feedback = [{"outcome": "did not work", "fix": "restart Redis", "incident_id": "INC-101", "date": "2026-11-12", "note": "No recovery"}]
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: ((memories, [], "", feedback), ""))
    def format_answer(reflection, candidates, feedback_records):
        assert [m["id"] for m in candidates] == ["INC-102"]
        return "Matched past incident: INC-102 — pool symptoms match\nFix that worked before: cap connection pool"
    monkeypatch.setattr(diagnose_module, "compress_reflection", format_answer)
    result = core.diagnose_with_memory("pool exhausted")
    assert "Matched past incident: INC-102" in result["answer"]
    assert "INC-101 fix (restart Redis) was marked 'Didn't work'" in result["answer"]
    assert "Trying INC-102 instead" in result["answer"]


def test_all_failed_candidates_do_not_invent_a_fix(monkeypatch):
    memories = [_memory("INC-101", "restart Redis")]
    feedback = [{"outcome": "did not work", "fix": "restart Redis", "incident_id": "INC-101", "date": "2026-11-12", "note": "No recovery"}]
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: ((memories, [], "", feedback), ""))
    result = core.diagnose_with_memory("pool exhausted")
    assert "No proven fix for this alert" in result["answer"]
    assert "No remaining recalled incident is available to try" in result["answer"]
    assert "Fix that worked before: restart Redis" not in result["answer"]


def test_no_failure_feedback_means_no_failure_notice(monkeypatch):
    memories = [_memory("INC-102", "cap connection pool")]
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: ((memories, [], "", []), ""))
    monkeypatch.setattr(diagnose_module, "compress_reflection", lambda *args: "Matched past incident: INC-102 — pool symptoms match\nFix that worked before: cap connection pool")
    result = core.diagnose_with_memory("pool exhausted")
    assert "was marked 'Didn't work'" not in result["answer"]


def test_worked_feedback_breaks_equal_recall_order_tie(monkeypatch):
    memories = [
        {**_memory("INC-201", "increase timeout"), "recall_order": 0},
        {**_memory("INC-202", "cap connection pool"), "recall_order": 0},
    ]
    feedback = [{"outcome": "worked", "fix": "cap connection pool", "incident_id": "INC-202", "date": "2026-11-12", "note": "Recovered"}]
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: ((memories, [], "", feedback), ""))
    monkeypatch.setattr(
        diagnose_module,
        "compress_reflection",
        lambda reflection, candidates, records: (
            f"Matched past incident: {candidates[0]['id']} — selected from equally relevant recall\n"
            f"Fix that worked before: {candidates[0]['fix']}"
        ),
    )
    result = core.diagnose_with_memory("connection pool")
    assert result["memories"][0]["id"] == "INC-202"
    assert "Matched past incident: INC-202" in result["answer"]


def test_recurring_insights_counts_metadata_correctly():
    memories = [
        {"id": "INC-1", "service": "payments", "root_cause": "pool exhausted", "mttr": "10 minutes"},
        {"id": "INC-2", "service": "payments", "root_cause": "pool exhausted", "mttr": "20 minutes"},
        {"id": "INC-3", "service": "auth", "root_cause": "stale key", "mttr": "40 minutes"},
    ]
    insights = core.compute_recurring_insights(memories)
    assert insights["service"] == "payments"
    assert insights["service_count"] == 2
    assert insights["recalled_count"] == 3
    assert insights["most_common_root_cause"] == "pool exhausted"
    assert insights["average_mttr_minutes"] == 15


def test_ask_memory_rejects_invented_incident_id(monkeypatch):
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args, **kwargs: (([_memory("INC-301", "cap pool")], [], "", []), ""))
    monkeypatch.setattr(diagnose_module, "answer_memory_question", lambda *args: "INC-999 shows the fix worked.")
    answer = core.ask_memory("What fixed the issue?")
    assert "can't support" in answer
    assert "INC-999" not in answer


def test_ask_memory_empty_recall_says_no_history(monkeypatch):
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args, **kwargs: (([], [], "", []), ""))
    answer = core.ask_memory("What fixed this issue?")
    assert answer == "No relevant history was recalled for this question."
