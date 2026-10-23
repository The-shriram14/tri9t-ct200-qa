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


def test_llm_request_sends_user_agent_and_auth_header(monkeypatch):
    captured_req = []

    class DummyResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    def fake_urlopen(req, timeout=45):
        captured_req.append(req)
        return DummyResponse()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm, "LLM_API_KEY", "gsk_secretkey12345")
    llm._request([{"role": "user", "content": "hi"}])
    assert len(captured_req) == 1
    req = captured_req[0]
    assert req.get_header("User-agent") == "Tri9T-CT200-QA/1.0"
    assert req.get_header("Authorization") == "Bearer gsk_secretkey12345"


def test_llm_request_http_error_redacts_secrets_and_reports_body(monkeypatch):
    import io
    secret_key = "gsk_super_secret_key_12345"
    monkeypatch.setattr(llm, "LLM_API_KEY", secret_key)
    error_body = f'{{"error": {{"message": "Access forbidden for key {secret_key}", "code": 403}}}}'.encode("utf-8")
    http_error = llm.urllib.error.HTTPError(
        url="https://api.groq.com/openai/v1/chat/completions",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=io.BytesIO(error_body)
    )

    def fake_urlopen(req, timeout=45):
        raise http_error

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm.LLMGenerationError) as exc_info:
        llm._request([{"role": "user", "content": "test"}])
    err_text = str(exc_info.value)
    assert "403" in err_text
    assert "Forbidden" in err_text
    assert "Access forbidden for key [REDACTED]" in err_text
    assert secret_key not in err_text

