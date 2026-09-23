#!/usr/bin/env python3
"""Check the active pstack skills for the Codex port's stated boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from skill_frontmatter import required_frontmatter_errors


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"

# These strings identify runtime seams that belong to the Cursor plugin, not the
# active Codex port. The check is intentionally small and easy to extend.
FORBIDDEN = (
    "~/.cursor",
    ".cursor/",
    "subagent_type",
    "run_in_background",
    "agent-transcripts",
    "cursor-team-kit",
    "AskQuestion",
    "create-skill",
)


def frontmatter(text: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    return text[4:end]


def main() -> int:
    errors: list[str] = []
    skill_dirs = sorted(path for path in SKILLS.iterdir() if path.is_dir())

    if not skill_dirs:
        errors.append("skills/ is empty")

    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"{skill_dir.relative_to(ROOT)} is missing SKILL.md")
            continue

        text = skill_file.read_text(encoding="utf-8")
        metadata = frontmatter(text)
        relative = skill_file.relative_to(ROOT)
        if metadata is None:
            errors.append(f"{relative} has no valid YAML frontmatter")
        else:
            for error in required_frontmatter_errors(metadata):
                errors.append(f"{relative} frontmatter {error}")

        for marker in FORBIDDEN:
            if marker in text:
                errors.append(f"{relative} contains excluded Cursor seam {marker!r}")

    if errors:
        print("Port verification failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Verified {len(skill_dirs)} active Codex skills.")
    print("No excluded Cursor seams found in active SKILL.md files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
