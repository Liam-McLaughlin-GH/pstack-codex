#!/usr/bin/env python3
"""Serve the make-bot-ui page and relay its payload server-side."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PAYLOAD_FIELDS = ("action", "message")
MAX_BODY_BYTES = 16 * 1024
WEBHOOK_URL_ENV = "MAKE_BOT_UI_WEBHOOK_URL"
WEBHOOK_KEY_ENV = "MAKE_BOT_UI_WEBHOOK_KEY"


@dataclass(frozen=True)
class RelayConfig:
    webhook_url: str
    webhook_key: str
    failure_log: Path
    timeout: float = 8.0


@dataclass(frozen=True)
class RelayResult:
    ok: bool
    status: int | None


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = build_opener(NoRedirectHandler)


def validate_payload(value: Any) -> dict[str, str]:
    """Validate the fixture's two-field webhook contract without echoing data."""

    if not isinstance(value, dict):
        raise ValueError("payload must be a JSON object")
    actual = set(value)
    expected = set(PAYLOAD_FIELDS)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise ValueError(f"payload is missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"payload has unexpected fields: {', '.join(sorted(extra))}")
    if any(not isinstance(value[field], str) or not value[field].strip() for field in PAYLOAD_FIELDS):
        raise ValueError("payload fields must be non-empty strings")
    return {field: value[field] for field in PAYLOAD_FIELDS}


def append_failure(failure_log: Path, payload: dict[str, str]) -> None:
    failure_log.parent.mkdir(parents=True, exist_ok=True)
    with failure_log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


def post_webhook(
    config: RelayConfig,
    payload: dict[str, str],
    opener: Callable[..., Any] = _OPENER.open,
) -> RelayResult:
    """Send exactly one request and record only the payload when it fails."""

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    request = Request(
        config.webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.webhook_key}",
            "X-Automation-Key": config.webhook_key,
        },
        method="POST",
    )
    try:
        with opener(request, timeout=config.timeout) as response:
            status = response.getcode()
            response.read()
    except HTTPError as error:
        error.close()
        append_failure(config.failure_log, payload)
        return RelayResult(False, None)
    except (OSError, TimeoutError, URLError):
        append_failure(config.failure_log, payload)
        return RelayResult(False, None)
    if status != HTTPStatus.OK:
        append_failure(config.failure_log, payload)
        return RelayResult(False, status)
    return RelayResult(True, status)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>Make bot UI</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #182230; }
    main { max-width: 42rem; margin: 4rem auto; padding: 2rem; background: white;
      border: 1px solid #d9e0ea; border-radius: 1rem; box-shadow: 0 1rem 3rem #243b5314; }
    h1 { margin-top: 0; }
    .actions { display: flex; gap: .75rem; flex-wrap: wrap; }
    button { border: 0; border-radius: .6rem; padding: .7rem 1rem; background: #2457d6;
      color: white; cursor: pointer; font: inherit; }
    button:hover { background: #173fa7; }
    label { display: block; margin: 1.25rem 0 .4rem; font-weight: 600; }
    input { width: 100%; box-sizing: border-box; padding: .7rem; border: 1px solid #b8c4d4;
      border-radius: .5rem; font: inherit; }
    #status { min-height: 1.5rem; margin-top: 1.5rem; font-weight: 600; }
  </style>
</head>
<body>
  <main>
    <h1>Wake the bot</h1>
    <p>This page sends a small JSON message through the local server.</p>
    <div class="actions">
      <button type="button" data-action="status">Send status check</button>
    </div>
    <form id="note-form">
      <label for="message">Message</label>
      <input id="message" name="message" value="Hello from the local UI" autocomplete="off">
      <button type="submit">Send note</button>
    </form>
    <p id="status" role="status" aria-live="polite">Ready</p>
  </main>
  <script>
    const statusNode = document.querySelector("#status");
    async function send(action, message) {
      statusNode.textContent = "Sending...";
      try {
        const response = await fetch("/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, message })
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || "Webhook request failed");
        statusNode.textContent = `Webhook accepted (${result.status})`;
      } catch (error) {
        statusNode.textContent = error instanceof Error ? error.message : "Webhook request failed";
      }
    }
    document.querySelector("[data-action='status']").addEventListener("click", () =>
      send("status", "status check"));
    document.querySelector("#note-form").addEventListener("submit", event => {
      event.preventDefault();
      send("note", document.querySelector("#message").value);
    });
  </script>
</body>
</html>
"""


def make_handler(config: RelayConfig) -> type[BaseHTTPRequestHandler]:
    class RelayHandler(BaseHTTPRequestHandler):
        server_version = "make-bot-ui/1"

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            path = urlsplit(self.path).path
            if path == "/":
                self._send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/healthz":
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if urlsplit(self.path).path != "/send":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            content_length = self.headers.get("Content-Length")
            try:
                length = int(content_length or "-1")
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request size"})
                return
            try:
                value = json.loads(self.rfile.read(length))
                payload = validate_payload(value)
            except (ValueError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid webhook payload"})
                return

            result = post_webhook(config, payload)
            if result.ok:
                self._send_json(HTTPStatus.OK, {"ok": True, "status": result.status})
            else:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "webhook request failed"})

        def _send_bytes(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            body = json.dumps(value, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return RelayHandler


def create_server(host: str, port: int, config: RelayConfig) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(config))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--webhook-url", default=os.environ.get(WEBHOOK_URL_ENV))
    parser.add_argument("--failure-log", type=Path, default=Path(".make-bot-ui-failures.jsonl"))
    args = parser.parse_args()
    if not args.webhook_url or not os.environ.get(WEBHOOK_KEY_ENV):
        parser.error(f"set {WEBHOOK_URL_ENV} and {WEBHOOK_KEY_ENV}; the key is read from the environment only")

    server = create_server(
        args.host,
        args.port,
        RelayConfig(args.webhook_url, os.environ[WEBHOOK_KEY_ENV], args.failure_log),
    )
    print(f"make-bot-ui listening on {args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
