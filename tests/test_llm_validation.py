import json
import pytest
from app import llm

VALID = {
    "test_cases": [
        {"title": "Check battery estimate", "preconditions": ["Use the specified battery type"],
         "steps": ["Perform repeated measurements"], "expected_result": "Observed cycles align with the stated specification.",
         "source_node_ids": ["node-1"], "rationale": "Checks a stated battery-life requirement."},
        {"title": "Check low battery indicator", "preconditions": [],
         "steps": ["Lower simulated battery capacity below the documented threshold"],
         "expected_result": "The low-battery indicator appears at the documented threshold.",
         "source_node_ids": ["node-1"], "rationale": "Checks the documented indicator behavior."},
        {"title": "Check repeated use", "preconditions": [],
         "steps": ["Run the documented typical-use scenario"],
         "expected_result": "The system behavior is consistent with the supplied manual text.",
         "source_node_ids": ["node-1"], "rationale": "Checks the specified typical-use statement."}
    ]
}

def test_malformed_llm_json_retries_once_then_validates(monkeypatch):
    monkeypatch.setattr(llm, "LLM_API_KEY", "test-key")
    responses = iter(["not json", json.dumps(VALID)])
    calls = []
    def fake_request(messages):
        calls.append(messages)
        return next(responses)
    monkeypatch.setattr(llm, "_request", fake_request)
    result = llm.generate_test_cases([{"node_id": "node-1", "heading": "Battery", "version_number": 1, "text_snapshot": "Battery requirement."}])
    assert result["attempts"] == 2
    assert len(result["test_cases"]) == 3
    assert len(calls) == 2

def test_llm_cannot_cite_node_outside_selection(monkeypatch):
    monkeypatch.setattr(llm, "LLM_API_KEY", "test-key")
    invalid = json.loads(json.dumps(VALID))
    invalid["test_cases"][0]["source_node_ids"] = ["not-selected"]
    monkeypatch.setattr(llm, "_request", lambda messages: json.dumps(invalid))
    with pytest.raises(llm.LLMGenerationError, match="outside the submitted selection"):
        llm.generate_test_cases([{"node_id": "node-1", "heading": "Battery", "version_number": 1, "text_snapshot": "Battery requirement."}])
