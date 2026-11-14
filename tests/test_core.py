import sys
import inspect
import logging
from pathlib import Path
from hindsight_client import Hindsight

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import oncall_memory.diagnose as diagnose_module
import oncall_memory.memory as memory_module


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


def test_superseded_incident_is_demoted_and_newer_chosen(monkeypatch):
    old = _memory("INC-401", "increase Redis timeout")
    new = _memory("INC-402", "move to healthy primary")
    record = {"record_type": "supersession", "old_id": "INC-401", "new_id": "INC-402",
              "relation": "contradicts", "reason": "The old primary was retired."}
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: (([old, new], [], "", [record]), ""))
    monkeypatch.setattr(diagnose_module, "compress_reflection", lambda _r, candidates, _f:
                        f"Matched past incident: {candidates[0]['id']} — current fix\nFix that worked before: {candidates[0]['fix']}")
    result = core.diagnose_with_memory("Redis primary down")
    assert result["memories"][0]["id"] == "INC-402"
    assert "Matched past incident: INC-402" in result["answer"]
    assert "Outdated: INC-401 was superseded by INC-402" in result["answer"]


def test_metadata_supersession_fragment_demotes_old_and_formats_badge(monkeypatch):
    supersession_fragment = {
        "text": "Record type: supersession\nOld incident: INC-499\nNew incident: INC-498\nRelation: supersedes\nReason: text fragment",
        "metadata": {
            "record_type": "supersession", "old_id": "INC-411", "new_id": "INC-412",
            "relation": "contradicts", "reason": "The resolver was healthy; an ACL blocked the new primary.",
        },
    }
    old = _memory("INC-411", "lower DNS TTL")
    new = _memory("INC-412", "allow active-primary traffic")
    def recalled_fact(memory):
        return {
            "text": (f"Incident ID: {memory['id']}\nService: {memory['service']}\nDate: {memory['date']}\n"
                     f"Root cause: {memory['root_cause']}\nFix applied: {memory['fix']}\nMTTR: {memory['mttr']}"),
            "metadata": {"incident_id": memory["id"], "service": memory["service"],
                         "date": memory["date"], "root_cause": memory["root_cause"],
                         "fix": memory["fix"], "mttr": memory["mttr"]},
        }

    class RecallClient:
        def recall(self, **_kwargs):
            return {"results": [recalled_fact(old), recalled_fact(new), supersession_fragment]}

        def reflect(self, **_kwargs):
            return ""

    (recalled_memories, _raw_facts, _reflection, feedback), error = memory_module._recall_with_timeout(
        RecallClient(), "auth requests fail after Redis promotion", "test-bank"
    )
    assert not error
    old, new = recalled_memories
    record = next(item for item in feedback if item.get("record_type") == "supersession")
    assert record["old_id"] == "INC-411"  # metadata takes precedence over fragment text
    assert record["new_id"] == "INC-412"
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: (([old, new], [], "", [record]), ""))
    monkeypatch.setattr(
        diagnose_module,
        "compress_reflection",
        lambda _reflection, candidates, _records: (
            f"Matched past incident: {candidates[0]['id']} — the corrected diagnosis matches\n"
            f"Fix that worked before: {candidates[0]['fix']}"
        ),
    )
    result = core.diagnose_with_memory("auth requests fail after Redis promotion")
    associated = core.associate_supersessions(result["memories"], result["feedback"])
    assert result["memories"][0]["id"] == "INC-412"
    assert "Outdated: INC-411 was superseded by INC-412" in result["answer"]
    assert core.supersession_badge_text(associated[0]).startswith("Outdated: superseded by INC-412")


def test_no_outdated_mention_without_recalled_record(monkeypatch):
    memory = _memory("INC-403", "restart service")
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: (([memory], [], "", []), ""))
    monkeypatch.setattr(diagnose_module, "compress_reflection", lambda *args:
                        "Matched past incident: INC-403 — similar\nFix that worked before: restart service")
    result = core.diagnose_with_memory("service unavailable")
    assert "outdated:" not in result["answer"].lower()
    assert "superseded by" not in result["answer"].lower()


def test_invalid_supersession_old_id_is_rejected():
    suggestion = '{"old_id":"INC-999","relation":"contradicts","reason":"Different fix."}'
    assert core.validate_supersession_suggestion(suggestion, [_memory("INC-404", "valid fix")]) is None


def test_feedback_metadata_fragment_demotes_cited_incident(monkeypatch):
    memories = [_memory("INC-501", "restart Redis"), _memory("INC-502", "route to healthy primary")]
    feedback_fragment = {
        "text": "Outcome: did not work",
        "metadata": {
            "record_type": "feedback",
            "feedback_id": "feedback-proof-1",
            "outcome": "did not work",
            "incident_id": "INC-501",
            "suggested_fix": "restart Redis",
        },
    }
    parsed_feedback = core.parse_feedback(feedback_fragment)
    assert parsed_feedback["incident_id"] == "INC-501"
    assert parsed_feedback["fix"] == "restart Redis"
    monkeypatch.setattr(diagnose_module, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(diagnose_module, "_recall_with_timeout", lambda *args: ((memories, [], "", [parsed_feedback]), ""))

    def answer_from_candidates(_reflection, candidates, _feedback):
        assert [memory["id"] for memory in candidates] == ["INC-502"]
        return "Matched past incident: INC-502 — same failover symptoms\nFix that worked before: route to healthy primary"

    monkeypatch.setattr(diagnose_module, "compress_reflection", answer_from_candidates)
    result = core.diagnose_with_memory("Redis failover connections refused")
    assert result["memories"][0]["id"] == "INC-502"
    assert "Trying INC-502 instead" in result["answer"]


def test_partial_recall_logs_one_summary_with_parse_details(caplog):
    fragments = [
        {"text": "Incident ID: INC-601\nService: auth"},
        {"text": "Incident ID: INC-602\nRoot cause: incomplete"},
    ]
    with caplog.at_level(logging.WARNING, logger="oncall_memory.memory"):
        memory_module._parse_incident_fragments(fragments)
    warnings = [record.message for record in caplog.records if record.name == "oncall_memory.memory"]
    assert len(warnings) == 1
    assert "2 fragments" in warnings[0]
    assert "2 partial incidents" in warnings[0]
    assert "INC-601 missing" in warnings[0]
    assert "INC-602 missing" in warnings[0]


def test_hindsight_call_keywords_match_installed_client_signatures():
    calls = {"recall": [], "reflect": []}

    class CapturingClient:
        def recall(self, **kwargs):
            calls["recall"].append(kwargs)
            return {"results": []}

        def reflect(self, **kwargs):
            calls["reflect"].append(kwargs)
            return ""

    memory_module._recall_with_timeout(CapturingClient(), "test alert", "test-bank", limit=20)

    recall_parameters = set(inspect.signature(Hindsight.recall).parameters) - {"self"}
    reflect_parameters = set(inspect.signature(Hindsight.reflect).parameters) - {"self"}
    assert calls["recall"] and calls["reflect"]
    assert all(set(kwargs) <= recall_parameters for kwargs in calls["recall"])
    assert all(set(kwargs) <= reflect_parameters for kwargs in calls["reflect"])
