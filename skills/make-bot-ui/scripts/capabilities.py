#!/usr/bin/env python3
"""Select the available local browser adapter without changing machine state."""

from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REQUIRED_PLAYWRIGHT_COMMANDS = ("open", "snapshot", "click", "screenshot")
TAILSCALE_COMMAND = "tailscale"


def _probe_playwright_cli(path: str) -> bool:
    try:
        result = subprocess.run(
            [path, "--help"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    help_text = f"{result.stdout}\n{result.stderr}".lower()
    return (
        result.returncode == 0
        and "playwright-cli" in help_text
        and all(command in help_text for command in REQUIRED_PLAYWRIGHT_COMMANDS)
    )


def select_browser_adapter(command: str = "playwright-cli") -> dict[str, Any]:
    """Prefer Playwright CLI and expose a truthful manual-browser fallback."""

    path = shutil.which(command)
    if path and _probe_playwright_cli(path):
        return {
            "adapter": "playwright-cli",
            "status": "available",
            "path": path,
            "fallback": "manual-browser",
        }
    reason = (
        f"{command} failed the Playwright CLI capability probe; open the local URL manually"
        if path
        else f"{command} is unavailable; open the local URL manually"
    )
    return {
        "adapter": "manual-browser",
        "status": "fallback",
        "path": None,
        "reason": reason,
    }


def _not_run(reason: str) -> dict[str, Any]:
    return {"status": "NOT RUN", "blocked": True, "reason": reason}


def _first_ipv4(output: str | None) -> str | None:
    for candidate in (output or "").split():
        try:
            address = ipaddress.ip_address(candidate.split("/", 1)[0])
        except ValueError:
            continue
        if address.version == 4:
            return str(address)
    return None


def probe_tailscale_reachability(
    port: int,
    *,
    command: str = TAILSCALE_COMMAND,
    timeout: float = 2.0,
    runner: Any = None,
    opener: Any = None,
) -> dict[str, Any]:
    """Probe only an already logged-in Tailscale node and its local health page."""

    if not isinstance(port, int) or not 1 <= port <= 65535:
        return _not_run("invalid local port")
    path = shutil.which(command)
    if not path:
        return _not_run("Tailscale is unavailable")
    runner = runner or subprocess.run
    opener = opener or urlopen
    try:
        status_result = runner(
            [path, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return _not_run("Tailscale status could not be read")
    if status_result.returncode != 0:
        return _not_run("Tailscale status could not be read")
    try:
        status = json.loads(status_result.stdout)
    except (TypeError, ValueError):
        return _not_run("Tailscale status could not be read")
    if not isinstance(status, dict) or str(status.get("BackendState", "")).casefold() != "running":
        return _not_run("Tailscale is not logged in")

    try:
        ip_result = runner(
            [path, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return _not_run("Tailscale IPv4 address could not be read")
    if ip_result.returncode != 0:
        return _not_run("Tailscale IPv4 address could not be read")
    address = _first_ipv4(ip_result.stdout)
    if address is None:
        return _not_run("Tailscale has no IPv4 address")

    probe_url = f"http://{address}:{port}/healthz"
    try:
        with opener(Request(probe_url, method="GET"), timeout=timeout) as response:
            http_status = response.getcode()
            response.read()
    except HTTPError as error:
        http_status = error.code if isinstance(error.code, int) else None
        error.close()
        return {
            "status": "FAIL",
            "blocked": False,
            "address": address,
            "http_status": http_status,
            "reason": "Tailscale health check returned a non-success status",
        }
    except (OSError, URLError, subprocess.SubprocessError, TypeError, ValueError):
        return {
            "status": "FAIL",
            "blocked": False,
            "address": address,
            "http_status": None,
            "reason": "Tailscale health check failed",
        }
    return {
        "status": "PASS" if http_status == 200 else "FAIL",
        "blocked": False,
        "address": address,
        "http_status": http_status,
        "reason": "Tailscale health check reached /healthz",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-command", default="playwright-cli")
    parser.add_argument(
        "--tailscale-port",
        type=int,
        help="probe the local /healthz page over Tailscale when the node is online",
    )
    args = parser.parse_args()
    result: dict[str, Any] = select_browser_adapter(args.browser_command)
    if args.tailscale_port is not None:
        result = {
            "browser": result,
            "tailscale": probe_tailscale_reachability(args.tailscale_port),
        }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
