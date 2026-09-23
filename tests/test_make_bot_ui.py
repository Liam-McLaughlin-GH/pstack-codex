from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "make-bot-ui" / "scripts"


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = load_script("make_bot_ui_serve", "serve.py")
capabilities = load_script("make_bot_ui_capabilities", "capabilities.py")


class CaptureState:
    def __init__(self, status: int = HTTPStatus.OK, response_body: bytes = b"") -> None:
        self.status = status
        self.response_body = response_body
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self.lock = threading.Lock()


def capture_handler(state: CaptureState) -> type[BaseHTTPRequestHandler]:
    class CaptureHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            with state.lock:
                state.requests.append(({key.lower(): value for key, value in self.headers.items()}, body))
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.response_body)))
            self.end_headers()
            self.wfile.write(state.response_body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return CaptureHandler


class RunningServer:
    def __init__(self, server: ThreadingHTTPServer) -> None:
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        finally:
            error.close()


def raw_request(url: str, request_line: bytes) -> bytes:
    parsed = urlsplit(url)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=2) as connection:
        connection.sendall(request_line + b"\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


class MakeBotUIServerTests(unittest.TestCase):
    @staticmethod
    def authorization_headers() -> dict[str, str]:
        return {"Authorization": "Bearer trigger-secret"}

    def start_relay(
        self,
        target: str,
        failure_log: Path,
        *,
        webhook_key: str = "test-secret",
        trigger_token: str = "trigger-secret",
        allow_local_http: bool = True,
    ) -> RunningServer:
        config = serve.RelayConfig(
            target,
            webhook_key,
            failure_log,
            timeout=1,
            trigger_token=trigger_token,
            allow_local_http=allow_local_http,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(config))
        return RunningServer(server)

    def test_page_and_click_relay_json_with_server_side_headers(self) -> None:
        capture = CaptureState()
        with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
            with tempfile.TemporaryDirectory(prefix="make-bot-ui-test-") as temporary:
                failure_log = Path(temporary) / "failures.jsonl"
                with self.start_relay(target, failure_log) as local:
                    with urlopen(Request(local + "/", method="GET"), timeout=2) as response:
                        status = response.status
                        page = response.read().decode("utf-8")
                        set_cookie = response.headers["Set-Cookie"]
                        cookie = set_cookie.split(";", 1)[0]
                    self.assertEqual(status, HTTPStatus.OK)
                    self.assertIn("Send status check", page)
                    self.assertNotIn(target, page)
                    self.assertNotIn("test-secret", page)
                    self.assertNotIn("trigger-secret", page)
                    self.assertNotIn("Secure", set_cookie)
                    self.assertIn("no-store", response.headers["Cache-Control"])

                    request = Request(local + "/send", method="POST", data=json.dumps({
                        "action": "status",
                        "message": "status check",
                    }).encode("utf-8"), headers={
                        "Content-Type": "application/json",
                        "Cookie": cookie,
                    })
                    with urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, HTTPStatus.OK)
                        self.assertEqual(json.loads(response.read())["status"], HTTPStatus.OK)

                self.assertEqual(len(capture.requests), 1)
                headers, body = capture.requests[0]
                self.assertEqual(json.loads(body), {"action": "status", "message": "status check"})
                self.assertEqual(headers["authorization"], "Bearer test-secret")
                self.assertEqual(headers["x-automation-key"], "test-secret")
                self.assertEqual(headers["content-type"], "application/json")
                self.assertFalse(failure_log.exists())

    def test_failed_webhook_is_not_retried_and_logs_only_payload(self) -> None:
        capture = CaptureState(status=HTTPStatus.BAD_GATEWAY, response_body=b"webhook-secret-response")
        with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
            with tempfile.TemporaryDirectory(prefix="make-bot-ui-failure-test-") as temporary:
                failure_log = Path(temporary) / "nested" / "failures.jsonl"
                with self.start_relay(target, failure_log) as local:
                    status, body = request_json(
                        local + "/send",
                        method="POST",
                        payload={"action": "note", "message": "harmless failure"},
                        headers=self.authorization_headers(),
                    )
                    self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
                    self.assertEqual(
                        body,
                        {"error": "webhook request failed", "status": HTTPStatus.BAD_GATEWAY},
                    )

                self.assertEqual(len(capture.requests), 1)
                self.assertEqual(
                    failure_log.read_text(encoding="utf-8"),
                    '{"action":"note","message":"harmless failure"}\n',
                )
                self.assertNotIn("test-secret", failure_log.read_text(encoding="utf-8"))

    def test_invalid_payload_does_not_reach_webhook(self) -> None:
        capture = CaptureState()
        with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
            with tempfile.TemporaryDirectory(prefix="make-bot-ui-invalid-test-") as temporary:
                with self.start_relay(target, Path(temporary) / "failures.jsonl") as local:
                    status, body = request_json(
                        local + "/send",
                        method="POST",
                        payload={"action": "status", "message": "ok", "key": "do-not-forward"},
                        headers=self.authorization_headers(),
                    )
                    self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                    self.assertEqual(body, {"error": "invalid webhook payload"})
        self.assertEqual(capture.requests, [])

    def test_unauthorized_trigger_does_not_reach_webhook(self) -> None:
        capture = CaptureState()
        with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
            with tempfile.TemporaryDirectory(prefix="make-bot-ui-auth-test-") as temporary:
                failure_log = Path(temporary) / "failures.jsonl"
                with self.start_relay(target, failure_log) as local:
                    payload = {"action": "status", "message": "must not send"}
                    status, body = request_json(local + "/send", method="POST", payload=payload)
                    self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
                    self.assertEqual(body, {"error": "unauthorized"})

                    status, body = request_json(
                        local + "/send",
                        method="POST",
                        payload=payload,
                        headers={"Authorization": "Bearer wrong-token"},
                    )
                    self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
                    self.assertEqual(body, {"error": "unauthorized"})
        self.assertEqual(capture.requests, [])
        self.assertFalse(failure_log.exists())

    def test_non_success_webhook_status_is_preserved_without_response_body(self) -> None:
        for upstream_status in (HTTPStatus.FOUND, HTTPStatus.UNAUTHORIZED, HTTPStatus.INTERNAL_SERVER_ERROR):
            with self.subTest(upstream_status=upstream_status):
                capture = CaptureState(
                    status=upstream_status,
                    response_body=b"upstream-secret-body",
                )
                with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
                    with tempfile.TemporaryDirectory(prefix="make-bot-ui-status-test-") as temporary:
                        failure_log = Path(temporary) / "failures.jsonl"
                        with self.start_relay(target, failure_log) as local:
                            status, body = request_json(
                                local + "/send",
                                method="POST",
                                payload={"action": "status", "message": "status detail"},
                                headers=self.authorization_headers(),
                            )
                        self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
                        self.assertEqual(
                            body,
                            {"error": "webhook request failed", "status": upstream_status},
                        )
                        self.assertNotIn("upstream-secret-body", json.dumps(body))
                        self.assertNotIn(
                            "test-secret",
                            failure_log.read_text(encoding="utf-8"),
                        )
                        self.assertIn("Number.isInteger(result.status)", serve.PAGE)

                self.assertEqual(len(capture.requests), 1)

    def test_default_bind_is_loopback_and_remote_bind_requires_bootstrap_auth(self) -> None:
        self.assertEqual(serve.DEFAULT_HOST, "127.0.0.1")
        with tempfile.TemporaryDirectory(prefix="make-bot-ui-bind-test-") as temporary:
            config = serve.RelayConfig(
                "https://example.invalid/webhook",
                "test-secret",
                Path(temporary) / "failures.jsonl",
                trigger_token="trigger-secret",
            )
            with self.assertRaisesRegex(ValueError, "protected configuration"):
                serve.create_server("0.0.0.0", 0, config)

    def test_remote_page_requires_auth_before_issuing_secure_trigger_cookie(self) -> None:
        capture = CaptureState()
        with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
            with tempfile.TemporaryDirectory(prefix="make-bot-ui-remote-auth-test-") as temporary:
                config = serve.RelayConfig(
                    target,
                    "test-secret",
                    Path(temporary) / "failures.jsonl",
                    timeout=1,
                    trigger_token="trigger-secret",
                    allow_local_http=True,
                    remote_auth_token="remote-bootstrap",
                    allow_remote=True,
                    secure_cookie=True,
                )
                server = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    serve.make_handler(config, remote_mode=True),
                )
                with RunningServer(server) as local:
                    unauthenticated = Request(local + "/", method="GET")
                    try:
                        urlopen(unauthenticated, timeout=2)
                    except HTTPError as error:
                        self.assertEqual(error.code, HTTPStatus.UNAUTHORIZED)
                        self.assertIsNone(error.headers.get("Set-Cookie"))
                        try:
                            self.assertEqual(json.loads(error.read()), {"error": "unauthorized"})
                        finally:
                            error.close()
                    else:
                        self.fail("remote page should require bootstrap authorization")

                    authenticated = Request(
                        local + "/",
                        method="GET",
                        headers={"Authorization": "Bearer remote-bootstrap"},
                    )
                    with urlopen(authenticated, timeout=2) as response:
                        cookie = response.headers["Set-Cookie"]
                        self.assertIn("Secure", cookie)
                        self.assertNotIn("remote-bootstrap", response.read().decode("utf-8"))

                self.assertEqual(capture.requests, [])

    def test_malformed_request_targets_return_sanitized_400_without_traceback(self) -> None:
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                with tempfile.TemporaryDirectory(prefix="make-bot-ui-target-test-") as temporary:
                    stderr = io.StringIO()
                    config = serve.RelayConfig(
                        "https://example.invalid/webhook",
                        "test-secret",
                        Path(temporary) / "failures.jsonl",
                        trigger_token="trigger-secret",
                    )
                    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(config))
                    with redirect_stderr(stderr), RunningServer(server) as local:
                        response = raw_request(
                            local,
                            f"{method} http://[::1 HTTP/1.0".encode("ascii"),
                        )
                    self.assertIn(b"400 Bad Request", response)
                    self.assertIn(b'{"error":"invalid request target"}', response)
                    self.assertNotIn(b"trigger-secret", response)
                    self.assertNotIn("Traceback", stderr.getvalue())

    def test_malformed_webhook_endpoints_return_generic_502_and_log_payload(self) -> None:
        endpoints = (
            "not-a-url",
            "http://[::1",
            "http://127.0.0.1:notaport/",
            "https:///missing-host",
            "http://example.com/webhook",
            "ftp://example.com/webhook",
        )
        payload = {"action": "status", "message": "invalid endpoint"}
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                with tempfile.TemporaryDirectory(prefix="make-bot-ui-endpoint-test-") as temporary:
                    failure_log = Path(temporary) / "failures.jsonl"
                    with self.start_relay(endpoint, failure_log) as local:
                        status, body = request_json(
                            local + "/send",
                            method="POST",
                            payload=payload,
                            headers=self.authorization_headers(),
                        )
                    self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
                    self.assertEqual(body, {"error": "webhook request failed"})
                    self.assertEqual(
                        failure_log.read_text(encoding="utf-8"),
                        '{"action":"status","message":"invalid endpoint"}\n',
                    )

    def test_control_character_sender_key_returns_502_without_traceback_or_key_log(self) -> None:
        capture = CaptureState()
        secret = "bad\nX-Leak: yes"
        with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
            with tempfile.TemporaryDirectory(prefix="make-bot-ui-key-test-") as temporary:
                failure_log = Path(temporary) / "failures.jsonl"
                stderr = io.StringIO()
                with redirect_stderr(stderr), self.start_relay(
                    target,
                    failure_log,
                    webhook_key=secret,
                ) as local:
                    status, body = request_json(
                        local + "/send",
                        method="POST",
                        payload={"action": "status", "message": "invalid key"},
                        headers=self.authorization_headers(),
                    )
                self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
                self.assertEqual(body, {"error": "webhook request failed"})
                self.assertEqual(
                    failure_log.read_text(encoding="utf-8"),
                    '{"action":"status","message":"invalid key"}\n',
                )
                self.assertNotIn("bad", stderr.getvalue())
        self.assertEqual(capture.requests, [])

    def test_https_policy_requires_explicit_loopback_http_opt_in(self) -> None:
        self.assertEqual(
            serve.validate_webhook_url("https://example.com/webhook"),
            "https://example.com/webhook",
        )
        with self.assertRaises(ValueError):
            serve.validate_webhook_url("http://127.0.0.1:8765/webhook")
        with self.assertRaises(ValueError):
            serve.validate_webhook_url("http://example.com/webhook", allow_local_http=True)
        self.assertEqual(
            serve.validate_webhook_url("http://127.0.0.1:8765/webhook", allow_local_http=True),
            "http://127.0.0.1:8765/webhook",
        )

    def test_request_construction_is_skipped_for_invalid_configuration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="make-bot-ui-validation-test-") as temporary:
            config = serve.RelayConfig(
                "not-a-url",
                "bad\r\nX-Leak: yes",
                Path(temporary) / "failures.jsonl",
                trigger_token="trigger-secret",
            )
            with patch.object(serve, "Request", side_effect=AssertionError("request must not be built")):
                result = serve.post_webhook(config, {"action": "status", "message": "validation"})
            self.assertEqual(result, serve.RelayResult(False, None))


class MakeBotUICapabilityTests(unittest.TestCase):
    def test_prefers_playwright_cli_when_present(self) -> None:
        help_text = "playwright-cli open snapshot click screenshot"
        with patch.object(capabilities.shutil, "which", return_value="/usr/local/bin/playwright-cli"):
            with patch.object(
                capabilities.subprocess,
                "run",
                return_value=capabilities.subprocess.CompletedProcess(
                    ["/usr/local/bin/playwright-cli", "--help"],
                    0,
                    stdout=help_text,
                    stderr="",
                ),
            ) as probe:
                result = capabilities.select_browser_adapter()
        probe.assert_called_once_with(
            ["/usr/local/bin/playwright-cli", "--help"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        self.assertEqual(result["adapter"], "playwright-cli")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["fallback"], "manual-browser")

    def test_false_playwright_path_falls_back_after_identity_probe(self) -> None:
        with patch.object(capabilities.shutil, "which", return_value="/bin/echo"):
            with patch.object(
                capabilities.subprocess,
                "run",
                return_value=capabilities.subprocess.CompletedProcess(
                    ["/bin/echo", "--help"],
                    0,
                    stdout="--help\n",
                    stderr="",
                ),
            ):
                result = capabilities.select_browser_adapter("/bin/echo")
        self.assertEqual(result["adapter"], "manual-browser")
        self.assertEqual(result["status"], "fallback")
        self.assertIn("playwright cli capability probe", result["reason"].lower())

    def test_playwright_identity_without_required_commands_falls_back(self) -> None:
        with patch.object(capabilities.shutil, "which", return_value="/tmp/playwright-cli"):
            with patch.object(
                capabilities.subprocess,
                "run",
                return_value=capabilities.subprocess.CompletedProcess(
                    ["/tmp/playwright-cli", "--help"],
                    0,
                    stdout="playwright-cli open snapshot\n",
                    stderr="",
                ),
            ):
                result = capabilities.select_browser_adapter()
        self.assertEqual(result["adapter"], "manual-browser")
        self.assertEqual(result["status"], "fallback")

    def test_exercises_manual_fallback_when_browser_adapter_is_missing(self) -> None:
        with patch.object(capabilities.shutil, "which", return_value=None):
            result = capabilities.select_browser_adapter()
        self.assertEqual(result["adapter"], "manual-browser")
        self.assertEqual(result["status"], "fallback")
        self.assertIn("unavailable", result["reason"])

    def test_tailscale_probe_does_not_run_when_node_is_logged_out(self) -> None:
        runner = Mock(
            return_value=capabilities.subprocess.CompletedProcess(
                ["tailscale", "status", "--json"],
                0,
                stdout='{"BackendState":"NeedsLogin"}',
                stderr="",
            )
        )
        opener = Mock()
        with patch.object(capabilities.shutil, "which", return_value="/usr/bin/tailscale"):
            result = capabilities.probe_tailscale_reachability(8765, runner=runner, opener=opener)
        self.assertEqual(result["status"], "NOT RUN")
        self.assertTrue(result["blocked"])
        runner.assert_called_once()
        opener.assert_not_called()

    def test_tailscale_probe_does_not_run_without_an_address(self) -> None:
        runner = Mock(
            side_effect=[
                capabilities.subprocess.CompletedProcess(
                    ["tailscale", "status", "--json"],
                    0,
                    stdout='{"BackendState":"Running"}',
                    stderr="",
                ),
                capabilities.subprocess.CompletedProcess(
                    ["tailscale", "ip", "-4"],
                    0,
                    stdout="",
                    stderr="",
                ),
            ]
        )
        opener = Mock()
        with patch.object(capabilities.shutil, "which", return_value="/usr/bin/tailscale"):
            result = capabilities.probe_tailscale_reachability(8765, runner=runner, opener=opener)
        self.assertEqual(result["status"], "NOT RUN")
        self.assertTrue(result["blocked"])
        self.assertEqual(runner.call_count, 2)
        opener.assert_not_called()

    def test_tailscale_probe_checks_health_only_after_login_and_address(self) -> None:
        runner = Mock(
            side_effect=[
                capabilities.subprocess.CompletedProcess(
                    ["tailscale", "status", "--json"],
                    0,
                    stdout='{"BackendState":"Running"}',
                    stderr="",
                ),
                capabilities.subprocess.CompletedProcess(
                    ["tailscale", "ip", "-4"],
                    0,
                    stdout="100.64.0.7\n",
                    stderr="",
                ),
            ]
        )
        response = Mock()
        response.getcode.return_value = HTTPStatus.OK
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b"health-secret-body"
        opener = Mock(return_value=response)
        with patch.object(capabilities.shutil, "which", return_value="/usr/bin/tailscale"):
            result = capabilities.probe_tailscale_reachability(8765, runner=runner, opener=opener)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["address"], "100.64.0.7")
        self.assertEqual(result["http_status"], HTTPStatus.OK)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "http://100.64.0.7:8765/healthz")
        self.assertNotIn("/send", request.full_url)


if __name__ == "__main__":
    unittest.main()
