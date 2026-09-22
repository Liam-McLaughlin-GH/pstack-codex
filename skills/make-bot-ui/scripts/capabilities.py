#!/usr/bin/env python3
"""Select the available local browser adapter without changing machine state."""

from __future__ import annotations

import argparse
import json
import shutil
from typing import Any


def select_browser_adapter(command: str = "playwright-cli") -> dict[str, Any]:
    """Prefer Playwright CLI and expose a truthful manual-browser fallback."""

    path = shutil.which(command)
    if path:
        return {
            "adapter": "playwright-cli",
            "status": "available",
            "path": path,
            "fallback": "manual-browser",
        }
    return {
        "adapter": "manual-browser",
        "status": "fallback",
        "path": None,
        "reason": f"{command} is unavailable; open the local URL manually",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-command", default="playwright-cli")
    args = parser.parse_args()
    print(json.dumps(select_browser_adapter(args.browser_command), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
