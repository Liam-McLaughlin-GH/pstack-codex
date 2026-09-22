#!/usr/bin/env python3
"""Select the available local browser adapter without changing machine state."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from typing import Any


REQUIRED_PLAYWRIGHT_COMMANDS = ("open", "snapshot", "click", "screenshot")


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-command", default="playwright-cli")
    args = parser.parse_args()
    print(json.dumps(select_browser_adapter(args.browser_command), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
