#!/usr/bin/env python3
"""Behavior tests for the scoped Codex-native automate-me helper."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_DIR = REPO_ROOT / "skills" / "automate-me" / "scripts"
SKILL_DIR = REPO_ROOT / "skills" / "automate-me"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(HELPER_DIR))

from automate_me import (  # noqa: E402
    CollisionError,
    RoutingError,
    ScopeError,
    SkillDraft,
    UPSTREAM_COMMIT,
    confirmed_signals,
    read_allowlisted_artifacts,
    route_request,
    validate_skill_tree,
    write_generated_skill,
)


def skill_creator_initializer() -> Path | None:
    override = os.environ.get("AUTOMATE_ME_SKILL_CREATOR")
    candidates = [
        Path(override) if override else None,
        Path.home()
        / ".codex"
        / "skills"
        / ".system"
        / "skill-creator"
        / "scripts"
        / "init_skill.py",
    ]
    return next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        None,
    )


class AutomateMeBehaviorTests(unittest.TestCase):
    def test_routing_fixture_covers_skill_script_reference_and_no_artifact(self) -> None:
        cases = json.loads((FIXTURE_DIR / "requests.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {case["artifact"] for case in cases},
            {"skill", "script", "reference", "none"},
        )
        for case in cases:
            decision = route_request(case["request"])
            actual = decision.as_dict()
            for key in (
                "artifact",
                "category",
                "operation",
                "target",
                "confirmation_required",
            ):
                self.assertEqual(actual[key], case[key], case["id"])

    def test_create_and_update_are_distinct_and_conflict_is_rejected(self) -> None:
        self.assertEqual(
            route_request("Create my alex-mode skill").operation,
            "create",
        )
        self.assertEqual(
            route_request("Update my alex-mode skill").operation,
            "update",
        )
        with self.assertRaises(RoutingError):
            route_request("Create and update my alex-mode skill")

    def test_current_skill_has_frontmatter_references_and_provenance(self) -> None:
        self.assertEqual(validate_skill_tree(SKILL_DIR), ())

    def test_allowlist_scope_and_untrusted_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-scope-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            records_dir = workspace / "records"
            records_dir.mkdir(parents=True)
            marker = workspace / "DO_NOT_CREATE"
            for name in ("slice-a.md", "slice-b.md"):
                source = FIXTURE_DIR / "history" / name
                content = source.read_text(encoding="utf-8").replace(
                    "DO_NOT_CREATE", str(marker)
                )
                (records_dir / name).write_text(content, encoding="utf-8")

            records = read_allowlisted_artifacts(
                workspace,
                ["records/slice-a.md", "records/slice-b.md"],
            )
            self.assertEqual(
                [record.path for record in records],
                ["records/slice-a.md", "records/slice-b.md"],
            )
            self.assertFalse(marker.exists(), "transcript text was executed")
            self.assertEqual(
                confirmed_signals(records, ["keep replies short"]),
                ("keep replies short",),
            )
            self.assertEqual(confirmed_signals(records, ["another candidate"]), ())

            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, [])

            outside = Path(temp_dir) / "outside.md"
            outside.write_text("outside active workspace", encoding="utf-8")
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, [outside])

            escape_link = records_dir / "escape.md"
            escape_link.symlink_to(outside)
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, [escape_link])

    def test_generated_skill_uses_skill_creator_scaffold_and_validates(self) -> None:
        initializer = skill_creator_initializer()
        if initializer is None:
            self.skipTest("skill-creator initializer is not installed")
        with tempfile.TemporaryDirectory(prefix="automate-me-generate-") as temp_dir:
            temp_root = Path(temp_dir)
            skill_parent = temp_root / "project" / "skills"
            skill_parent.mkdir(parents=True)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(initializer),
                    "alex-mode",
                    "--path",
                    str(skill_parent),
                    "--resources",
                    "references",
                    "--interface",
                    "short_description=Apply Alex mode working rules",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            target = skill_parent / "alex-mode"
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex working conventions when explicitly invoked.",
                rules=(
                    "Keep replies short unless the user asks for depth.",
                    "Verify the requested behavior before reporting completion.",
                ),
            )
            result = write_generated_skill(
                target,
                draft,
                workspace_root=temp_root / "project",
                operation="update",
            )
            self.assertEqual(result.operation, "update")
            self.assertEqual(validate_skill_tree(target), ())
            provenance = (target / "references" / "provenance.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(UPSTREAM_COMMIT, provenance)
            self.assertIn("Copyright (c) 2026 Lauren Tan", provenance)
            self.assertTrue((target / "agents" / "openai.yaml").is_file())

    def test_create_collision_preserves_valid_existing_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-collision-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            target = workspace / "skills" / "alex-mode"
            (target / "references").mkdir(parents=True)
            existing_skill = """---
name: alex-mode
description: \"Existing approved mode skill.\"
---

# Alex mode

Read [existing](references/existing.md) and [provenance](references/provenance.md).
"""
            existing_reference = "# Existing reference\n"
            existing_provenance = (
                f"Source commit: {UPSTREAM_COMMIT}\n"
                "MIT License\n"
                "Copyright (c) 2026 Lauren Tan\n"
            )
            (target / "SKILL.md").write_text(existing_skill, encoding="utf-8")
            (target / "references" / "existing.md").write_text(
                existing_reference, encoding="utf-8"
            )
            (target / "references" / "provenance.md").write_text(
                existing_provenance, encoding="utf-8"
            )
            self.assertEqual(validate_skill_tree(target), ())
            before_skill = (target / "SKILL.md").read_bytes()
            before_reference = (target / "references" / "existing.md").read_bytes()
            draft = SkillDraft(
                name="alex-mode",
                description="Apply updated Alex working conventions.",
                rules=("Use short replies.",),
            )
            with self.assertRaises(CollisionError):
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="create",
                )
            self.assertEqual((target / "SKILL.md").read_bytes(), before_skill)
            self.assertEqual(
                (target / "references" / "existing.md").read_bytes(),
                before_reference,
            )

            write_generated_skill(
                target,
                draft,
                workspace_root=workspace,
                operation="update",
            )
            self.assertNotEqual((target / "SKILL.md").read_bytes(), before_skill)
            self.assertEqual(
                (target / "references" / "existing.md").read_bytes(),
                before_reference,
            )
            self.assertEqual(validate_skill_tree(target), ())

    def test_invalid_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-invalid-") as temp_dir:
            skill = Path(temp_dir) / "bad-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                """---
name: bad-skill
description: \"Bad reference\"
---

Read [outside](../outside.md).
""",
                encoding="utf-8",
            )
            errors = validate_skill_tree(skill, require_provenance=False)
            self.assertTrue(any("escapes skill directory" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
