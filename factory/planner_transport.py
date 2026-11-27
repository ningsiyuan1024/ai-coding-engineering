"""Optional stateless planner transport for the official OpenAI Responses API."""

from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPException, HTTPSConnection
import json
import os
import re

from .runtime import RuntimeUnavailable


KEY_VARIABLE = "FACTORY_PLANNER_OPENAI_API_KEY"
API_HOST = "api.openai.com"
MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PlannerResponse:
    request_id: str
    response_id: str
    text: str


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response field")
        result[key] = value
    return result


def _decode(body, request_id, model, effort):
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (UnicodeError, ValueError, RecursionError):
        raise RuntimeUnavailable("Invalid Responses API JSON") from None
    if not isinstance(data, dict):
        raise RuntimeUnavailable("Expected a Responses API object")
    reasoning = data.get("reasoning")
    if (data.get("object") != "response" or data.get("status") != "completed"
            or data.get("error") is not None or data.get("incomplete_details") is not None
            or data.get("model") != model or not isinstance(reasoning, dict)
            or reasoning.get("effort") != effort):
        raise RuntimeUnavailable("Provider did not confirm the exact completed model and effort")
    if (data.get("tools") != [] or data.get("tool_choice") != "none"
            or data.get("previous_response_id") is not None or data.get("conversation") is not None
            or ("store" in data and data["store"] is not False)):
        raise RuntimeUnavailable("Provider returned unexpected tools or state")
    response_id = data.get("id")
    if (not isinstance(response_id, str) or not re.fullmatch(r"resp_[a-zA-Z0-9_-]{1,180}", response_id)
            or not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", request_id)):
        raise RuntimeUnavailable("Provider response or HTTP request identity is missing")
    output = data.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise RuntimeUnavailable("Malformed provider output")
    messages = [item for item in output if item.get("type") != "reasoning"]
    if len(messages) != 1:
        raise RuntimeUnavailable("Expected one assistant message and no tool calls")
    message = messages[0]
    parts = message.get("content")
    if (message.get("type") != "message" or message.get("role") != "assistant"
            or message.get("status") != "completed" or not isinstance(parts, list) or len(parts) != 1
            or not isinstance(parts[0], dict) or parts[0].get("type") != "output_text"
            or not isinstance(parts[0].get("text"), str) or not parts[0]["text"].strip()):
        raise RuntimeUnavailable("Expected one completed assistant text payload")
    return PlannerResponse(request_id, response_id, parts[0]["text"])


class ResponsesPlanner:
    """Send only the caller-supplied projection. No CLI login, SDK state, proxy or retries."""

    def __init__(self, *, api_key=None, timeout=120):
        self._key = os.environ.get(KEY_VARIABLE, "") if api_key is None else api_key
        self.timeout = timeout

    @property
    def credentials_configured(self):
        return isinstance(self._key, str) and bool(re.fullmatch(r"[!-~]+", self._key))

    def run_data_only(self, prompt, model, effort, *, on_event=None):
        if not self.credentials_configured:
            raise RuntimeUnavailable("Dedicated Responses API credential required")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Planner input is required")
        if any(not isinstance(s, str) or not re.fullmatch(r"[\w.:-]{1,128}", s, re.ASCII)
               for s in (model, effort)):
            raise ValueError("Explicit model and effort are required")
        payload = {"model": model, "reasoning": {"effort": effort}, "input": prompt,
                   "tools": [], "tool_choice": "none", "store": False}
        # HTTPSConnection uses certificate verification and makes no redirect
        # or environment-based endpoint changes. Never forward the credential.
        try:
            with closing(HTTPSConnection(API_HOST, timeout=self.timeout)) as connection:
                connection.request("POST", "/v1/responses", json.dumps(payload).encode("utf-8"), {
                    "Authorization": "Bearer " + self._key,
                    "Content-Type": "application/json", "Accept": "application/json",
                })
                response = connection.getresponse()
                if response.status != 200:
                    raise RuntimeUnavailable(f"Responses API returned HTTP {response.status}")
                request_id = response.getheader("x-request-id")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPException, OSError):
            raise RuntimeUnavailable("Responses API connection failed") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeUnavailable("Responses API payload exceeded the planner limit")
        result = _decode(body, request_id, model, effort)
        if on_event is not None:
            on_event("runtime.turn", {"model": model, "effort": effort, "status": "completed"})
        return result
