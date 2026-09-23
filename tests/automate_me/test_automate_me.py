#!/usr/bin/env python3
"""Behavior tests for the scoped Codex-native automate-me helper."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_DIR = REPO_ROOT / "skills" / "automate-me" / "scripts"
SKILL_DIR = REPO_ROOT / "skills" / "automate-me"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(HELPER_DIR))

from automate_me import (  # noqa: E402
    ArtifactRecord,
    CollisionError,
    PreferenceApproval,
    PreferenceInteraction,
    PreferenceQuestion,
    PreferenceResponse,
    PublicationRecoveryError,
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
    validate_skill_discoverability,
    validate_skill_tree,
    write_generated_skill,
)
import automate_me  # noqa: E402


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
    def _approval(self, intent: str, *, status: str = "accepted", approved: bool = True):
        interaction = PreferenceInteraction(
            questions=(
                PreferenceQuestion(
                    question_id="focus",
                    prompt="Which conventions should be captured?",
                    options=("Replies", "Verification", "Process", "Other"),
                    allow_multiple=True,
                ),
                PreferenceQuestion(
                    question_id="follow_up",
                    prompt="Which result matters most?",
                    options=("Short", "Thorough", "Fast", "Explicit"),
                    follow_up_for="focus",
                ),
            ),
            free_form_prompt="What else should the generated skill preserve?",
        )
        response = PreferenceResponse(
            status=status,
            answers={
                "focus": ("Replies",),
                "follow_up": ("Short",),
            }
            if status == "accepted"
            else {},
            free_form="Keep replies short." if status == "accepted" else "",
        )
        return PreferenceApproval(
            approved=approved,
            intent=intent,
            skill_name="alex-mode",
            signals=("keep replies short",),
            interaction=interaction,
            response=response,
        )

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
                approval=self._approval("create"),
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

    def test_negated_automate_me_and_intervening_punctuation_are_routed_safely(self) -> None:
        for request in (
            "Do not automate me.",
            "Please don't, under any circumstances, automate me!",
            "I do not want you to automate me; route this elsewhere.",
            "Do not, under any circumstances, create my alex-mode skill.",
            "The automate-me helper is already installed.",
        ):
            decision = route_request(request)
            self.assertEqual(decision.artifact, "none", request)
            self.assertEqual(decision.operation, "none", request)

        decision = route_request("Automate me—create my alex-mode skill, please.")
        self.assertEqual(decision.artifact, "skill")
        self.assertEqual(decision.operation, "create")
        self.assertEqual(decision.target, "skills/alex-mode")

    def test_hyphenated_invocation_invalid_names_and_positive_clauses(self) -> None:
        decision = route_request("Automate-me create my alex-mode skill")
        self.assertEqual(decision.artifact, "skill")
        self.assertEqual(decision.operation, "create")
        self.assertEqual(decision.target, "skills/alex-mode")

        positive_clause = route_request(
            "Do not automate-me, but create my alex-mode skill."
        )
        self.assertEqual(positive_clause.artifact, "skill")
        self.assertEqual(positive_clause.operation, "create")
        self.assertEqual(positive_clause.target, "skills/alex-mode")

        for request in (
            "Automate me create a skill named foo.bar",
            "Automate me create my foo.bar-mode skill",
            "Create a project-local verification skill named foo.bar",
        ):
            with self.subTest(request=request), self.assertRaises(RoutingError):
                route_request(request)

    def test_preference_interaction_requires_bounded_questions_and_response_states(self) -> None:
        with self.assertRaises(ValidationError):
            PreferenceQuestion(
                question_id="focus",
                prompt="Too few choices",
                options=("One", "Two", "Three"),
                allow_multiple=True,
            )
        with self.assertRaises(ValidationError):
            PreferenceInteraction(
                questions=(
                    PreferenceQuestion(
                        question_id="focus",
                        prompt="Choose focus",
                        options=("One", "Two", "Three", "Four"),
                        allow_multiple=True,
                    ),
                    PreferenceQuestion(
                        question_id="follow_up",
                        prompt="Choose detail",
                        options=("One", "Two", "Three", "Four"),
                        follow_up_for="wrong-question",
                    ),
                ),
                free_form_prompt="Anything else?",
            )

        interaction = PreferenceInteraction(
            questions=(
                PreferenceQuestion(
                    question_id="focus",
                    prompt="Choose focus",
                    options=("One", "Two", "Three", "Four"),
                    allow_multiple=True,
                ),
            ),
            free_form_prompt="Anything else?",
        )
        with self.assertRaises(ValidationError):
            PreferenceApproval(
                approved=True,
                intent="create",
                skill_name="alex-mode",
                signals=("keep replies short",),
                interaction=interaction,
                response=PreferenceResponse(
                    status="accepted", answers={}, free_form=""
                ),
            )

        declined = PreferenceApproval(
            approved=False,
            intent="create",
            skill_name="alex-mode",
            signals=("keep replies short",),
            interaction=interaction,
            response=PreferenceResponse(status="declined", answers={}),
        )
        with tempfile.TemporaryDirectory(prefix="automate-me-declined-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "skills/alex-mode",
                    SkillDraft(
                        name="alex-mode",
                        description="Apply approved Alex conventions.",
                        rules=("Keep replies short.",),
                        evidence=evidence,
                    ),
                    workspace_root=workspace,
                    operation="create",
                    approval=declined,
                )

        accepted = self._approval("create")
        self.assertEqual(accepted.response.status, "accepted")
        self.assertEqual(accepted.interaction.validate_response(accepted.response), ())

    def test_preference_approval_mapping_requires_the_full_interaction(self) -> None:
        base = {
            "approved": True,
            "intent": "create",
            "skill_name": "alex-mode",
            "signals": ["keep replies short"],
        }
        with self.assertRaises(ValidationError):
            PreferenceApproval.from_mapping(base)
        base["interaction"] = {
            "questions": [
                {
                    "id": "focus",
                    "prompt": "Choose focus",
                    "options": ["Replies", "Verification", "Process", "Other"],
                    "allow_multiple": True,
                }
            ],
            "free_form_prompt": "Anything else?",
        }
        base["response"] = {
            "status": "accepted",
            "answers": {"focus": ["Replies"]},
            "free_form": "Keep replies short.",
        }
        parsed = PreferenceApproval.from_mapping(base)
        self.assertTrue(parsed.approved)

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

    def test_allowlist_uses_one_lexical_identity_for_relative_absolute_and_parent_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-lexical-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            records_dir = workspace / "records"
            records_dir.mkdir(parents=True)
            record = records_dir / "slice-a.md"
            record.write_text("slice_id: a\nPreference: keep replies short\n", encoding="utf-8")
            absolute = record.absolute()
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, ["records/slice-a.md", absolute])

            outside = Path(temp_dir) / "outside-records"
            outside.mkdir()
            (outside / "slice-b.md").write_text(
                "Preference: keep replies short\n", encoding="utf-8"
            )
            (records_dir / "alias-parent").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, ["records/alias-parent/slice-b.md"])

            with self.assertRaises(ScopeError):
                read_allowlisted_artifacts(workspace, [Path(temp_dir) / "outside.md"])

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
            fenced_paths = []
            for index, fence in enumerate(("```", "~~~")):
                path = records_dir / f"fenced-{index}.md"
                path.write_text(
                    f"{fence}markdown\n"
                    "Preference candidate: keep replies short.\n"
                    f"{fence}\n",
                    encoding="utf-8",
                )
                fenced_paths.append(path.relative_to(workspace))
            fenced_records = read_allowlisted_artifacts(workspace, fenced_paths)
            self.assertEqual(
                confirmed_signals(fenced_records, ["keep replies short"]),
                (),
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
                approval=self._approval("update"),
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
                approval=self._approval("update"),
            )
            self.assertIn("Approved local exception", (target / "SKILL.md").read_text())
            self.assertIn("Keep the release checklist visible", (target / "SKILL.md").read_text())
            updated_working_style = (target / "references" / "working-style.md").read_text()
            self.assertIn("Keep the checklist visible", updated_working_style)
            self.assertIn("- Keep replies short.", updated_working_style)
            self.assertIn("records/slice-a.md", updated_working_style)
            self.assertNotEqual(updated_working_style, working_style)
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
                    approval=self._approval("create", status="declined", approved=False),
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
                    approval=self._approval("create"),
                )

    def test_generation_rejects_forged_stale_and_missing_allowlisted_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-evidence-integrity-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex working conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )

            forged = ArtifactRecord(
                path=evidence[0].path,
                text=evidence[0].text,
                sha256=evidence[0].sha256,
                line_count=evidence[0].line_count,
                size_bytes=evidence[0].size_bytes,
                slice_id=evidence[0].slice_id,
                captured_at=evidence[0].captured_at,
            )
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "skills/alex-mode",
                    SkillDraft(
                        name=draft.name,
                        description=draft.description,
                        rules=draft.rules,
                        evidence=(forged, evidence[1]),
                    ),
                    workspace_root=workspace,
                    operation="create",
                    approval=self._approval("create"),
                )

            (workspace / evidence[0].path).write_text(
                "Preference candidate: keep replies long.\n", encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "skills/alex-mode",
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=self._approval("create"),
                )

            (workspace / evidence[0].path).unlink()
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "skills/alex-mode",
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=self._approval("create"),
                )

    def test_relative_links_reject_absolute_and_protocol_relative_targets(self) -> None:
        for link in (
            "/tmp/outside.md",
            "//example.com/outside.md",
            "https://example.com/doc.md",
            "http://[::1",
            "",
        ):
            with tempfile.TemporaryDirectory(prefix="automate-me-links-") as temp_dir:
                skill = Path(temp_dir) / "safe-skill"
                skill.mkdir()
                (skill / "SKILL.md").write_text(
                    f'---\nname: safe-skill\ndescription: "Safe"\n---\n\nRead [link]({link}).\n',
                    encoding="utf-8",
                )
                errors = validate_skill_tree(skill, require_provenance=False)
                self.assertTrue(any("link" in error for error in errors), link)

    def test_markdown_link_validation_skips_backtick_and_tilde_fences(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-fenced-links-") as temp_dir:
            skill = Path(temp_dir) / "safe-skill"
            (skill / "references").mkdir(parents=True)
            (skill / "references" / "real.md").write_text(
                "# Real reference\n", encoding="utf-8"
            )
            body = """Read [real](references/real.md) and [named][real-reference].

[real-reference]: references/real.md

```markdown
[missing-inline](references/missing-inline.md)
[missing-reference]: references/missing-reference.md
```

~~~text
[missing-tilde](references/missing-tilde.md)
~~~

    [missing-indented](references/missing-indented.md)
	[missing-tab](references/missing-tab.md)
"""
            (skill / "SKILL.md").write_text(
                '---\nname: safe-skill\ndescription: "Safe"\n---\n\n' + body,
                encoding="utf-8",
            )
            self.assertEqual(validate_skill_tree(skill, require_provenance=False), ())

            (skill / "SKILL.md").write_text(
                '---\nname: safe-skill\ndescription: "Safe"\n---\n\n'
                + body
                + "\nRead [outside-missing](references/outside-missing.md).\n",
                encoding="utf-8",
            )
            errors = validate_skill_tree(skill, require_provenance=False)
            self.assertTrue(any("outside-missing" in error for error in errors))

    def test_frontmatter_fallback_rejects_non_strings_and_unterminated_quotes(self) -> None:
        cases = (
            "description: true",
            "description: [not, a, scalar]",
            'description: "unterminated',
            "description: 'unterminated",
        )
        for field in cases:
            with tempfile.TemporaryDirectory(prefix="automate-me-frontmatter-") as temp_dir:
                skill = Path(temp_dir) / "safe-skill"
                skill.mkdir()
                (skill / "SKILL.md").write_text(
                    f"---\nname: safe-skill\n{field}\n---\n",
                    encoding="utf-8",
                )
                errors = validate_skill_tree(skill, require_provenance=False)
                self.assertTrue(
                    any("frontmatter" in error for error in errors),
                    field,
                )

    def test_frontmatter_accepts_folded_and_literal_yaml_descriptions(self) -> None:
        for style in (">-", "|-"):
            with self.subTest(style=style):
                with tempfile.TemporaryDirectory(prefix="automate-me-block-description-") as temp_dir:
                    skill = Path(temp_dir) / "safe-skill"
                    skill.mkdir()
                    text = "\n".join(
                        (
                            "---",
                            "name: safe-skill",
                            f"description: {style}",
                            "  A valid multi-line description.",
                            "  It remains a YAML string.",
                            "---",
                            "",
                        )
                    )
                    (skill / "SKILL.md").write_text(text, encoding="utf-8")
                    self.assertEqual(
                        validate_skill_tree(skill, require_provenance=False), ()
                    )

        malformed = (
            "\n".join(
                ("---", "name: safe-skill", "description: >-", "not indented", "---", "")
            ),
            "\n".join(
                (
                    "---",
                    "name: safe-skill",
                    "description: |-",
                    "  valid first line",
                    " bad second line",
                    "---",
                    "",
                )
            ),
            "\n".join(
                ("---", "name: safe-skill", "description: >invalid", "  content", "---", "")
            ),
        )
        for text in malformed:
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory(prefix="automate-me-bad-block-description-") as temp_dir:
                    skill = Path(temp_dir) / "safe-skill"
                    skill.mkdir()
                    (skill / "SKILL.md").write_text(text, encoding="utf-8")
                    self.assertTrue(
                        validate_skill_tree(skill, require_provenance=False)
                    )

    def test_validation_rejects_symlinked_ancestor_components(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-ancestor-alias-") as temp_dir:
            root = Path(temp_dir)
            real_parent = root / "real-parent"
            skill = real_parent / "safe-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: safe-skill\ndescription: \"Safe\"\n---\n",
                encoding="utf-8",
            )
            alias = root / "alias-parent"
            alias.symlink_to(real_parent, target_is_directory=True)
            errors = validate_skill_tree(alias / "safe-skill", require_provenance=False)
            self.assertTrue(any("symlinked ancestor" in error for error in errors))

    def test_readme_activation_guard_skips_dangling_collision_safely(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            '[ -e "$CODEX_SKILLS/$skill_name" ] || [ -L "$CODEX_SKILLS/$skill_name" ]',
            readme,
        )
        with tempfile.TemporaryDirectory(prefix="automate-me-readme-activation-") as temp_dir:
            root = Path(temp_dir)
            repo_skills = root / "repo" / "skills"
            discovery = root / "codex-skills"
            (repo_skills / "literal%name").mkdir(parents=True)
            discovery.mkdir()
            (discovery / "literal%name").symlink_to(root / "missing-target")
            script = """
for skill_dir in "$REPO"/skills/*; do
  skill_name=$(basename "$skill_dir")
  if [ -e "$CODEX_SKILLS/$skill_name" ] || [ -L "$CODEX_SKILLS/$skill_name" ]; then
    printf 'Skipping existing skill %s\\n' "$skill_name"
    continue
  fi
  ln -s "$skill_dir" "$CODEX_SKILLS/$skill_name"
done
"""
            completed = subprocess.run(
                ["zsh", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "REPO": str(root / "repo"),
                    "CODEX_SKILLS": str(discovery),
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Skipping existing skill literal%name", completed.stdout)
            self.assertTrue((discovery / "literal%name").is_symlink())

    def test_every_bundled_markdown_file_is_link_checked_relative_to_itself(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-bundled-links-") as temp_dir:
            skill = Path(temp_dir) / "safe-skill"
            (skill / "references").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                f'''---
name: safe-skill
description: "Safe"
---

Read [style](references/style.md) and [provenance](references/provenance.md).
''',
                encoding="utf-8",
            )
            (skill / "references" / "style.md").write_text(
                "Read [missing](nested/not-there.md).\n", encoding="utf-8"
            )
            (skill / "references" / "provenance.md").write_text(
                f"{UPSTREAM_COMMIT}\nCopyright (c) 2026 Lauren Tan\nMIT License\n",
                encoding="utf-8",
            )
            errors = validate_skill_tree(skill)
            self.assertTrue(any("not-there" in error for error in errors))

    def test_discovery_rejects_direct_and_nested_dangling_aliases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-discovery-alias-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            skills = workspace / "skills"
            skills.mkdir(parents=True)
            (skills / "dangling").symlink_to(workspace / "missing", target_is_directory=True)
            with self.assertRaises(ScopeError):
                discover_existing_skills(workspace)

            (skills / "dangling").unlink()
            target = skills / "alex-mode"
            (target / "agents").mkdir(parents=True)
            (target / "SKILL.md").write_text(
                '---\nname: alex-mode\ndescription: "Alex"\n---\n',
                encoding="utf-8",
            )
            (target / "agents" / "openai.yaml").symlink_to(
                workspace / "missing-openai.yaml"
            )
            errors = validate_skill_discoverability(target)
            self.assertTrue(any("symlink" in error for error in errors))

    def test_concurrent_updates_are_serialized_without_losing_rules(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-lock-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            initial = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )
            write_generated_skill(
                "skills/alex-mode",
                initial,
                workspace_root=workspace,
                operation="create",
                approval=self._approval("create"),
            )
            first_ready = threading.Event()
            release_first = threading.Event()
            second_done = threading.Event()

            def first_injector(point: str) -> None:
                if point == "before_install":
                    first_ready.set()
                    self.assertTrue(release_first.wait(timeout=5))

            def run_first() -> None:
                write_generated_skill(
                    "skills/alex-mode",
                    SkillDraft(
                        name="alex-mode",
                        description="Apply first conventions.",
                        rules=("Use explicit checkpoints.",),
                        evidence=evidence,
                    ),
                    workspace_root=workspace,
                    operation="update",
                    approval=self._approval("update"),
                    failure_injector=first_injector,
                )

            def run_second() -> None:
                write_generated_skill(
                    "skills/alex-mode",
                    SkillDraft(
                        name="alex-mode",
                        description="Apply second conventions.",
                        rules=("Keep a short handoff.",),
                        evidence=evidence,
                    ),
                    workspace_root=workspace,
                    operation="update",
                    approval=self._approval("update"),
                )
                second_done.set()

            first = threading.Thread(target=run_first)
            second = threading.Thread(target=run_second)
            first.start()
            self.assertTrue(first_ready.wait(timeout=5))
            second.start()
            time.sleep(0.1)
            self.assertFalse(second_done.is_set())
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            final_text = (workspace / "skills" / "alex-mode" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Use explicit checkpoints.", final_text)
            self.assertIn("Keep a short handoff.", final_text)

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
            approval = self._approval("create")
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
                    approval=self._approval("update"),
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
            self.assertFalse(
                any(path.name.startswith(".alex-mode.") for path in target.parent.iterdir())
            )

            outside = workspace / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            (target / "references" / "alias.md").symlink_to(outside)
            with self.assertRaises(ScopeError):
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="update",
                    approval=self._approval("update"),
                )

    def test_output_paths_are_direct_and_alias_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-output-alias-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "aliases/alex-mode",
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=self._approval("create"),
                )

            skills = workspace / "skills"
            skills.mkdir(parents=True)
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            (skills / "alex-mode").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(CollisionError):
                write_generated_skill(
                    "skills/alex-mode",
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=self._approval("create"),
                )

            (skills / "alex-mode").unlink()
            (workspace / "skills-alias").symlink_to(skills, target_is_directory=True)
            with self.assertRaises(ValidationError):
                write_generated_skill(
                    "skills-alias/alex-mode",
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=self._approval("create"),
                )

    def test_create_publication_does_not_use_replace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-no-replace-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )
            with patch.object(automate_me.os, "replace", side_effect=AssertionError("replace")):
                write_generated_skill(
                    "skills/alex-mode",
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=self._approval("create"),
                )
            self.assertTrue((workspace / "skills" / "alex-mode" / "SKILL.md").is_file())

    def test_create_collision_cleanup_does_not_remove_target_appearing_after_preflight(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-create-race-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            target = workspace / "skills" / "alex-mode"
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )

            def create_competitor(point: str) -> None:
                if point == "before_install":
                    target.mkdir()
                    (target / "concurrent.txt").write_text("keep me", encoding="utf-8")

            with self.assertRaises(CollisionError):
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="create",
                    approval=self._approval("create"),
                    failure_injector=create_competitor,
                )
            self.assertEqual(
                (target / "concurrent.txt").read_text(encoding="utf-8"),
                "keep me",
            )

    def test_stage_binding_rejects_replacement_or_tampering_and_preserves_competitors(
        self,
    ) -> None:
        for mode in ("replacement", "mutation"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory(prefix="automate-me-stage-binding-") as temp_dir:
                    workspace = Path(temp_dir) / "project"
                    evidence = self._write_evidence(workspace)
                    target = workspace / "skills" / "alex-mode"
                    draft = SkillDraft(
                        name="alex-mode",
                        description="Apply approved Alex conventions.",
                        rules=("Keep replies short.",),
                        evidence=evidence,
                    )

                    def tamper_stage(point: str) -> None:
                        if point != "before_install":
                            return
                        stage_parent = next(
                            (workspace / "skills").glob(".alex-mode.stage-*")
                        )
                        stage_root = stage_parent / "alex-mode"
                        if mode == "replacement":
                            stage_root.rename(stage_parent / "original-stage")
                            stage_root.mkdir()
                            (stage_root / "competitor.txt").write_text(
                                "preserve competitor\n", encoding="utf-8"
                            )
                        else:
                            (stage_root / "SKILL.md").write_text(
                                "tampered after validation\n", encoding="utf-8"
                            )

                    with self.assertRaises(ScopeError):
                        write_generated_skill(
                            target,
                            draft,
                            workspace_root=workspace,
                            operation="create",
                            approval=self._approval("create"),
                            failure_injector=tamper_stage,
                        )
                    self.assertFalse(target.exists())
                    if mode == "replacement":
                        stage_parents = list(
                            (workspace / "skills").glob(".alex-mode.stage-*")
                        )
                        self.assertEqual(len(stage_parents), 1)
                        self.assertEqual(
                            (stage_parents[0] / "alex-mode" / "competitor.txt").read_text(
                                encoding="utf-8"
                            ),
                            "preserve competitor\n",
                        )
                        self.assertEqual(discover_existing_skills(workspace), ())
                    else:
                        self.assertEqual(
                            list((workspace / "skills").glob(".alex-mode.stage-*")),
                            [],
                        )

    def test_update_rejects_external_swap_before_publication(self) -> None:
        for replacement_kind in ("directory", "symlink"):
            with self.subTest(replacement_kind=replacement_kind):
                with tempfile.TemporaryDirectory(prefix="automate-me-update-swap-") as temp_dir:
                    workspace = Path(temp_dir) / "project"
                    evidence = self._write_evidence(workspace)
                    target = workspace / "skills" / "alex-mode"
                    draft = SkillDraft(
                        name="alex-mode",
                        description="Apply approved Alex conventions.",
                        rules=("Keep replies short.",),
                        evidence=evidence,
                    )
                    write_generated_skill(
                        target,
                        draft,
                        workspace_root=workspace,
                        operation="create",
                        approval=self._approval("create"),
                    )
                    displaced = workspace / "displaced-original"
                    replacement = workspace / "external-replacement"

                    def swap_target(point: str) -> None:
                        if point != "before_install":
                            return
                        target.rename(displaced)
                        replacement.mkdir()
                        (replacement / "sentinel.txt").write_text(
                            "external", encoding="utf-8"
                        )
                        if replacement_kind == "symlink":
                            target.symlink_to(replacement, target_is_directory=True)
                        else:
                            target.mkdir()
                            (target / "sentinel.txt").write_text(
                                "external", encoding="utf-8"
                            )

                    with self.assertRaises(ScopeError):
                        write_generated_skill(
                            target,
                            draft,
                            workspace_root=workspace,
                            operation="update",
                            approval=self._approval("update"),
                            failure_injector=swap_target,
                        )
                    self.assertTrue(displaced.exists())
                    if replacement_kind == "symlink":
                        self.assertTrue(target.is_symlink())
                    else:
                        self.assertEqual(
                            (target / "sentinel.txt").read_text(encoding="utf-8"),
                            "external",
                        )

    def test_update_collision_cleanup_preserves_target_appearing_after_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-update-race-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            target = workspace / "skills" / "alex-mode"
            initial_draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex conventions.",
                rules=("Keep the original approval visible.",),
                evidence=evidence,
            )
            write_generated_skill(
                target,
                initial_draft,
                workspace_root=workspace,
                operation="create",
                approval=self._approval("create"),
            )
            approved_skill = (target / "SKILL.md").read_text(encoding="utf-8")
            updated_draft = SkillDraft(
                name="alex-mode",
                description="Apply the replacement Alex conventions.",
                rules=("Keep the replacement approval visible.",),
                evidence=evidence,
            )

            def create_competitor(point: str) -> None:
                if point == "after_backup":
                    target.mkdir()
                    (target / "concurrent.txt").write_text("keep me", encoding="utf-8")
                    raise OSError("stop after external target appears")

            with self.assertRaises(PublicationRecoveryError) as raised:
                write_generated_skill(
                    target,
                    updated_draft,
                    workspace_root=workspace,
                    operation="update",
                    approval=self._approval("update"),
                    failure_injector=create_competitor,
                )
            recovery_error = raised.exception
            self.assertTrue(recovery_error.competitor_preserved)
            self.assertEqual(recovery_error.recovery_status, "approved_tree_recovered")
            self.assertEqual(recovery_error.cleanup_status, "hidden_backup_removed")
            self.assertEqual(
                (target / "concurrent.txt").read_text(encoding="utf-8"),
                "keep me",
            )
            recovery = Path(recovery_error.recovery_path)
            self.assertEqual(
                recovery,
                workspace / "skills" / ".automate-me-recovery" / "alex-mode",
            )
            self.assertEqual(
                (recovery / "SKILL.md").read_text(encoding="utf-8"),
                approved_skill,
            )
            self.assertEqual(validate_skill_tree(recovery), ())
            self.assertFalse(
                any(
                    path.name.startswith(".alex-mode.backup-")
                    for path in target.parent.iterdir()
                )
            )

    def test_tampered_backup_is_never_restored_and_recovery_status_is_deterministic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-tampered-backup-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            target = workspace / "skills" / "alex-mode"
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )
            write_generated_skill(
                target,
                draft,
                workspace_root=workspace,
                operation="create",
                approval=self._approval("create"),
            )

            def tamper_backup(point: str) -> None:
                if point != "after_backup":
                    return
                backup = next(target.parent.glob(".alex-mode.backup-*"))
                (backup / "SKILL.md").write_text(
                    "tampered backup content\n", encoding="utf-8"
                )
                raise OSError("injected rollback failure")

            with self.assertRaises(PublicationRecoveryError) as raised:
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="update",
                    approval=self._approval("update"),
                    failure_injector=tamper_backup,
                )
            recovery_error = raised.exception
            self.assertEqual(
                recovery_error.recovery_status,
                "backup_rejected_recovery_preserved",
            )
            self.assertEqual(
                recovery_error.cleanup_status,
                "hidden_backup_preserved",
            )
            self.assertFalse(target.exists())
            self.assertTrue(Path(recovery_error.recovery_path).is_dir())
            backups = list(target.parent.glob(".alex-mode.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                backups[0]
                .joinpath("SKILL.md")
                .read_text(encoding="utf-8"),
                "tampered backup content\n",
            )
            self.assertEqual(validate_skill_tree(Path(recovery_error.recovery_path)), ())
            self.assertEqual(discover_existing_skills(workspace), ())

    def test_replaced_backup_identity_is_preserved_and_not_restored(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-replaced-backup-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            target = workspace / "skills" / "alex-mode"
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )
            write_generated_skill(
                target,
                draft,
                workspace_root=workspace,
                operation="create",
                approval=self._approval("create"),
            )

            def replace_backup(point: str) -> None:
                if point != "after_backup":
                    return
                backup = next(target.parent.glob(".alex-mode.backup-*"))
                displaced = backup.with_name(backup.name + "-competitor")
                backup.rename(displaced)
                backup.mkdir()
                (backup / "competitor.txt").write_text("keep competitor\n", encoding="utf-8")
                raise OSError("injected rollback failure")

            with self.assertRaises(PublicationRecoveryError) as raised:
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="update",
                    approval=self._approval("update"),
                    failure_injector=replace_backup,
                )
            self.assertEqual(
                raised.exception.recovery_status,
                "backup_rejected_recovery_preserved",
            )
            self.assertFalse(target.exists())
            backup = next(
                path
                for path in target.parent.glob(".alex-mode.backup-*")
                if (path / "competitor.txt").exists()
            )
            self.assertEqual(
                (backup / "competitor.txt").read_text(encoding="utf-8"),
                "keep competitor\n",
            )
            self.assertTrue(
                next(target.parent.glob(".alex-mode.backup-*-competitor")).is_dir()
            )

    def test_update_rejects_same_directory_content_mutation_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="automate-me-content-race-") as temp_dir:
            workspace = Path(temp_dir) / "project"
            evidence = self._write_evidence(workspace)
            target = workspace / "skills" / "alex-mode"
            draft = SkillDraft(
                name="alex-mode",
                description="Apply approved Alex conventions.",
                rules=("Keep replies short.",),
                evidence=evidence,
            )
            write_generated_skill(
                target,
                draft,
                workspace_root=workspace,
                operation="create",
                approval=self._approval("create"),
            )
            notes = target / "notes.md"
            notes.write_text("approved before the update\n", encoding="utf-8")

            def mutate_content(point: str) -> None:
                if point == "before_install":
                    notes.write_text(
                        "concurrent edit after preflight\n", encoding="utf-8"
                    )

            with self.assertRaises(ScopeError) as raised:
                write_generated_skill(
                    target,
                    draft,
                    workspace_root=workspace,
                    operation="update",
                    approval=self._approval("update"),
                    failure_injector=mutate_content,
                )
            self.assertIn("content changed after preflight", str(raised.exception))
            self.assertEqual(
                notes.read_text(encoding="utf-8"),
                "concurrent edit after preflight\n",
            )
            self.assertFalse(
                any(
                    path.name.startswith(".alex-mode.backup-")
                    for path in target.parent.iterdir()
                )
            )
            self.assertFalse((target.parent / ".automate-me-recovery").exists())

    def test_update_bounds_preserved_regular_file_copies(self) -> None:
        for mode in ("per-file", "aggregate"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory(prefix="automate-me-preserved-bounds-") as temp_dir:
                    workspace = Path(temp_dir) / "project"
                    evidence = self._write_evidence(workspace)
                    target = workspace / "skills" / "alex-mode"
                    draft = SkillDraft(
                        name="alex-mode",
                        description="Apply approved Alex conventions.",
                        rules=("Keep replies short.",),
                        evidence=evidence,
                    )
                    write_generated_skill(
                        target,
                        draft,
                        workspace_root=workspace,
                        operation="create",
                        approval=self._approval("create"),
                    )
                    if mode == "per-file":
                        (target / "unrelated.bin").write_bytes(
                            b"x" * (automate_me.MAX_ARTIFACT_BYTES + 1)
                        )
                    else:
                        for name in ("one.bin", "two.bin"):
                            (target / name).write_bytes(
                                b"x" * (automate_me.MAX_TOTAL_ARTIFACT_BYTES // 2)
                            )
                    with self.assertRaises(ScopeError):
                        write_generated_skill(
                            target,
                            draft,
                            workspace_root=workspace,
                            operation="update",
                            approval=self._approval("update"),
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
                    approval=self._approval("create"),
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
                approval=self._approval("update"),
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
