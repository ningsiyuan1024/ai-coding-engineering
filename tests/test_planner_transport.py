from http.client import HTTPConnection
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch

from factory import planner_transport as transport
from factory.runtime import RuntimeUnavailable


MODEL, EFFORT = "gpt-6.2-astra", "ultra"


def sample():
    return {
        "id": "resp_fixture", "object": "response", "status": "completed",
        "model": MODEL, "reasoning": {"effort": EFFORT}, "store": False,
        "tools": [], "tool_choice": "none", "previous_response_id": None,
        "output": [
            {"type": "reasoning", "id": "rs_fixture", "summary": []},
            {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": '{"action":"hold"}'}]},
        ],
    }


class TransportTests(unittest.TestCase):
    def test_http_roundtrip_only_transmits_the_explicit_projection(self):
        # Exercise actual HTTP request serialization and HTTPResponse decoding
        # without requiring a network permit for even a loopback socket.
        fixture = sample()
        del fixture["store"]  # optional echo; the no-store request remains explicit
        body = json.dumps(fixture).encode()
        wire = Mock()
        wire.makefile.return_value = BytesIO(b"HTTP/1.1 200 OK\r\nx-request-id: req_fixture\r\n"
                                              + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        connection = HTTPConnection("api.openai.com")
        connection.sock = wire
        observer = Mock()
        with patch.object(transport, "HTTPSConnection", return_value=connection) as dial:
            result = transport.ResponsesPlanner(api_key="fixture", timeout=5).run_data_only(
                "synthetic budget", MODEL, EFFORT, on_event=observer)
        dial.assert_called_once_with("api.openai.com", timeout=5)
        request = b"".join(call.args[0] for call in wire.sendall.call_args_list)
        headers, payload = request.split(b"\r\n\r\n", 1)
        self.assertTrue(headers.startswith(b"POST /v1/responses HTTP/1.1\r\n"))
        self.assertIn(b"Authorization: Bearer fixture", headers.split(b"\r\n"))
        self.assertEqual(json.loads(payload), {
            "model": MODEL, "reasoning": {"effort": EFFORT}, "input": "synthetic budget",
            "store": False, "tools": [], "tool_choice": "none",
        })
        self.assertEqual((result.request_id, result.response_id, result.text),
                         ("req_fixture", "resp_fixture", '{"action":"hold"}'))
        observer.assert_called_once_with("runtime.turn", {"model": MODEL, "effort": EFFORT, "status": "completed"})

    def test_missing_credential_never_opens_connection(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "ambient", transport.KEY_VARIABLE: ""}), \
             patch.object(transport, "HTTPSConnection") as dial:
            client = transport.ResponsesPlanner()
            self.assertFalse(client.credentials_configured)
            with self.assertRaises(RuntimeUnavailable):
                client.run_data_only("synthetic", MODEL, EFFORT)
            dial.assert_not_called()

    def test_missing_or_mismatched_provider_evidence_fails_closed(self):
        for mutation in [
            {"status": "incomplete"}, {"model": "other"}, {"reasoning": None},
            {"reasoning": {"effort": "high"}}, {"reasoning": {}}, {"id": None},
            {"store": True}, {"tools": [{"type": "web_search"}]}, {"tool_choice": "auto"},
            {"previous_response_id": "resp_parent"}, {"conversation": "conv_parent"},
            {"output": [{"type": "function_call", "name": "run"}]},
            {"output": sample()["output"] + [{"type": "web_search_call"}]},
            {"output": [{"type":"message", "role":"assistant", "status":"completed",
                         "content":[{"type":"refusal", "refusal":"fixture"}]}]},
        ]:
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeUnavailable):
                transport._decode(json.dumps(sample() | mutation).encode(), "req_fixture", MODEL, EFFORT)
        for name in ["model","reasoning","tools","tool_choice"]:
            obj = sample()
            del obj[name]
            with self.subTest(missing=name), self.assertRaises(RuntimeUnavailable):
                transport._decode(json.dumps(obj).encode(), "req_fixture", MODEL, EFFORT)
        for body in [b"[]", b"not json", b'{"id":"resp_first","id":"resp_last"}']:
            with self.assertRaises(RuntimeUnavailable):
                transport._decode(body, "req_fixture", MODEL, EFFORT)
        with self.assertRaises(RuntimeUnavailable):
            transport._decode(json.dumps(sample()).encode(), None, MODEL, EFFORT)

    def test_http_redirect_or_invalid_body_never_reports_a_turn_or_retries(self):
        for status, body in ((302,b""),(401,b""),(200,b"{}"),(200,b"x"*(transport.MAX_RESPONSE_BYTES+1))):
            response = Mock(status=status)
            response.read.return_value = body
            observer = Mock()
            with patch.object(transport, "HTTPSConnection") as dial:
                dial.return_value.getresponse.return_value = response
                with self.assertRaises(RuntimeUnavailable):
                    transport.ResponsesPlanner(api_key="fixture").run_data_only("input", MODEL, EFFORT,
                                                                             on_event=observer)
                observer.assert_not_called()
                self.assertEqual(dial.call_count, 1)
