from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch


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
    def __init__(self, status: int = HTTPStatus.OK) -> None:
        self.status = status
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
            self.send_header("Content-Length", "0")
            self.end_headers()

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


def request_json(url: str, *, method: str = "GET", payload: dict[str, str] | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        finally:
            error.close()


def request_text(url: str) -> tuple[int, str]:
    with urlopen(Request(url, method="GET"), timeout=2) as response:
        return response.status, response.read().decode("utf-8")


class MakeBotUIServerTests(unittest.TestCase):
    def start_relay(self, target: str, failure_log: Path) -> RunningServer:
        config = serve.RelayConfig(target, "test-secret", failure_log, timeout=1)
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(config))
        return RunningServer(server)

    def test_page_and_click_relay_json_with_server_side_headers(self) -> None:
        capture = CaptureState()
        with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
            with tempfile.TemporaryDirectory(prefix="make-bot-ui-test-") as temporary:
                failure_log = Path(temporary) / "failures.jsonl"
                with self.start_relay(target, failure_log) as local:
                    status, page = request_text(local + "/")
                    self.assertEqual(status, HTTPStatus.OK)
                    self.assertIn("Send status check", page)
                    self.assertNotIn(target, page)
                    self.assertNotIn("test-secret", page)

                    request = Request(local + "/send", method="POST", data=json.dumps({
                        "action": "status",
                        "message": "status check",
                    }).encode("utf-8"), headers={"Content-Type": "application/json"})
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
        capture = CaptureState(status=HTTPStatus.BAD_GATEWAY)
        with RunningServer(ThreadingHTTPServer(("127.0.0.1", 0), capture_handler(capture))) as target:
            with tempfile.TemporaryDirectory(prefix="make-bot-ui-failure-test-") as temporary:
                failure_log = Path(temporary) / "nested" / "failures.jsonl"
                with self.start_relay(target, failure_log) as local:
                    status, body = request_json(
                        local + "/send",
                        method="POST",
                        payload={"action": "note", "message": "harmless failure"},
                    )
                    self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
                    self.assertEqual(body, {"error": "webhook request failed"})

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
                    )
                    self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                    self.assertEqual(body, {"error": "invalid webhook payload"})
        self.assertEqual(capture.requests, [])


class MakeBotUICapabilityTests(unittest.TestCase):
    def test_prefers_playwright_cli_when_present(self) -> None:
        with patch.object(capabilities.shutil, "which", return_value="/usr/local/bin/playwright-cli"):
            result = capabilities.select_browser_adapter()
        self.assertEqual(result["adapter"], "playwright-cli")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["fallback"], "manual-browser")

    def test_exercises_manual_fallback_when_browser_adapter_is_missing(self) -> None:
        with patch.object(capabilities.shutil, "which", return_value=None):
            result = capabilities.select_browser_adapter()
        self.assertEqual(result["adapter"], "manual-browser")
        self.assertEqual(result["status"], "fallback")
        self.assertIn("unavailable", result["reason"])


if __name__ == "__main__":
    unittest.main()
