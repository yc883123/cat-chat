# -*- coding: utf-8 -*-
"""Offline integration tests for the adjacent Windows PowerShell diagnostic.

Run with the repository interpreter (no dependencies, app imports or real config)::

    .venv\\Scripts\\python.exe -B -I \
        verify\\claude-cache-diagnostic\\test_cache_diagnostic.py -v

All HTTP traffic goes to a ThreadingHTTPServer bound to 127.0.0.1:0. Each test
owns a temporary config and output parent. Report assertions intentionally use
only the diagnostic's essential fields, not a complete JSON schema or exact
Chinese prose. No fixture server or PowerShell process starts at import time.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
import zipfile


SCRIPT = Path(__file__).resolve().with_name("cache-diagnostic.ps1")
API_KEY = "sk-SECRET-NOT-FOR-REPORT"
BEARER_SECRET = "fixture-bearer-DO-NOT-REPORT"
COOKIE_SECRET = "fixture-cookie-DO-NOT-REPORT"
HEADER_SECRET = "fixture-header-DO-NOT-REPORT"
PROVIDER = "Fixture Provider"
MODEL = "claude-test"
TIMEOUT_SECONDS = 10
ROUTES = {
    "/v1/messages": "claude",
    "/messages": "claude",
    "/v1/chat/completions": "openai_chat",
    "/chat/completions": "openai_chat",
}


def sse(payload: dict | str, event: str = "") -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    prefix = "event: " + event + "\r\n" if event else ""
    return (prefix + "data: " + data + "\r\n\r\n").encode("utf-8")


def claude_events(response_id: str, round_number: int) -> list[bytes]:
    """Cache counters exist ONLY in message_start.message.usage, as on Anthropic."""
    cold = round_number == 1
    start = {
        "type": "message_start",
        "message": {
            "id": response_id,
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 25,
                "cache_read_input_tokens": 0 if cold else 6000,
                "cache_creation_input_tokens": 6000 if cold else 0,
                "output_tokens": 0,
            },
        },
    }
    return [
        sse(start, "message_start"),
        sse({"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}}, "content_block_start"),
        sse({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "OK"}}, "content_block_delta"),
        sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
        sse({"type": "message_delta",
             "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 1}}, "message_delta"),
        sse({"type": "message_stop"}, "message_stop"),
    ]


def chat_events(response_id: str, round_number: int) -> list[bytes]:
    base = {"id": response_id, "object": "chat.completion.chunk",
            "created": 1700000000, "model": MODEL}
    return [
        sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"},
                                 "finish_reason": None}]}),
        sse({**base, "choices": [{"index": 0, "delta": {"content": "OK"},
                                 "finish_reason": None}]}),
        sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        sse({**base, "choices": [], "usage": {
            "prompt_tokens": 6025,
            "completion_tokens": 1,
            "total_tokens": 6026,
            "prompt_tokens_details": {"cached_tokens": 0 if round_number == 1 else 6000},
        }}),
        sse("[DONE]"),
    ]


@dataclass(frozen=True)
class RequestRecord:
    method: str
    path: str
    format: str
    headers: dict
    body: dict
    received_at: float


class FixtureServer(ThreadingHTTPServer):
    """Thread-safe recorder; an Event releases stalled handlers during cleanup."""

    daemon_threads = True

    def __init__(self, config_path: Path):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.config_path = config_path
        self.scenario = "success"
        self.records: list[RequestRecord] = []
        self.handler_errors: list[str] = []
        self.counts: Counter = Counter()
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.stalled = threading.Event()
        self.thread = threading.Thread(target=self.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:" + str(self.server_address[1])

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.release.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=3)

    def capture(self, record: RequestRecord) -> int:
        with self.lock:
            self.records.append(record)
            self.counts[record.format] += 1
            return self.counts[record.format]

    def snapshot(self) -> list[RequestRecord]:
        with self.lock:
            return list(self.records)

    def handle_error(self, request, client_address) -> None:
        # Do not let a background handler failure masquerade as a script timeout.
        import traceback
        with self.lock:
            self.handler_errors.append(traceback.format_exc())

    def error_echo(self) -> dict:
        # The extra bearer/cookie values are NOT the configured API key: merely
        # replacing that one key must not pass the report-redaction test.
        message = (
            "Fixture deliberate failure; these are synthetic secrets.\n"
            f"Selected credential: {API_KEY}\n"
            f"Authorization: Bearer {BEARER_SECRET}\n"
            f"Cookie: fixture_session={COOKIE_SECRET}\n"
            f"Configuration file: {self.config_path}\n"
        )
        return {"type": "error", "error": {"type": "overloaded_error", "message": message}}


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args) -> None:
        pass

    def respond(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Set-Cookie", f"fixture_session={COOKIE_SECRET}; HttpOnly")
        self.send_header("X-Fixture-Private", HEADER_SECRET)
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A reader is allowed to stop as soon as it sees an SSE error.
            pass

    def do_GET(self) -> None:
        self.server.capture(RequestRecord("GET", self.path, "unexpected", {}, {}, time.monotonic()))
        self.respond(405, b'{"error":{"message":"No catalog request was expected"}}',
                     "application/json")

    def do_POST(self) -> None:
        self.connection.settimeout(5)
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 1024 * 1024:
            raise ValueError("fixture expected a bounded JSON request with Content-Length")
        body = json.loads(self.rfile.read(size).decode("utf-8-sig"))
        if not isinstance(body, dict):
            raise ValueError("fixture expected a JSON object")
        protocol = ROUTES.get(urlsplit(self.path).path, "unexpected")
        record = RequestRecord("POST", self.path, protocol,
                               {k.lower(): v for k, v in self.headers.items()},
                               body, time.monotonic())
        ordinal = self.server.capture(record)
        if protocol == "unexpected":
            self.respond(404, b'{"error":{"message":"Unexpected fixture route"}}', "application/json")
            return
        scenario = self.server.scenario
        if scenario == "http_error":
            data = json.dumps(self.server.error_echo()).encode("utf-8")
            self.respond(503, data, "application/json")
            return
        if scenario == "sse_error":
            self.respond(200, sse(self.server.error_echo(), "error"), "text/event-stream")
            return
        suffix = "reused" if scenario == "same_id" else str(ordinal)
        response_id = "fixture_" + protocol + "_" + suffix
        events = (claude_events(response_id, ordinal) if protocol == "claude"
                  else chat_events(response_id, ordinal))
        if scenario == "stall":
            # Headers AND one full SSE event arrive. Then an unfinished data line
            # blocks ReadLineAsync: bounding SendAsync alone does not pass.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(events[0] + b"data: {")
            self.wfile.flush()
            self.server.stalled.set()
            self.server.release.wait(timeout=40)
            return
        self.respond(200, b"".join(events), "text/event-stream")


@dataclass
class DiagnosticResult:
    report: dict
    text: str
    directory: Path
    requests: list[RequestRecord]
    elapsed: float
    finished_at: float


def values_for_key(value, name: str):
    """Find a request parameter regardless of its position in content blocks."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == name:
                yield item
            yield from values_for_key(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from values_for_key(item, name)


def content_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(block.get("text", "")) for block in value if isinstance(block, dict))
    return ""


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell 5.1 (powershell.exe)")
class CacheDiagnosticIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.powershell = shutil.which("powershell.exe")
        if not cls.powershell:
            raise unittest.SkipTest("powershell.exe is unavailable; pwsh is not a PS 5.1 substitute")
        if not SCRIPT.is_file():
            raise FileNotFoundError(f"The diagnostic is not ready: {SCRIPT}")

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cache-diagnostic-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        profile_dir = self.root / "fixture profile"
        profile_dir.mkdir()
        self.config_path = profile_dir / "config.json"
        self.output_parent = self.root / "output parent"
        self.output_parent.mkdir()
        self.fixture = FixtureServer(self.config_path)
        self.fixture.start()
        self.addCleanup(self.fixture.close)
        config = {
            "providers": [{
                "id": "fixture", "name": PROVIDER, "kind": "online",
                "base_url": self.fixture.url, "model": MODEL, "api_key": API_KEY,
                "request_format": "openai_chat", "reasoning_effort": "auto",
            }],
            "proxy": {"enabled": False},
        }
        self.config_bytes = json.dumps(config, ensure_ascii=False).encode("utf-8")
        self.config_path.write_bytes(self.config_bytes)
        self.previous_reports: dict[Path, bytes] = {}

    def assert_redacted(self, text: str, label: str) -> None:
        # Search both raw serialized JSON and decoded values so backslash or
        # Unicode escaping cannot conceal a leaked credential/path from the test.
        needles = [API_KEY, BEARER_SECRET, COOKIE_SECRET, HEADER_SECRET,
                   str(self.config_path), self.config_path.as_posix(), self.config_path.as_uri()]
        for needle in needles:
            variants = {needle, json.dumps(needle)[1:-1]}
            for variant in variants:
                self.assertNotIn(variant.casefold(), text.casefold(),
                                 f"{label} leaked a synthetic secret or the full config path")

    def invoke(self, mode: str = "both", budget: float = 40) -> DiagnosticResult:
        before_entries = set(self.output_parent.iterdir())
        before_requests = len(self.fixture.snapshot())
        command = [
            self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
            "-ConfigPath", str(self.config_path), "-ProviderName", PROVIDER,
            "-Model", MODEL, "-Mode", mode, "-OutputDirectory", str(self.output_parent),
            "-Yes", "-NoPause", "-TimeoutSeconds", str(TIMEOUT_SECONDS),
        ]
        env = {key: value for key, value in os.environ.items()
               if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
        env["NO_PROXY"] = "127.0.0.1,localhost"
        start = time.monotonic()
        try:
            process = subprocess.run(command, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=budget, check=False)
        except subprocess.TimeoutExpired:
            self.fail(f"PowerShell exceeded the {budget:g}s process watchdog; "
                      f"recorded {len(self.fixture.snapshot()) - before_requests} requests")
        finished = time.monotonic()
        console = process.stdout + "\n" + process.stderr
        requests = self.fixture.snapshot()[before_requests:]
        self.assertFalse(self.fixture.handler_errors, "\n".join(self.fixture.handler_errors))
        # Exit codes for unsuccessful probes are deliberately not prescribed.
        # A valid failure report is required regardless of the chosen exit code.
        added = set(self.output_parent.iterdir()) - before_entries
        self.assertEqual(len(added), 1,
                         f"Expected one new report directory; exit={process.returncode}\n{console[-3000:]}")
        directory = added.pop()
        self.assertTrue(directory.is_dir(), "reports must live in a per-invocation subdirectory")
        artifacts = {}
        for name in ("report.json", "report.txt"):
            path = directory / name
            self.assertTrue(path.is_file(), f"Missing {name}; exit={process.returncode}\n{console[-3000:]}")
            data = path.read_bytes()
            artifacts[name] = data
            text = data.decode("utf-8-sig")
            self.assertTrue(text.strip(), f"{name} must not be empty")
            self.assert_redacted(text, name)
            self.previous_reports[path] = data
        report = json.loads(artifacts["report.json"].decode("utf-8-sig"))
        self.assertIsInstance(report, dict)
        self.assertIsInstance(report.get("runs"), list)
        self.assertIsInstance(report.get("findings"), list)
        self.assert_redacted(json.dumps(report, ensure_ascii=False), "decoded JSON")
        # Inspect scalar values separately: JSON serialization would escape a
        # raw Windows path again even with ensure_ascii=False.
        def inspect(value):
            if isinstance(value, str):
                self.assert_redacted(value, "JSON value")
            elif isinstance(value, dict):
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)
        inspect(report)
        archive = directory / "report.zip"
        if archive.exists():
            with zipfile.ZipFile(archive) as bundle:
                self.assertCountEqual(bundle.namelist(), ["report.json", "report.txt"],
                                      "the shareable archive must not contain config or raw headers")
                for name, data in artifacts.items():
                    self.assertEqual(bundle.read(name), data, "archive contains a stale/different report")
            self.previous_reports[archive] = archive.read_bytes()
        for path, original in self.previous_reports.items():
            self.assertEqual(path.read_bytes(), original, "a later invocation overwrote an earlier report")
        self.assertEqual(self.config_path.read_bytes(), self.config_bytes, "diagnostic modified its input config")
        self.assertTrue(all(path.is_dir() for path in self.output_parent.iterdir()),
                        "report files must not be written directly into OutputDirectory")
        return DiagnosticResult(report, artifacts["report.txt"].decode("utf-8-sig"),
                                directory, requests, finished - start, finished)

    def assert_requests(self, result: DiagnosticResult, counts: dict[str, int]) -> None:
        self.assertEqual(Counter(request.format for request in result.requests), Counter(counts),
                         "unexpected request, missing protocol, extra round or automatic retry")
        self.assertTrue(all(request.method == "POST" for request in result.requests),
                        "explicit -Model must not cause a GET /models probe")
        self.assertEqual(Counter(row.get("format") for row in result.report["runs"]), Counter(counts))
        for request in result.requests:
            body = request.body
            self.assertIs(body.get("stream"), True)
            self.assertEqual(body.get("max_tokens"), 256)
            self.assertEqual(body.get("model"), MODEL)
            self.assertNotIn("cookie", request.headers, "the fixture's Set-Cookie must not be replayed")
            controls = list(values_for_key(body, "cache_control"))
            if request.format == "claude":
                self.assertTrue(controls, "Claude probes must request prompt caching")
                self.assertEqual(request.headers.get("x-api-key"), API_KEY)
            else:
                self.assertEqual(controls, [], "Chat probes must not contain Claude cache_control")
                self.assertEqual(request.headers.get("authorization"), "Bearer " + API_KEY)
        for protocol, count in counts.items():
            rows = self.rows(result, protocol)
            self.assertEqual([row.get("round") for row in rows], list(range(1, count + 1)))
            if count != 2:
                continue
            pair = [request.body for request in result.requests if request.format == protocol]
            systems = [body.get("system") if protocol == "claude" else
                       [message for message in body.get("messages", []) if message.get("role") == "system"]
                       for body in pair]
            self.assertTrue(systems[0], "a nonempty common system prefix is required")
            self.assertEqual(systems[0], systems[1], "system must stay identical across the two requests")
            messages = [body["messages"] for body in pair]
            for sequence in messages:
                self.assertTrue(sequence)
                self.assertEqual(sequence[-1].get("role"), "user")
                self.assertTrue(content_text(sequence[-1].get("content")))
            self.assertEqual(messages[0][:-1], messages[1][:-1], "the shared message prefix changed")
            self.assertNotEqual(messages[0][-1]["content"], messages[1][-1]["content"],
                                "identical requests cannot rule out full-response caching")
            for key, equal in (("system_sha256", True), ("request_body_sha256", False)):
                # These evidence fields may grow, but their existing meaning is stable.
                self.assertRegex(rows[0].get(key, ""), r"^[0-9a-fA-F]{64}$")
                self.assertRegex(rows[1].get(key, ""), r"^[0-9a-fA-F]{64}$")
                self.assertEqual(rows[0][key] == rows[1][key], equal, key)

    @staticmethod
    def rows(result: DiagnosticResult, protocol: str) -> list[dict]:
        return sorted((row for row in result.report["runs"] if row.get("format") == protocol),
                      key=lambda row: row.get("round", 0))

    def assert_successful(self, result: DiagnosticResult) -> None:
        for row in result.report["runs"]:
            self.assertEqual(row.get("http_status"), 200)
            self.assertIs(row.get("success"), True, row.get("error"))
            self.assertFalse(row.get("error"))

    def test_both_formats_preserve_prefix_and_expose_anthropic_parser_gap(self) -> None:
        result = self.invoke()
        self.assert_requests(result, {"claude": 2, "openai_chat": 2})
        self.assert_successful(result)
        for protocol in ("claude", "openai_chat"):
            pair = self.rows(result, protocol)
            self.assertTrue(pair[0].get("response_id_sha256"))
            self.assertNotEqual(pair[0]["response_id_sha256"], pair[1].get("response_id_sha256"))
            for index, row in enumerate(pair):
                with self.subTest(protocol=protocol, round=index + 1):
                    cached = 6000 if index else 0
                    written = 6000 if protocol == "claude" and index == 0 else 0
                    server = row["server_usage"]
                    snapshot = row["cat_chat_snapshot_usage"]
                    self.assertEqual(server.get("input_tokens"), 6025)
                    self.assertEqual(server.get("cached_tokens"), cached)
                    self.assertEqual(server.get("cache_creation_input_tokens"), written)
                    self.assertEqual(server.get("output_tokens"), 1)
                    self.assertIs(server.get("has_usage"), True)
                    self.assertIs(server.get("has_cache_read_field"), True)
                    self.assertAlmostEqual(server.get("cache_hit_percent"), cached / 6025 * 100, delta=0.002)
                    self.assertEqual(snapshot.get("output_tokens"), 1)
                    events = row.get("usage_events", [])
                    if protocol == "claude":
                        self.assertEqual(snapshot.get("input_tokens"), 0)
                        self.assertEqual(snapshot.get("cached_tokens"), 0)
                        self.assertIs(snapshot.get("has_cache_read_field"), False)
                        starts = [event for event in events if event.get("location") == "message.usage"]
                        self.assertEqual(len(starts), 1, "retain message_start usage as source evidence")
                        self.assertEqual(starts[0]["usage"].get("input_tokens"), 25)
                        self.assertEqual(starts[0]["usage"].get("cache_read_input_tokens"), cached)
                        self.assertEqual(starts[0]["usage"].get("cache_creation_input_tokens"), written)
                        self.assertTrue(any(event.get("event") == "message_delta" and
                                            event.get("usage") == {"output_tokens": 1} for event in events))
                    else:
                        self.assertEqual(snapshot.get("input_tokens"), 6025)
                        self.assertEqual(snapshot.get("cached_tokens"), cached)
                        self.assertIs(snapshot.get("has_cache_read_field"), True)
                        self.assertTrue(any(event.get("usage", {}).get("prompt_tokens_details", {})
                                            .get("cached_tokens") == cached for event in events))
        findings = [str(item) for item in result.report["findings"]]
        self.assertTrue(any("claude" in line.lower() and "6000" in line and
                            re.search(r"不一致|遗漏|漏读|mismatch|missing|differ|discrepancy", line, re.I)
                            for line in findings), "findings must explain the server/parser discrepancy")
        self.assertIn("6000", result.text)

    def test_single_format_modes_isolate_reports_and_warn_about_reused_ids(self) -> None:
        self.fixture.scenario = "same_id"
        directories = set()
        # Also verifies that configured follows request_format, not the Claude model name.
        for mode, protocol in (("configured", "openai_chat"), ("chat", "openai_chat"), ("claude", "claude")):
            with self.subTest(mode=mode):
                result = self.invoke(mode)
                self.assert_requests(result, {protocol: 2})
                self.assert_successful(result)
                self.assertNotIn(result.directory, directories)
                directories.add(result.directory)
                pair = self.rows(result, protocol)
                self.assertTrue(pair[0].get("response_id_sha256"))
                self.assertEqual(pair[0]["response_id_sha256"], pair[1].get("response_id_sha256"))
                findings = [str(item) for item in result.report["findings"]]
                self.assertTrue(any(re.search(r"响应|response|\bid\b", line, re.I) and
                                    re.search(r"同一|相同|重复|复用|same|duplicat|identical|reus", line, re.I)
                                    for line in findings), "same response ID must produce a warning")

    def test_503_stops_each_format_without_retry_and_redacts_error_echo(self) -> None:
        self.fixture.scenario = "http_error"
        result = self.invoke()
        self.assert_requests(result, {"claude": 1, "openai_chat": 1})
        for row in result.report["runs"]:
            self.assertEqual(row.get("http_status"), 503)
            self.assertIs(row.get("success"), False)
            self.assertTrue(row.get("error"), "report the HTTP failure without exposing the raw body")
        self.assertTrue(result.report["findings"], "failed probes need an explanatory finding")

    def test_http_200_error_event_stops_each_format_and_redacts_error_echo(self) -> None:
        self.fixture.scenario = "sse_error"
        result = self.invoke()
        self.assert_requests(result, {"claude": 1, "openai_chat": 1})
        for row in result.report["runs"]:
            self.assertEqual(row.get("http_status"), 200)
            self.assertIs(row.get("success"), False, "HTTP 200 must not hide an SSE error event")
            self.assertTrue(row.get("error"))
        self.assertTrue(result.report["findings"])

    def test_stalled_sse_read_has_one_bounded_request_and_a_failure_report(self) -> None:
        self.fixture.scenario = "stall"
        result = self.invoke("claude", budget=25)
        self.assert_requests(result, {"claude": 1})
        self.assertTrue(self.fixture.stalled.is_set(), "the server must actually enter the stalled-read case")
        row = result.report["runs"][0]
        self.assertEqual(row.get("http_status"), 200, "response headers arrived before the stall")
        self.assertIs(row.get("success"), False)
        self.assertRegex(str(row.get("error", "")), r"(?i)超时|时间限制|timeout|timed\s*out")
        # Allow scheduling/report-writing overhead, but not another full timeout
        # interval. Starting from receipt avoids charging PowerShell startup time.
        request_seconds = result.finished_at - result.requests[0].received_at
        self.assertGreaterEqual(request_seconds, TIMEOUT_SECONDS - 2)
        self.assertLess(request_seconds, TIMEOUT_SECONDS + 8)
        self.assertLess(result.elapsed, 25)


if __name__ == "__main__":
    unittest.main()
