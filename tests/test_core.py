import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core


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
    monkeypatch.setattr(core, "get_hindsight_client", lambda: object())
    monkeypatch.setattr(core, "_recall_with_timeout", lambda *args: (([], [], "", []), ""))
    monkeypatch.setattr(core, "compress_reflection", lambda *args: "Matched past incident: No relevant history\nFix that worked before: No relevant history")
    result = core.diagnose_with_memory("new alert")
    assert "no relevant history" not in result["answer"].lower()
    assert "No closely matching past incident" in result["answer"]
