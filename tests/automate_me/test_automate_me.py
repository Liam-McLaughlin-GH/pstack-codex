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
    PreferenceApproval,
    RoutingError,
    ScopeError,
    SkillDraft,
    UPSTREAM_COMMIT,
    ValidationError,
    confirmed_signals,
    discover_existing_skills,
    is_skill_discoverable,
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

    def test_skill_route_target_is_a_directory_and_end_to_end_output_is_discoverable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-e2e-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            records_dir = workspace / "records"
            records_dir.mkdir(parents=True)
            for name in ("slice-a.md", "slice-b.md"):
                (records_dir / name).write_text(
                    (FIXTURE_DIR / "history" / name).read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            decision = route_request("Create my alex-mode skill from approved evidence")
            self.assertEqual(decision.target, "skills/alex-mode")
            evidence = read_allowlisted_artifacts(
                workspace, ["records/slice-a.md", "records/slice-b.md"]
            )
            result = write_generated_skill(
                decision.target,
                SkillDraft(
                    name="alex-mode",
                    description="Apply approved Alex working conventions.",
                    rules=("Keep replies short.",),
                    evidence=evidence,
                ),
                workspace_root=workspace,
                operation=decision.operation,
                approval=PreferenceApproval(
                    approved=True,
                    intent="create",
                    skill_name="alex-mode",
                    signals=("keep replies short",),
                ),
            )
            target = workspace / decision.target
            self.assertEqual(result.target, "skills/alex-mode")
            self.assertEqual(validate_skill_tree(target), ())
            self.assertTrue(is_skill_discoverable(target))
            discovered = discover_existing_skills(workspace, name="alex-mode")
            self.assertEqual([item.directory for item in discovered], ["skills/alex-mode"])

    def test_ordinary_mode_words_and_existing_workflow_use_do_not_create_personal_mode(
        self,
    ) -> None:
        for request in (
            "Fix the dark-mode CSS bug",
            "Create a dark-mode skill for the UI",
            "Review the alex-mode pull request",
            "Use the existing checkout workflow",
            "Do not create my alex-mode skill",
        ):
            decision = route_request(request)
            self.assertEqual(decision.artifact, "none", request)

    def test_existing_discovery_is_scoped_and_update_intent_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-discovery-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            workspace.mkdir()
            self.assertEqual(discover_existing_skills(workspace), ())
            self.assertEqual(
                route_request("Create my alex-mode skill").operation,
                "create",
            )
            self.assertEqual(
                route_request("Update my alex-mode skill").operation,
                "update",
            )
            self.assertEqual(
                route_request("Refresh my alex-mode skill").operation,
                "update",
            )
            self.assertEqual(route_request("Existing alex-mode skill").artifact, "none")

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

    def test_artifacts_require_unique_files_and_distinct_slice_identities(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-records-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            records_dir = workspace / "records"
            records_dir.mkdir(parents=True)
            first = records_dir / "first.md"
            second = records_dir / "second.md"
            first.write_text("slice_id: same-slice\nPreference: keep replies short\n")
            second.write_text("slice_id: same-slice\nPreference: keep replies short\n")
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, ["records/first.md", "records/./first.md"])
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, ["records/first.md", "records/second.md"])
            slice_dir = records_dir / "slice-c"
            slice_dir.mkdir()
            (slice_dir / "part-one.md").write_text("Preference: keep replies short\n")
            (slice_dir / "part-two.md").write_text("Preference: keep replies short\n")
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(
                    workspace,
                    ["records/slice-c/part-one.md", "records/slice-c/part-two.md"],
                )

    def test_signal_confirmation_ignores_raw_substrings_quotes_and_negation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-signals-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            records_dir = workspace / "records"
            records_dir.mkdir(parents=True)
            values = (
                "The user said keep replies short somewhere in a transcript.\n",
                'Preference candidate: "keep replies short"\n',
                "Preference candidate: do not keep replies short\n",
            )
            paths = []
            for index, value in enumerate(values):
                path = records_dir / f"slice-{index}.md"
                path.write_text(value, encoding="utf-8")
                paths.append(path.relative_to(workspace))
            records = read_allowlisted_artifacts(workspace, paths)
            self.assertEqual(confirmed_signals(records[:2], ["keep replies short"]), ())
            self.assertEqual(confirmed_signals(records[1:], ["keep replies short"]), ())
            positive = []
            for index in (3, 4):
                path = records_dir / f"positive-{index}.md"
                path.write_text("Preference candidate: keep replies short.\n", encoding="utf-8")
                positive.append(path.relative_to(workspace))
            positive_records = read_allowlisted_artifacts(workspace, positive)
            self.assertEqual(
                confirmed_signals(positive_records, ["keep replies short"]),
                ("keep replies short",),
            )

    def test_history_has_per_record_and_aggregate_bounds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-bounds-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            records_dir = workspace / "records"
            records_dir.mkdir(parents=True)
            paths = []
            for index in range(9):
                path = records_dir / f"slice-{index}.md"
                path.write_text("Preference: keep replies short\n", encoding="utf-8")
                paths.append(path.relative_to(workspace))
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, paths)
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(
                    workspace,
                    ["records/slice-0.md", "records/slice-1.md"],
                    max_total_bytes=10,
                )
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(
                    workspace,
                    ["records/slice-0.md"],
                    max_bytes=1,
                )
            (records_dir / "old.md").write_text(
                "slice_id: old\ncaptured_at: 2020-01-01T00:00:00Z\n",
                encoding="utf-8",
            )
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, ["records/old.md"])

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
                evidence=self._write_evidence(temp_root / "project"),
            )
            result = write_generated_skill(
                target,
                draft,
                workspace_root=temp_root / "project",
                operation="update",
                approval=PreferenceApproval(
                    approved=True,
                    intent="update",
                    skill_name="alex-mode",
                    signals=("keep replies short",),
                ),
            )
            self.assertEqual(result.operation, "update")
            self.assertEqual(validate_skill_tree(target), ())
            provenance = (target / "references" / "provenance.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(UPSTREAM_COMMIT, provenance)
            self.assertIn("Copyright (c) 2026 Lauren Tan", provenance)
            self.assertTrue((target / "agents" / "openai.yaml").is_file())

    def test_update_preserves_approved_managed_and_unrelated_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-preserve-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            target = workspace / "skills" / "alex-mode"
            (target / "references").mkdir(parents=True)
            (target / "agents").mkdir(parents=True)
            existing_skill = f'''---
name: alex-mode
description: "Existing approved mode skill."
---

# Alex mode

## Approved local exception

Keep the release checklist visible.

Read [provenance](references/provenance.md).
'''
            working_style = "# Approved working style\n\nKeep the checklist visible.\n"
            provenance = f"Source commit: {UPSTREAM_COMMIT}\nMIT License\nCopyright (c) 2026 Lauren Tan\n"
            openai_yaml = "interface:\n  display_name: Approved Alex\n"
            (target / "SKILL.md").write_text(existing_skill, encoding="utf-8")
            (target / "references" / "working-style.md").write_text(
                working_style, encoding="utf-8"
            )
            (target / "references" / "provenance.md").write_text(
                provenance, encoding="utf-8"
            )
            (target / "agents" / "openai.yaml").write_text(openai_yaml, encoding="utf-8")
            evidence = self._write_evidence(workspace)
            write_generated_skill(
                target,
                SkillDraft(
                    name="alex-mode",
                    description="Apply updated Alex working conventions.",
                    rules=("Keep replies short.",),
                    evidence=evidence,
                ),
                workspace_root=workspace,
                operation="update",
                approval=PreferenceApproval(
                    approved=True,
                    intent="update",
                    skill_name="alex-mode",
                    signals=("keep replies short",),
                ),
            )
            self.assertIn("Approved local exception", (target / "SKILL.md").read_text())
            self.assertIn("Keep the release checklist visible", (target / "SKILL.md").read_text())
            self.assertEqual((target / "references" / "working-style.md").read_text(), working_style)
            self.assertEqual((target / "agents" / "openai.yaml").read_text(), openai_yaml)
            self.assertEqual(validate_skill_tree(target), ())

    def test_skill_directory_basename_and_frontmatter_name_must_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-invariant-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            target = workspace / "skills" / "alex-mode"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(
                '---\nname: other-mode\ndescription: "Mismatch"\n---\n',
                encoding="utf-8",
            )
            self.assertTrue(any("directory basename" in error for error in validate_skill_tree(target)))
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    target,
                    SkillDraft(
                        name="other-mode",
                        description="Mismatch",
                        rules=("Use the approved rule.",),
                    ),
                    workspace_root=workspace,
                    operation="update",
                )

    def test_generation_requires_structured_approval_and_two_slices(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-approval-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence_dir = workspace / "records"
            evidence_dir.mkdir(parents=True)
            for name in ("slice-a.md", "slice-b.md"):
                (evidence_dir / name).write_text(
                    "Preference candidate: keep replies short.\n", encoding="utf-8"
                )
            evidence = read_allowlisted_artifacts(
                workspace, ["records/slice-a.md", "records/slice-b.md"]
            )
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex working conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "skills/alex-mode",
                    draft,
                    workspace_root=workspace,
                    operation="create",
                )
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "skills/alex-mode",
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=PreferenceApproval(
                        approved=False,
                        intent="create",
                        skill_name="alex-mode",
                        signals=("keep replies short",),
                    ),
                )
            one_record_draft = SkillDraft(
                name="alex-mode",
                description=draft.description,
                rules=draft.rules,
                evidence=evidence[:1],
            )
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "skills/alex-mode",
                    one_record_draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=PreferenceApproval(
                        approved=True,
                        intent="create",
                        skill_name="alex-mode",
                        signals=("keep replies short",),
                    ),
                )

    def test_relative_links_reject_absolute_and_protocol_relative_targets(self) -> None:
        for link in ("/tmp/outside.md", "//example.com/outside.md", "https://example.com/doc.md"):
            with tempfile.TemporaryDirectory(prefix="automate-me-links-") as temp_dir:
                skill = Path(temp_dir) / "safe-skill"
                skill.mkdir()
                (skill / "SKILL.md").write_text(
                    f'---\nname: safe-skill\ndescription: "Safe"\n---\n\nRead [link]({link}).\n',
                    encoding="utf-8",
                )
                errors = validate_skill_tree(skill, require_provenance=False)
                self.assertTrue(any("link" in error for error in errors), link)

    def test_failed_update_rolls_back_and_symlinked_tree_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-rollback-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex working conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )
            approval = PreferenceApproval(
                approved=True,
                intent="create",
                skill_name="alex-mode",
                signals=("keep replies short",),
            )
            target = workspace / "skills" / "alex-mode"
            write_generated_skill(
                target,
                draft,
                workspace_root=workspace,
                operation="create",
                approval=approval,
            )
            before = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            with self.assertRaises(OSError):
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="update",
                    approval=PreferenceApproval(
                        approved=True,
                        intent="update",
                        skill_name="alex-mode",
                        signals=("keep replies short",),
                    ),
                    failure_injector=lambda point: (
                        (_ for _ in ()).throw(OSError("injected install failure"))
                        if point == "after_backup"
                        else None
                    ),
                )
            after = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertFalse(any(path.name.startswith(".alex-mode.") for path in target.parent.iterdir()))

            outside = workspace / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            (target / "references" / "alias.md").symlink_to(outside)
            with self.assertRaises(ScopeError):
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="update",
                    approval=PreferenceApproval(
                        approved=True,
                        intent="update",
                        skill_name="alex-mode",
                        signals=("keep replies short",),
                    ),
                )

    def _write_evidence(self, workspace: Path):
        records_dir = workspace / "records"
        records_dir.mkdir(parents=True, exist_ok=True)
        for name in ("slice-a.md", "slice-b.md"):
            (records_dir / name).write_text(
                "Preference candidate: keep replies short.\n", encoding="utf-8"
            )
        return read_allowlisted_artifacts(
            workspace, ["records/slice-a.md", "records/slice-b.md"]
        )

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
                evidence=self._write_evidence(workspace),
            )
            with self.assertRaises(CollisionError):
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=PreferenceApproval(
                        approved=True,
                        intent="create",
                        skill_name="alex-mode",
                        signals=("keep replies short",),
                    ),
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
                approval=PreferenceApproval(
                    approved=True,
                    intent="update",
                    skill_name="alex-mode",
                    signals=("keep replies short",),
                ),
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
