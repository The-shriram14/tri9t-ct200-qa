import json, re, urllib.request, urllib.error
from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from .schemas import LLMTestOutput

SYSTEM_PROMPT = """You are a careful QA test-design assistant for a fictional medical device manual.
Use only the supplied source text. Do not invent thresholds, timing, error codes, or behaviors.
Do not infer or calculate unstated thresholds, timings, capacities, or relationships from other values in the source. Include a numeric value only when it is explicitly stated in the supplied source text; otherwise, describe the test without inventing a value.
Return JSON only with this exact top-level shape: {\"test_cases\": [{\"title\": str, \"preconditions\": [str], \"steps\": [str], \"expected_result\": str, \"source_node_ids\": [str], \"rationale\": str}]}. Return 3 to 5 distinct test cases. Steps must be executable and expected results must be grounded in the supplied requirement. If a requirement is ambiguous, describe the ambiguity instead of guessing."""

class LLMGenerationError(Exception):
    pass


def _sanitize_error_text(text: str) -> str:
    if not text:
        return ""
    text = str(text)
    api_key = globals().get("LLM_API_KEY", "")
    if api_key and len(api_key) >= 4:
        text = text.replace(api_key, "[REDACTED]")
    text = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", text)
    text = re.sub(r"gsk_[A-Za-z0-9_\-]+", "gsk_[REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-[REDACTED]", text)
    if len(text) > 1000:
        text = text[:1000] + "... [truncated]"
    return text


def _request(messages: list[dict]) -> str:
    payload = json.dumps({"model": LLM_MODEL, "temperature": 0.1, "response_format": {"type": "json_object"}, "messages": messages}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "Tri9T-CT200-QA/1.0",
    }
    req = urllib.request.Request(LLM_BASE_URL, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            raw = exc.read()
            if raw:
                body = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        safe_body = _sanitize_error_text(body)
        detail = f"{exc} - {safe_body}" if safe_body else str(exc)
        safe_msg = _sanitize_error_text(f"LLM provider request failed: {detail}")
        raise LLMGenerationError(safe_msg) from exc
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as exc:
        safe_msg = _sanitize_error_text(f"LLM provider request failed: {exc}")
        raise LLMGenerationError(safe_msg) from exc



def generate_test_cases(source_nodes: list[dict]) -> dict:
    if not LLM_API_KEY:
        raise LLMGenerationError("LLM_API_KEY is not configured. Set it in .env before calling generation.")
    source_text = "\n\n".join(
        f"NODE_ID: {n['node_id']}\nHEADING: {n['heading']}\nSOURCE VERSION: {n['version_number']}\nCONTENT:\n{n['text_snapshot']}"
        for n in source_nodes
    )
    user_prompt = "Create 3–5 QA test-case ideas from these selected source nodes. Cite the exact source node IDs in source_node_ids.\n\n" + source_text
    last_error = None
    for attempt in range(2):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
        if attempt:
            messages.append({"role": "user", "content": "Your previous response failed schema validation. Return corrected JSON only, with 3–5 valid test_cases and every required field."})
        try:
            raw = _request(messages)
            parsed = json.loads(raw)
            validated = LLMTestOutput.model_validate(parsed)
            allowed_ids = {n["node_id"] for n in source_nodes}
            for case in validated.test_cases:
                if not set(case.source_node_ids).issubset(allowed_ids):
                    raise ValueError("LLM returned a source_node_id outside the submitted selection")
            return {"test_cases": [x.model_dump() for x in validated.test_cases], "attempts": attempt + 1}
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            last_error = exc
    raise LLMGenerationError(f"LLM output remained invalid after 2 attempts: {last_error}")
