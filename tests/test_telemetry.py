import gzip
import http.client
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from factory.telemetry import LocalTraceReceiver, Trace, read_logs, read_metrics, read_trace


def attributes(record):
    return {item["key"]: item["value"] for item in record["attributes"]}


class TraceTests(unittest.TestCase):
    def test_requires_loopback_endpoint(self):
        for endpoint in (
            "http://example.com/v1/traces",
            "http://localhost:4318/v1/traces",
            "http://user@127.0.0.1:4318/v1/traces",
            "http://127.0.0.1:4318/v1/traces?redirect=external",
            "http://127.0.0.1:4318/v1/logs",
            "http://127.0.0.1/v1/traces",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                Trace(endpoint, task_id="A-1", run_id="run-1", line="sylan")

    def test_blocks_raw_fields_and_nonrelative_artifact_refs(self):
        trace = Trace("http://127.0.0.1:4318/v1/traces", task_id="A-1", run_id="run-1", line="sylan")
        with trace.span("factory.task") as root:
            for field in ("gen_ai.prompt", "tool.stdout", "auth.api_key", "command", "access_token"):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    root.event("tool.executed", attributes={field: "raw-data"})
            for value in ("../outside.log", r"C:\private.log", "/absolute.log", "https://host/artifact"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    root.add_artifact(value)
            with self.assertRaisesRegex(RuntimeError, "Finish all spans"):
                trace.export()


class LocalReceiverTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(workspace.cleanup)
        self.path = Path(workspace.name) / "evidence" / "traces.ndjson"
        self.receiver = self.enterContext(LocalTraceReceiver(self.path))
        self.port = urlsplit(self.receiver.endpoint).port

    def trace(self, run_id):
        return Trace(
            self.receiver.endpoint,
            task_id="A-1",
            run_id=run_id,
            line="sylan",
            session_id="session-1",
        )

    def post(self, body, *, encoding=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {"Content-Type": "application/json"}
            if encoding is not None:
                headers["Content-Encoding"] = encoding
            connection.request("POST", "/v1/traces", body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_traces_round_trip_and_preserve_provenance(self):
        trace = self.trace("run-1")
        with trace.span("factory.task") as root:
            root.set_attributes({"factory.run.id": "overwritten"})
            root.event(
                "model.usage",
                attributes={
                    "gen_ai.usage.input_tokens": 42,
                    "gen_ai.usage.output_tokens": 0,
                    "factory.cost.usd": None,
                    "factory.cost.status": "unknown",
                },
            )
            with root.span("factory.verify") as verify:
                verify.add_artifact(r"artifacts\verification.json", sha256="1" * 64)
                verify.event("skill.invoked", attributes={"skill.name": "review"}, source="self_reported")
                verify.set_status("ok")
        self.assertEqual(trace.export(), 2)
        self.assertEqual(trace.export(), 2)
        unrelated = self.trace("run-2")
        with unrelated.span("factory.task"):
            pass
        self.assertEqual(unrelated.export(), 1)
        requests = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(requests), 2)
        document = json.loads(requests[0])
        service = document["resourceSpans"][0]["resource"]
        self.assertEqual(attributes(service)["service.name"], {"stringValue": "ai-software-factory"})

        parent, child = read_trace(self.path, trace.trace_id.upper())
        self.assertRegex(parent["traceId"], r"^[0-9a-f]{32}$")
        self.assertRegex(parent["spanId"], r"^[0-9a-f]{16}$")
        self.assertEqual(parent["spanId"], root.span_id)
        self.assertEqual(child["parentSpanId"], root.span_id)
        self.assertNotIn("parentSpanId", parent)
        self.assertEqual(child["kind"], 1)
        self.assertEqual(child["status"], {"code": 1})
        self.assertLessEqual(int(parent["startTimeUnixNano"]), int(child["startTimeUnixNano"]))
        self.assertLessEqual(int(child["endTimeUnixNano"]), int(parent["endTimeUnixNano"]))
        for span in (parent, child):
            self.assertEqual(attributes(span)["factory.run.id"], {"stringValue": "run-1"})
            self.assertEqual(attributes(span)["factory.session.id"], {"stringValue": "session-1"})
        usage = attributes(parent["events"][0])
        self.assertEqual(usage["gen_ai.usage.input_tokens"], {"intValue": "42"})
        self.assertEqual(usage["gen_ai.usage.output_tokens"], {"intValue": "0"})
        self.assertEqual(usage["factory.cost.status"], {"stringValue": "unknown"})
        self.assertNotIn("factory.cost.usd", usage)
        artifact, skill = child["events"]
        self.assertEqual(attributes(artifact)["factory.artifact.ref"], {"stringValue": "artifacts/verification.json"})
        self.assertEqual(attributes(artifact)["factory.artifact.sha256"], {"stringValue": "1" * 64})
        self.assertEqual(attributes(skill)["factory.evidence.source"], {"stringValue": "self_reported"})

    def test_marks_failures_without_exception_text(self):
        trace = self.trace("failed-run")
        with self.assertRaises(RuntimeError), trace.span("factory.task"):
            raise RuntimeError("private failure text")
        trace.export()
        span, = read_trace(self.path, trace.trace_id)
        self.assertEqual(span["status"], {"code": 2})
        self.assertNotIn("private failure text", self.path.read_text(encoding="utf-8"))
        log, = read_logs(self.receiver.logs_path, trace.trace_id)
        self.assertEqual(log["severityText"], "ERROR")
        self.assertEqual(attributes(log)["factory.run.status"], {"stringValue": "error"})
        self.assertNotIn("private failure text", self.receiver.logs_path.read_text(encoding="utf-8"))

    def test_duration_and_completion_log_have_independent_otlp_receipts(self):
        trace = self.trace("run-with-signals")
        with trace.span("factory.task") as root:
            root.set_status("ok")
        trace.export()
        unrelated = self.trace("another-run")
        with unrelated.span("factory.task"):
            pass
        unrelated.export()
        span, = read_trace(self.path, trace.trace_id)
        metric, = read_metrics(self.receiver.metrics_path, trace.trace_id)
        log, = read_logs(self.receiver.logs_path, trace.trace_id)
        self.assertEqual(metric["name"], "factory.run.duration")
        self.assertEqual(metric["unit"], "s")
        point, = metric["gauge"]["dataPoints"]
        exemplar, = point["exemplars"]
        start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
        self.assertEqual(point["asDouble"], (end - start) / 1_000_000_000)
        self.assertEqual(exemplar["asDouble"], point["asDouble"])
        self.assertEqual(exemplar["traceId"], trace.trace_id)
        self.assertEqual(exemplar["spanId"], root.span_id)
        self.assertEqual(log["eventName"], "factory.run.finished")
        self.assertEqual(log["traceId"], trace.trace_id)
        self.assertEqual(log["spanId"], root.span_id)
        self.assertEqual(log["timeUnixNano"], point["timeUnixNano"])
        self.assertEqual(attributes(log)["factory.run.id"], {"stringValue": "run-with-signals"})
        self.assertEqual(attributes(log)["factory.run.status"], {"stringValue": "ok"})
        other_log, = read_logs(self.receiver.logs_path, unrelated.trace_id)
        self.assertEqual(attributes(other_log)["factory.run.status"], {"stringValue": "unset"})

    def test_a_retry_resumes_from_the_first_unacknowledged_signal(self):
        trace = self.trace("retry-metric")
        with trace.span("factory.task"):
            pass
        self.receiver.metrics_path.mkdir()
        with self.assertRaisesRegex(OSError, "/v1/metrics"):
            trace.export()
        self.assertTrue(self.path.is_file())
        self.assertFalse(self.receiver.logs_path.exists())
        self.receiver.metrics_path.rmdir()
        self.assertEqual(trace.export(), 1)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(len(read_metrics(self.receiver.metrics_path, trace.trace_id)), 1)
        self.assertEqual(len(read_logs(self.receiver.logs_path, trace.trace_id)), 1)

    def test_native_session_can_be_linked_after_the_trace_started(self):
        trace = Trace(self.receiver.endpoint, task_id="A-1", run_id="native-run", line="sylan")
        with trace.span("factory.task") as root:
            with root.span("factory.session"):
                pass
            trace.bind_session("native-session-id")
            trace.bind_session("native-session-id")
            with self.assertRaises(RuntimeError):
                trace.bind_session("other-session-id")
        trace.export()
        traces = read_trace(self.path, trace.trace_id)
        logs = read_logs(self.receiver.logs_path, trace.trace_id)
        for record in traces + logs:
            self.assertEqual(
                attributes(record)["factory.session.id"],
                {"stringValue": "native-session-id"},
            )

    def test_validates_requests_and_accepts_gzip(self):
        self.assertEqual(self.post(b"not json")[0], 400)
        self.assertEqual(self.post(b'{"resourceSpans":[null]}')[0], 400)
        self.assertFalse(self.path.exists())
        payload = b'{"resourceSpans":[],"unknownField":true}'
        self.assertEqual(self.post(gzip.compress(payload), encoding="gzip"), (200, {}))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), json.loads(payload))

    def test_duplicate_receipts_have_one_span(self):
        trace = self.trace("retry")
        with trace.span("factory.task"):
            pass
        trace.export()
        payload = self.path.read_bytes()
        self.assertEqual(self.post(payload)[0], 200)
        self.assertEqual(len(read_trace(self.path, trace.trace_id)), 1)
