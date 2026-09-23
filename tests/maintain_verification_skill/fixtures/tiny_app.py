#!/usr/bin/env python3
"""Small CLI used to exercise the maintenance receipts."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--state", type=Path, required=True)

    check = subparsers.add_parser("check")
    check.add_argument("--state", type=Path, required=True)
    check.add_argument("--expected", required=True)

    args = parser.parse_args()
    if not args.state.is_file():
        print(f"state file missing: {args.state}")
        return 2
    state = args.state.read_text(encoding="utf-8").strip()
    if args.command == "doctor":
        if state != "ready":
            print(f"unhealthy state: {state}")
            return 1
        print("tiny fixture ready")
        return 0
    if state != args.expected:
        print(f"check failed: expected={args.expected!r} actual={state!r}")
        return 1
    print(f"check passed: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
