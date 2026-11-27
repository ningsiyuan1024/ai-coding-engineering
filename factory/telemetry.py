"""Local factory tracing, metrics and logs via OTLP/HTTP JSON.

https://opentelemetry.io/docs/specs/otlp/#otlphttp

The receiver persists accepted OTLP documents for independent evidence readback.
"""

from __future__ import annotations

import gzip
import http.client
import io
import json
import math
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


_TRACE_PATH = "/v1/traces"
_METRIC_PATH = "/v1/metrics"
_LOG_PATH = "/v1/logs"
_MAX_REQUEST_BYTES = 1_048_576
_MAX_RESPONSE_BYTES = 65_536
_TIMEOUT = 5
_STATUSES = {"unset": 0, "ok": 1, "error": 2}
_SOURCES = {"observed", "self_reported", "unknown"}
_ATTRIBUTE_KEY = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,127}")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_.-])(?:authorization|password|credential|credentials|secret|"
    r"access[_-]?token|refresh[_-]?token|api[_-]?key|prompt|stdout|stderr|"
    r"content|message|transcript|payload|command|body|cookie)(?:$|[_.-])",
    re.IGNORECASE,
)


def _identifier(value, width):
    return (
        isinstance(value, str)
        and re.fullmatch(fr"[0-9a-fA-F]{{{width}}}", value) is not None
        and int(value, 16) != 0
    )


def _name(value):
    if not isinstance(value, str) or not _ATTRIBUTE_KEY.fullmatch(value):
        raise ValueError("Expected a telemetry name with letters, digits, dots or dashes")
    return value


def _typed_value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int) and -(1 << 63) <= value < (1 << 63):
        return {"intValue": str(value)}
    if isinstance(value, float) and math.isfinite(value):
        return {"doubleValue": value}
    if isinstance(value, str) and len(value) <= 512 and value.isprintable():
        return {"stringValue": value}
    raise ValueError("Telemetry attributes require bounded printable text or finite scalars")


def _attributes(values):
    attributes = {}
    if values is not None:
        for key, value in values.items():
            _name(key)
            if _SENSITIVE_KEY.search(key):
                raise ValueError("Private or raw fields cannot be exported as telemetry attributes")
            if value is not None:
                attributes[key] = _typed_value(value)
    return attributes


def _encode(attributes):
    return [{"key": key, "value": value} for key, value in attributes.items()]


def _envelope(resource_key, scope_key, record_key, records):
    resource = _attributes({"service.name": "ai-software-factory"})
    return {
        resource_key: [
            {
                "resource": {"attributes": _encode(resource)},
                scope_key: [
                    {"scope": {"name": "factory.telemetry"}, record_key: records}
                ],
            }
        ]
    }


def _endpoint_port(endpoint):
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or parsed.port < 1
        or parsed.path != _TRACE_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected an HTTP trace endpoint on 127.0.0.1 with an explicit port")
    return parsed.port


class Trace:
    """Collect spans for one run and explicitly export them after they end.

    Pass correlation metadata and allowlisted scalar observations only.
    Missing optional observations can be recorded with an explicit unknown state.
    """

    def __init__(self, endpoint, *, task_id, run_id, line, session_id=None, attributes=None):
        self._port = _endpoint_port(endpoint)
        self.trace_id = secrets.token_hex(16)
        self._start_ns = time.time_ns()
        self._tick = time.monotonic_ns()
        if any(not isinstance(value, str) or not value for value in (task_id, run_id, line)):
            raise ValueError("Task ID, run ID and production line are required")
        context = {
            "factory.task.id": task_id,
            "factory.run.id": run_id,
            "factory.line": line,
            "factory.session.id": session_id,
        }
        self._context = _attributes(context)
        self._context_keys = frozenset(context)
        self._root_attributes = _attributes(attributes)
        self._root = None
        self._spans = []
        self._exported_signals = set()

    def _now(self):
        return self._start_ns + time.monotonic_ns() - self._tick

    def bind_session(self, session_id):
        """Correlate the runtime's session ID once it is observed."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Expected a native session ID")
        value = _typed_value(session_id)
        current = self._context.get("factory.session.id")
        if current == value:
            return
        if current is not None or self._exported_signals:
            raise RuntimeError("Cannot replace published or previously bound native session")
        self._context["factory.session.id"] = value

    def span(self, name, *, attributes=None):
        if self._root is not None:
            raise RuntimeError("A trace already has a root span")
        root_attributes = self._root_attributes | _attributes(attributes)
        self._root = Span(self, name, parent=None, attributes=root_attributes)
        return self._root

    def export(self):
        """Export traces, metrics and logs over loopback and return span count."""
        if not self._spans or any(span._end_ns is None for span in self._spans):
            raise RuntimeError("Finish all spans before exporting")
        payloads = {
            _TRACE_PATH: _envelope(
                "resourceSpans", "scopeSpans", "spans", [span._to_otlp() for span in self._spans]
            ),
            _METRIC_PATH: _envelope("resourceMetrics", "scopeMetrics", "metrics", [self._duration_metric()]),
            _LOG_PATH: _envelope("resourceLogs", "scopeLogs", "logRecords", [self._completion_log()]),
        }
        for signal, payload in payloads.items():
            if signal not in self._exported_signals:
                self._post(signal, payload)
                self._exported_signals.add(signal)
        return len(self._spans)

    def _duration_metric(self):
        root = self._root
        duration = (root._end_ns - root._start_ns) / 1_000_000_000
        time_ns = str(root._end_ns)
        return {
            "name": "factory.run.duration",
            "description": "Elapsed time of the completed factory run",
            "unit": "s",
            "gauge": {
                "dataPoints": [
                    {
                        "timeUnixNano": time_ns,
                        "asDouble": duration,
                        "attributes": _encode({"factory.line": self._context["factory.line"]}),
                        "exemplars": [
                            {
                                "timeUnixNano": time_ns,
                                "asDouble": duration,
                                "traceId": self.trace_id,
                                "spanId": root.span_id,
                            }
                        ],
                    }
                ]
            },
        }

    def _completion_log(self):
        root = self._root
        time_ns = str(root._end_ns)
        error = root._status == "error"
        attributes = self._context | _attributes(
            {"factory.evidence.source": "observed", "factory.run.status": root._status}
        )
        return {
            "timeUnixNano": time_ns,
            "observedTimeUnixNano": time_ns,
            "severityNumber": 17 if error else 9,
            "severityText": "ERROR" if error else "INFO",
            "eventName": "factory.run.finished",
            "traceId": self.trace_id,
            "spanId": root.span_id,
            "flags": 1,
            "attributes": _encode(attributes),
        }

    def _post(self, signal, payload):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > _MAX_REQUEST_BYTES:
            raise ValueError("OTLP request exceeds the local receiver size limit")
        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=_TIMEOUT)
        try:
            connection.request("POST", signal, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise OSError(f"Local OTLP export rejected for {signal} (HTTP {response.status})")
            data = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(data) > _MAX_RESPONSE_BYTES:
                raise OSError("Local OTLP export response exceeds size limit")
            result = json.loads(data)
            if not isinstance(result, dict) or "partialSuccess" in result:
                raise OSError("Local OTLP receiver did not acknowledge full acceptance")
        finally:
            connection.close()


class Span:
    """Record the duration, status and correlated observations of an operation."""

    def __init__(self, trace, name, *, parent, attributes):
        self._trace = trace
        self._parent = parent
        self._name = _name(name)
        self.span_id = secrets.token_hex(8)
        self._attributes = attributes
        self._events = []
        self._start_ns = None
        self._end_ns = None
        self._status = "unset"

    def __enter__(self):
        if self._start_ns is not None:
            raise RuntimeError("A span can only be started once")
        if self._parent is not None:
            self._parent._require_open()
        self._start_ns = self._trace._now()
        self._trace._spans.append(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._end_ns = self._trace._now()
        if exc_type is not None:
            self._status = "error"

    def _require_open(self):
        if self._start_ns is None or self._end_ns is not None:
            raise RuntimeError("Span is not active")

    def span(self, name, *, attributes=None):
        self._require_open()
        return Span(self._trace, name, parent=self, attributes=_attributes(attributes))

    def set_attributes(self, attributes):
        self._require_open()
        self._attributes.update(_attributes(attributes))

    def set_status(self, status):
        self._require_open()
        if status not in _STATUSES:
            raise ValueError("Invalid trace status")
        self._status = status

    def event(self, name, *, attributes=None, source="observed"):
        self._require_open()
        if source not in _SOURCES:
            raise ValueError("Invalid observation source")
        values = _attributes(attributes)
        values["factory.evidence.source"] = _typed_value(source)
        self._events.append(
            {
                "name": _name(name),
                "timeUnixNano": str(self._trace._now()),
                "attributes": _encode(values),
            }
        )

    def add_artifact(self, relative_ref, *, kind=None, sha256=None, source="observed"):
        """Link an artifact by its relative reference and optional digest."""
        if not isinstance(relative_ref, str) or not relative_ref:
            raise ValueError("Expected a relative artifact reference")
        windows_path = PureWindowsPath(relative_ref)
        path = PurePosixPath(relative_ref.replace("\\", "/"))
        if (
            path.is_absolute()
            or windows_path.drive
            or ".." in path.parts
            or ":" in relative_ref
            or not path.name
        ):
            raise ValueError("Expected a relative artifact reference")
        if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ValueError("Invalid artifact SHA-256")
        self.event(
            "artifact.link",
            attributes={
                "factory.artifact.ref": path.as_posix(),
                "factory.artifact.kind": kind,
                "factory.artifact.sha256": sha256,
            },
            source=source,
        )

    def _to_otlp(self):
        context = self._trace._context
        span_attributes = {
            key: value
            for key, value in self._attributes.items()
            if key not in self._trace._context_keys
        }
        data = {
            "traceId": self._trace.trace_id,
            "spanId": self.span_id,
            "name": self._name,
            "kind": 1,
            "flags": 1,
            "startTimeUnixNano": str(self._start_ns),
            "endTimeUnixNano": str(self._end_ns),
            "attributes": _encode(span_attributes | context),
            "events": self._events,
            "status": {"code": _STATUSES[self._status]},
        }
        if self._parent is not None:
            data["parentSpanId"] = self._parent.span_id
        return data


def _records(document, resource_key, scope_key, record_key):
    for resource in document.get(resource_key, []):
        for scope in resource.get(scope_key, []):
            yield from scope.get(record_key, [])


_RECORD_KEYS = {
    _TRACE_PATH: ("resourceSpans", "scopeSpans", "spans"),
    _METRIC_PATH: ("resourceMetrics", "scopeMetrics", "metrics"),
    _LOG_PATH: ("resourceLogs", "scopeLogs", "logRecords"),
}


def _valid_span(span):
    start = int(span["startTimeUnixNano"])
    end = int(span["endTimeUnixNano"])
    parent = span.get("parentSpanId", "")
    return (
        _identifier(span["traceId"], 32)
        and _identifier(span["spanId"], 16)
        and (parent == "" or _identifier(parent, 16))
        and isinstance(span["name"], str)
        and bool(span["name"])
        and 0 < start <= end < 1 << 64
    )


def _valid_record(signal, record):
    if signal == _TRACE_PATH:
        return _valid_span(record)
    if signal == _LOG_PATH:
        return isinstance(record, dict) and isinstance(record.get("severityNumber", 0), int)
    return isinstance(record, dict) and isinstance(record.get("name"), str) and bool(record["name"])


class LocalTraceReceiver:
    """Receive OTLP JSON signals on an ephemeral loopback port into NDJSON."""

    def __init__(self, path):
        self.path = Path(path)
        self.metrics_path = self.path.with_name(f"{self.path.stem}.metrics{self.path.suffix}")
        self.logs_path = self.path.with_name(f"{self.path.stem}.logs{self.path.suffix}")
        self._server = None

    @property
    def endpoint(self):
        if self._server is None:
            raise RuntimeError("Local OTLP receiver is not running")
        return f"http://127.0.0.1:{self._server.server_port}{_TRACE_PATH}"

    def verify_run(self, trace_id, root_span_id, span_count):
        """Verify persisted cross-signal correlation and return the three paths."""
        spans = read_trace(self.path, trace_id)
        metrics = read_metrics(self.metrics_path, trace_id)
        logs = read_logs(self.logs_path, trace_id)
        root_ids = {span["spanId"] for span in spans if not span.get("parentSpanId")}
        exemplars = {
            exemplar.get("spanId")
            for metric in metrics
            for point in metric["gauge"]["dataPoints"]
            for exemplar in point.get("exemplars", [])
            if exemplar.get("traceId") == trace_id
        }
        if (
            len(spans) != span_count
            or root_ids != {root_span_id}
            or exemplars != {root_span_id}
            or not any(log.get("eventName") == "factory.run.finished" and log.get("spanId") == root_span_id
                       for log in logs)
        ):
            raise RuntimeError("Could not correlate persisted OTLP traces, metrics and logs")
        return {"traces": str(self.path), "metrics": str(self.metrics_path), "logs": str(self.logs_path)}

    def __enter__(self):
        if self._server is not None:
            raise RuntimeError("Local OTLP receiver is already running")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        outputs = {
            _TRACE_PATH: self.path,
            _METRIC_PATH: self.metrics_path,
            _LOG_PATH: self.logs_path,
        }
        write_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(_TIMEOUT)
                super().setup()

            def log_message(self, format, *args):
                return

            def _respond(self, status, message=None):
                code = {400: 3, 404: 5, 413: 8, 415: 3, 500: 13}.get(status)
                response = {} if message is None else {"code": code, "message": message}
                body = json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                output = outputs.get(self.path)
                if output is None:
                    self._respond(404, "Unknown OTLP endpoint")
                    return
                encoding = self.headers.get("Content-Encoding", "identity").lower()
                if self.headers.get_content_type() != "application/json" or encoding not in (
                    "identity", "gzip"
                ):
                    self._respond(415, "Expected OTLP JSON")
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                    if length <= 0:
                        raise ValueError
                    if length > _MAX_REQUEST_BYTES:
                        self._respond(413, "OTLP request exceeds size limit")
                        return
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError
                    if encoding == "gzip":
                        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                            body = compressed.read(_MAX_REQUEST_BYTES + 1)
                        if len(body) > _MAX_REQUEST_BYTES:
                            self._respond(413, "OTLP request exceeds size limit")
                            return
                    document = json.loads(body.decode("utf-8"))
                    if not isinstance(document, dict) or not all(
                        _valid_record(self.path, record)
                        for record in _records(document, *_RECORD_KEYS[self.path])
                    ):
                        raise ValueError
                    line = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
                except (AttributeError, KeyError, TypeError, ValueError, OSError, EOFError):
                    self._respond(400, "Invalid OTLP request")
                    return
                try:
                    with write_lock, output.open("a", encoding="utf-8", newline="\n") as evidence:
                        evidence.write(line + "\n")
                except OSError:
                    self._respond(500, "Could not persist OTLP signal")
                    return
                self._respond(200)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        self._server = None


def read_trace(path, trace_id):
    """Return the received spans for a trace ID, including raw OTLP attributes."""
    if not _identifier(trace_id, 32):
        raise ValueError("Invalid trace ID")
    spans = {}
    for span in _read_records(path, _TRACE_PATH):
        if span["traceId"].lower() != trace_id.lower():
            continue
        key = span["spanId"].lower()
        if key in spans and spans[key] != span:
            raise ValueError("Conflicting spans in OTLP evidence")
        spans[key] = span
    return list(spans.values())


def _read_records(path, signal):
    with Path(path).open(encoding="utf-8") as evidence:
        for line in evidence:
            yield from _records(json.loads(line), *_RECORD_KEYS[signal])


def read_metrics(path, trace_id):
    """Return received factory duration measurements correlated by exemplar."""
    if not _identifier(trace_id, 32):
        raise ValueError("Invalid trace ID")
    return [
        metric
        for metric in _read_records(path, _METRIC_PATH)
        if any(
            exemplar.get("traceId", "").lower() == trace_id.lower()
            for point in metric.get("gauge", {}).get("dataPoints", [])
            for exemplar in point.get("exemplars", [])
        )
    ]


def read_logs(path, trace_id):
    """Return received OTLP log records for a trace ID."""
    if not _identifier(trace_id, 32):
        raise ValueError("Invalid trace ID")
    return [
        record
        for record in _read_records(path, _LOG_PATH)
        if record.get("traceId", "").lower() == trace_id.lower()
    ]
