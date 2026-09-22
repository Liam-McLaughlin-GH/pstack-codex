from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "skills" / "maintain-verification-skill" / "scripts" / "maintain.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tiny_app.py"


def run_helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def json_output(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)


def create_project(root: Path, *, stale: bool) -> dict[str, Path | str | dict[str, object]]:
    project = root / "project"
    project.mkdir()
    app = project / "app.py"
    app.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    state = project / "scratch" / "state.txt"
    state.parent.mkdir()
    state.write_text("ready\n", encoding="utf-8")

    skill_root = project / ".agents" / "skills" / "verify-tiny"
    features = skill_root / "features"
    features.mkdir(parents=True)
    skill_root.joinpath("SKILL.md").write_text(
        "---\n"
        "name: verify-tiny\n"
        "description: Verify the tiny CLI through its shell harness.\n"
        "---\n\n"
        "# Verify tiny\n\n"
        "## Launch\n\nRun the fixture.\n\n"
        "## Doctor\n\nRun the fixture Doctor.\n\n"
        "## Drive\n\nUse the shell harness.\n\n"
        "## Evidence\n\nKeep command output.\n\n"
        "## Cleanup\n\nRemove owned scratch state.\n\n"
        "## Helpers\n\nNo helpers.\n",
        encoding="utf-8",
    )
    command = f"{sys.executable} {app} check --state {state} --expected ready"
    old_command = command.replace("--expected ready", "--expected stale")
    fixture_text = f"state file containing ready at {state}"
    expected_result = "stdout contains check passed"

    def feature(title: str, feature_command: str, expected: str) -> str:
        return (
            f"# {title}\n\n"
            "An operator checks the current state from a terminal.\n\n"
            "## Sub-features\n\n"
            "- `check`: reports the requested state.\n\n"
            "## How to get to it (user POV)\n\n"
            "- Run the check command from a terminal.\n\n"
            "## Driving it with shell\n\n"
            "Preconditions:\n\n"
            f"- Actors: operator\n- Fixtures: {fixture_text}\n\n"
            "- **Check the state.**\n"
            "  - Actors: operator\n"
            f"  - Fixtures: {fixture_text}\n"
            f"  - Action: `{feature_command}`\n"
            f"  - Expected result: {expected}\n"
            "  - Evidence: record stdout, stderr, and exit code in the named artifact.\n\n"
            "## Gotchas\n\n"
            "- A missing state file is a failed precondition.\n"
        )

    (features / "check-state.md").write_text(
        feature("Check fixture state", old_command if stale else command, "stdout contains check failed" if stale else expected_result),
        encoding="utf-8",
    )
    (features / "healthy-state.md").write_text(
        feature("Confirm fixture state", command, expected_result),
        encoding="utf-8",
    )
    (features / "README.md").write_text(
        "# Tiny verification map\n\n"
        "## Baseline preconditions\n\n"
        f"- The state file contains ready at `{state}`.\n\n"
        "## Driving conventions\n\n"
        "- Use the shell harness and record command output.\n\n"
        "## Proof and skip reporting\n\n"
        "- Keep evidence after cleanup.\n\n"
        "## Features\n\n"
        "- [Check fixture state](./check-state.md) checks the state.\n"
        "- [Confirm fixture state](./healthy-state.md) confirms the state.\n",
        encoding="utf-8",
    )

    artifacts = project / "artifacts"
    artifacts.mkdir()
    check_evidence = artifacts / "check-state.txt"
    healthy_evidence = artifacts / "healthy-state.txt"
    check_evidence.write_text("placeholder evidence\n", encoding="utf-8")
    healthy_evidence.write_text("placeholder evidence\n", encoding="utf-8")

    readers = {
        "readers": [
            {
                "feature_id": "check-state",
                "status": "complete",
                "summary": "An operator checks the current state from a terminal.",
                "source_entries": [{"path": "app.py", "lines": "1-40"}],
                "authoritative": {
                    "entry_points": ["Run the check command from a terminal."],
                    "commands": [command],
                    "selectors": [],
                    "fixtures": [fixture_text],
                    "expected_results": [expected_result],
                },
                "harness": {"status": "available"},
                "live_recipe": {
                    "actor": "operator",
                    "fixture": fixture_text,
                    "action": command,
                    "expected_result": expected_result,
                    "evidence": "terminal receipt",
                },
            },
            {
                "feature_id": "healthy-state",
                "status": "complete",
                "summary": "An operator confirms the current state from a terminal.",
                "source_entries": [{"path": "app.py", "lines": "1-40"}],
                "authoritative": {
                    "entry_points": ["Run the check command from a terminal."],
                    "commands": [command],
                    "selectors": [],
                    "fixtures": [fixture_text],
                    "expected_results": [expected_result],
                },
                "harness": {"status": "available"},
                "live_recipe": {
                    "actor": "operator",
                    "fixture": fixture_text,
                    "action": command,
                    "expected_result": expected_result,
                    "evidence": "terminal receipt",
                },
            },
        ]
    }
    reader_path = root / "readers.json"
    reader_path.write_text(json.dumps(readers, indent=2), encoding="utf-8")

    live = {
        "doctor": {
            "status": "pass",
            "skill_root": str(skill_root),
            "session_id": "tiny-run-1",
            "owned": True,
            "healthy": True,
            "target": "tiny-cli",
            "build": "fixture-v1",
            "endpoint": "cli",
            "auth": "not-required",
            "disposable": True,
        },
        "run": {
            "run_id": "tiny-run-1",
            "session_id": "tiny-run-1",
            "phase": "baseline",
        },
        "features": [
            {
                "feature_id": "check-state",
                "status": "pass",
                "source_match": True,
                "evidence": [str(check_evidence)],
                "evidence_survives_cleanup": True,
            },
            {
                "feature_id": "healthy-state",
                "status": "pass",
                "source_match": True,
                "evidence": [str(healthy_evidence)],
                "evidence_survives_cleanup": True,
            },
        ],
    }
    live_path = root / "live.json"
    live_path.write_text(json.dumps(live, indent=2), encoding="utf-8")
    return {
        "project": project,
        "app": app,
        "state": state,
        "skill_root": skill_root,
        "command": command,
        "old_command": old_command,
        "reader_path": reader_path,
        "live_path": live_path,
        "live": live,
        "check_evidence": check_evidence,
        "healthy_feature": features / "healthy-state.md",
    }


class MaintainVerificationSkillBehaviorTests(unittest.TestCase):
    def test_seeded_stale_recipe_is_classified_fixed_and_reproven(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-drift-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=True)
            skill_root = fixture["skill_root"]
            project = fixture["project"]
            reader_path = fixture["reader_path"]
            live_path = fixture["live_path"]
            healthy_before = fixture["healthy_feature"].read_bytes()

            audit_before = run_helper(
                "audit",
                "--skill-root",
                str(skill_root),
                "--project-root",
                str(project),
                "--reader-report",
                str(reader_path),
                "--live-report",
                str(live_path),
                "--json",
            )
            self.assertEqual(audit_before.returncode, 1, audit_before.stderr)
            before_payload = json_output(audit_before)
            self.assertEqual(before_payload["outcome"], "blocked")
            self.assertTrue(any(item["kind"] == "doc-drift" for item in before_payload["findings"]))

            patch_plan = root / "patches.json"
            patch_plan.write_text(
                json.dumps(
                    {
                        "changes": [
                            {
                                "path": "features/check-state.md",
                                "kind": "doc-drift",
                                "old": "--expected stale",
                                "new": "--expected ready",
                                "reason": "The source reader found the current CLI expectation.",
                                "source": "app.py:1-40",
                            },
                            {
                                "path": "features/check-state.md",
                                "kind": "doc-drift",
                                "old": "stdout contains check failed",
                                "new": "stdout contains check passed",
                                "reason": "The fixture prints check passed for a matching state.",
                                "source": "app.py:1-40",
                            },
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            applied = run_helper("apply", "--skill-root", str(skill_root), "--patch-plan", str(patch_plan), "--json")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            applied_payload = json_output(applied)
            self.assertEqual(applied_payload["changed_files"], ["features/check-state.md"])
            apply_record = Path(applied_payload["apply_record_path"])
            self.assertTrue(apply_record.is_file())
            self.assertEqual(apply_record.stat().st_mode & 0o777, 0o600)
            self.assertEqual(fixture["healthy_feature"].read_bytes(), healthy_before)

            command_result = subprocess.run(
                [sys.executable, str(fixture["app"]), "check", "--state", str(fixture["state"]), "--expected", "ready"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(command_result.returncode, 0, command_result.stderr)
            self.assertIn("check passed: ready", command_result.stdout)
            fixture["check_evidence"].write_text(command_result.stdout, encoding="utf-8")

            fixture["live"]["run"] = {
                "run_id": applied_payload["run_id"],
                "session_id": applied_payload["session_id"],
                "phase": "post-apply",
                "apply_id": applied_payload["apply_id"],
            }
            fixture["live"]["doctor"]["session_id"] = applied_payload["session_id"]
            fixture["live"]["features"][0]["post_apply"] = {
                "redriven": True,
                "apply_id": applied_payload["apply_id"],
                "run_id": applied_payload["run_id"],
                "session_id": applied_payload["session_id"],
            }
            live_path.write_text(json.dumps(fixture["live"]), encoding="utf-8")

            change_log = root / "change-log.json"
            change_log.write_text(json.dumps(applied_payload), encoding="utf-8")
            audit_after = run_helper(
                "audit",
                "--skill-root",
                str(skill_root),
                "--project-root",
                str(project),
                "--reader-report",
                str(reader_path),
                "--live-report",
                str(live_path),
                "--change-log",
                str(change_log),
                "--json",
            )
            self.assertEqual(audit_after.returncode, 0, audit_after.stderr)
            after_payload = json_output(audit_after)
            self.assertEqual(after_payload["outcome"], "changed")
            self.assertFalse(any(item["blocking"] for item in after_payload["findings"]))

    def test_product_and_harness_gaps_are_not_documentation_fixes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-gaps-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            live = fixture["live"]
            live["features"][0] = {
                "feature_id": "check-state",
                "status": "fail",
                "source_match": True,
                "route": "check command",
                "observed": "the real command returned exit code 1",
                "evidence": [str(fixture["check_evidence"])],
                "evidence_survives_cleanup": True,
            }
            product_live = root / "product-live.json"
            product_live.write_text(json.dumps(live), encoding="utf-8")
            product = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(product_live),
                "--json",
            )
            self.assertEqual(product.returncode, 1)
            self.assertTrue(any(item["kind"] == "product-gap" for item in json_output(product)["findings"]))

            live["features"][0] = {
                "feature_id": "check-state",
                "status": "harness-gap",
                "route": "check command",
                "reason": "the shell adapter cannot start in this environment",
            }
            harness_live = root / "harness-live.json"
            harness_live.write_text(json.dumps(live), encoding="utf-8")
            harness = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(harness_live),
                "--json",
            )
            self.assertEqual(harness.returncode, 1)
            self.assertTrue(any(item["kind"] == "harness-gap" for item in json_output(harness)["findings"]))

    def test_changed_requires_post_apply_identity_and_affected_redrive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-receipt-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=True)
            patch_plan = root / "patches.json"
            patch_plan.write_text(
                json.dumps(
                    {
                        "changes": [
                            {
                                "path": "features/check-state.md",
                                "kind": "doc-drift",
                                "old": "--expected stale",
                                "new": "--expected ready",
                                "reason": "source changed",
                                "source": "app.py:1-40",
                            },
                            {
                                "path": "features/check-state.md",
                                "kind": "doc-drift",
                                "old": "stdout contains check failed",
                                "new": "stdout contains check passed",
                                "reason": "source changed",
                                "source": "app.py:1-40",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            applied = run_helper(
                "apply",
                "--skill-root",
                str(fixture["skill_root"]),
                "--patch-plan",
                str(patch_plan),
                "--json",
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            receipt = json_output(applied)
            change_log = root / "change-log.json"
            change_log.write_text(json.dumps(receipt), encoding="utf-8")

            stale_live = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(fixture["live_path"]),
                "--change-log",
                str(change_log),
                "--json",
            )
            self.assertEqual(stale_live.returncode, 1, stale_live.stderr)
            stale_payload = json_output(stale_live)
            self.assertEqual(stale_payload["outcome"], "blocked")
            self.assertTrue(any(item["kind"] == "evidence-failure" for item in stale_payload["findings"]))

            post_live = fixture["live"]
            post_live["run"] = {
                "run_id": receipt["run_id"],
                "session_id": receipt["session_id"],
                "phase": "post-apply",
                "apply_id": receipt["apply_id"],
            }
            post_live["doctor"]["session_id"] = receipt["session_id"]
            post_path = root / "post-live.json"
            post_path.write_text(json.dumps(post_live), encoding="utf-8")
            missing_redrive = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(post_path),
                "--change-log",
                str(change_log),
                "--json",
            )
            self.assertEqual(missing_redrive.returncode, 1, missing_redrive.stderr)
            missing_payload = json_output(missing_redrive)
            self.assertEqual(missing_payload["outcome"], "blocked")
            self.assertTrue(any(item["kind"] == "evidence-failure" for item in missing_payload["findings"]))

            alias_live = json.loads(json.dumps(post_live))
            alias_feature = alias_live["features"][0]
            alias_feature["redriven_after_apply"] = True
            alias_feature.update(
                {
                    "apply_id": receipt["apply_id"],
                    "run_id": receipt["run_id"],
                    "session_id": receipt["session_id"],
                }
            )
            alias_path = root / "alias-live.json"
            alias_path.write_text(json.dumps(alias_live), encoding="utf-8")
            alias_result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(alias_path),
                "--change-log",
                str(change_log),
                "--json",
            )
            self.assertEqual(alias_result.returncode, 1, alias_result.stderr)
            alias_payload = json_output(alias_result)
            self.assertEqual(alias_payload["outcome"], "blocked")
            self.assertTrue(any(item["kind"] == "evidence-failure" for item in alias_payload["findings"]))

            alias_feature["post_apply"] = {
                "redriven": True,
                "apply_id": receipt["apply_id"],
                "run_id": receipt["run_id"],
                "session_id": receipt["session_id"],
            }
            alias_path.write_text(json.dumps(alias_live), encoding="utf-8")
            aliases_with_binding = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(alias_path),
                "--change-log",
                str(change_log),
                "--json",
            )
            self.assertEqual(aliases_with_binding.returncode, 1, aliases_with_binding.stderr)
            aliases_payload = json_output(aliases_with_binding)
            self.assertEqual(aliases_payload["outcome"], "blocked")
            self.assertTrue(any(item["kind"] == "evidence-failure" for item in aliases_payload["findings"]))

    def test_fabricated_change_receipt_requires_helper_provenance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-fabricated-receipt-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            changed_file = "features/check-state.md"
            changed_path = fixture["skill_root"] / changed_file
            receipt = {
                "receipt_version": 2,
                "outcome": "changed",
                "apply_id": "fabricated-apply",
                "run_id": "fabricated-run",
                "session_id": "fabricated-session",
                "skill_root": str(fixture["skill_root"]),
                "plan_sha256": "0" * 64,
                "affected_features": ["check-state"],
                "changed_files": [changed_file],
                "before_file_digests": {changed_file: "1" * 64},
                "file_digests": {
                    changed_file: hashlib.sha256(changed_path.read_bytes()).hexdigest()
                },
                "changes": [
                    {
                        "path": changed_file,
                        "kind": "doc-drift",
                        "feature_ids": ["check-state"],
                    }
                ],
            }
            change_log = root / "fabricated-change-log.json"
            change_log.write_text(json.dumps(receipt), encoding="utf-8")

            live = json.loads(json.dumps(fixture["live"]))
            live["run"] = {
                "run_id": receipt["run_id"],
                "session_id": receipt["session_id"],
                "phase": "post-apply",
                "apply_id": receipt["apply_id"],
            }
            live["doctor"]["session_id"] = receipt["session_id"]
            live["features"][0]["post_apply"] = {
                "redriven": True,
                "apply_id": receipt["apply_id"],
                "run_id": receipt["run_id"],
                "session_id": receipt["session_id"],
            }
            live_path = root / "fabricated-live.json"
            live_path.write_text(json.dumps(live), encoding="utf-8")

            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(live_path),
                "--change-log",
                str(change_log),
                "--json",
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json_output(result)
            self.assertEqual(payload["outcome"], "blocked")
            self.assertTrue(any(item["kind"] == "evidence-failure" for item in payload["findings"]))

    def test_fully_forged_apply_bundle_cannot_prove_changed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-forged-bundle-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            changed_file = "features/check-state.md"
            skill_root = fixture["skill_root"].resolve()
            changed_path = skill_root / changed_file
            apply_id = "apply-" + ("a" * 32)
            after_digest = hashlib.sha256(changed_path.read_bytes()).hexdigest()
            before_digest = "1" * 64
            patch_plan = root / "forged-patches.json"
            patch_plan.write_text(
                json.dumps(
                    {
                        "changes": [
                            {
                                "path": changed_file,
                                "kind": "doc-drift",
                                "old": "a caller-claimed old span",
                                "new": "a caller-claimed new span",
                                "reason": "forged correction",
                                "source": "app.py:1-40",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            plan_sha256 = hashlib.sha256(patch_plan.read_bytes()).hexdigest()
            receipt = {
                "receipt_version": 2,
                "outcome": "changed",
                "apply_id": apply_id,
                "run_id": "forged-run",
                "session_id": "forged-session",
                "skill_root": str(skill_root),
                "provenance_path": str(
                    patch_plan.with_name(f"{patch_plan.name}.{apply_id}.provenance.json")
                ),
                "apply_record_path": str(
                    skill_root / ".maintain-verification-state" / "apply-records" / f"{apply_id}.json"
                ),
                "trust_marker": "0" * 64,
                "plan_sha256": plan_sha256,
                "affected_features": ["check-state"],
                "changed_files": [changed_file],
                "before_file_digests": {changed_file: before_digest},
                "file_digests": {changed_file: after_digest},
                "changes": [
                    {
                        "path": changed_file,
                        "kind": "doc-drift",
                        "feature_ids": ["check-state"],
                    }
                ],
            }
            provenance = {
                "provenance_version": 1,
                "apply_id": receipt["apply_id"],
                "run_id": receipt["run_id"],
                "session_id": receipt["session_id"],
                "skill_root": receipt["skill_root"],
                "plan_path": str(patch_plan),
                "plan_sha256": plan_sha256,
                "affected_features": receipt["affected_features"],
                "changed_files": receipt["changed_files"],
                "before_file_digests": receipt["before_file_digests"],
                "after_file_digests": receipt["file_digests"],
                "changes": receipt["changes"],
            }
            Path(receipt["provenance_path"]).write_text(json.dumps(provenance), encoding="utf-8")
            change_log = root / "forged-change-log.json"
            change_log.write_text(json.dumps(receipt), encoding="utf-8")

            live = json.loads(json.dumps(fixture["live"]))
            live["run"] = {
                "run_id": receipt["run_id"],
                "session_id": receipt["session_id"],
                "phase": "post-apply",
                "apply_id": receipt["apply_id"],
            }
            live["doctor"]["session_id"] = receipt["session_id"]
            live["features"][0]["post_apply"] = {
                "redriven": True,
                "apply_id": receipt["apply_id"],
                "run_id": receipt["run_id"],
                "session_id": receipt["session_id"],
            }
            live_path = root / "forged-live.json"
            live_path.write_text(json.dumps(live), encoding="utf-8")

            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(live_path),
                "--change-log",
                str(change_log),
                "--json",
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json_output(result)
            self.assertEqual(payload["outcome"], "blocked")
            self.assertTrue(any(item["kind"] == "evidence-failure" for item in payload["findings"]))

    def test_audit_and_doctor_use_the_same_strict_doctor_contract(self) -> None:
        required = ("target", "build", "endpoint", "auth", "disposable")
        with tempfile.TemporaryDirectory(prefix="maintain-verification-doctor-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            for field in required:
                report = dict(fixture["live"]["doctor"])
                report.pop(field)
                report_path = root / f"doctor-{field}.json"
                report_path.write_text(json.dumps(report), encoding="utf-8")
                doctor_result = run_helper(
                    "doctor",
                    "--skill-root",
                    str(fixture["skill_root"]),
                    "--report",
                    str(report_path),
                    "--json",
                )
                self.assertEqual(doctor_result.returncode, 1, field)
                self.assertEqual(json_output(doctor_result)["status"], "fail")

                live = json.loads(json.dumps(fixture["live"]))
                live["doctor"] = report
                live_path = root / f"live-{field}.json"
                live_path.write_text(json.dumps(live), encoding="utf-8")
                audit_result = run_helper(
                    "audit",
                    "--skill-root",
                    str(fixture["skill_root"]),
                    "--project-root",
                    str(fixture["project"]),
                    "--reader-report",
                    str(fixture["reader_path"]),
                    "--live-report",
                    str(live_path),
                    "--json",
                )
                self.assertEqual(audit_result.returncode, 1, field)
                findings = json_output(audit_result)["findings"]
                self.assertTrue(any(item["kind"] == "doctor-failure" for item in findings), field)
                self.assertFalse(any(item["kind"] == "doc-drift" for item in findings), field)

    def test_cleanup_preflights_all_targets_and_rejects_product_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-cleanup-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            project = fixture["project"]
            product = project / "product.txt"
            product.write_text("keep me\n", encoding="utf-8")
            safe = root / "safe.txt"
            safe.write_text("scratch\n", encoding="utf-8")
            manifest = root / "cleanup.json"
            manifest.write_text(
                json.dumps(
                    {
                        "owned_paths": ["safe.txt", "project/product.txt"],
                        "evidence_paths": [str(fixture["check_evidence"])],
                    }
                ),
                encoding="utf-8",
            )
            result = run_helper(
                "cleanup",
                "--run-root",
                str(root),
                "--manifest",
                str(manifest),
                "--project-root",
                str(project),
                "--skill-root",
                str(fixture["skill_root"]),
                "--json",
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue(safe.exists(), "preflight must prevent partial cleanup")
            self.assertEqual(product.read_text(encoding="utf-8"), "keep me\n")

            mis_scoped = project / "run"
            mis_scoped.mkdir()
            mis_manifest = mis_scoped / "cleanup.json"
            mis_manifest.write_text(
                json.dumps({"owned_paths": ["product.txt"], "evidence_paths": [str(fixture["check_evidence"])]}),
                encoding="utf-8",
            )
            mis_result = run_helper(
                "cleanup",
                "--run-root",
                str(mis_scoped),
                "--manifest",
                str(mis_manifest),
                "--project-root",
                str(project),
                "--json",
            )
            self.assertEqual(mis_result.returncode, 2, mis_result.stderr)
            self.assertEqual(product.read_text(encoding="utf-8"), "keep me\n")

    def test_feature_actions_are_validated_individually_and_in_order(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-actions-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            feature_path = fixture["skill_root"] / "features" / "check-state.md"
            text = feature_path.read_text(encoding="utf-8")
            text = text.replace(
                "  - Actors: operator\n"
                f"  - Fixtures: state file containing ready at {fixture['state']}\n",
                f"  - Fixtures: state file containing ready at {fixture['state']}\n"
                "  - Actors: operator\n",
                1,
            )
            feature_path.write_text(text, encoding="utf-8")
            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(fixture["live_path"]),
                "--json",
            )
            self.assertEqual(result.returncode, 1)
            findings = json_output(result)["findings"]
            self.assertTrue(any(item["kind"] == "doc-drift" for item in findings))

    def test_driving_section_requires_preconditions_as_its_first_nonempty_line(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-preconditions-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            feature_path = fixture["skill_root"] / "features" / "check-state.md"
            text = feature_path.read_text(encoding="utf-8").replace(
                "## Driving it with shell\n\nPreconditions:",
                "## Driving it with shell\n\nIntroductory text.\n\nPreconditions:",
                1,
            )
            feature_path.write_text(text, encoding="utf-8")
            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(fixture["live_path"]),
                "--json",
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            findings = json_output(result)["findings"]
            self.assertTrue(
                any(
                    item["kind"] == "doc-drift"
                    and "must start with Preconditions" in item["message"]
                    for item in findings
                )
            )

    def test_complete_source_reader_requires_all_authoritative_string_arrays(self) -> None:
        authoritative_fields = (
            "entry_points",
            "commands",
            "selectors",
            "fixtures",
            "expected_results",
        )
        with tempfile.TemporaryDirectory(prefix="maintain-verification-authoritative-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)

            for field in authoritative_fields:
                readers = json.loads(fixture["reader_path"].read_text(encoding="utf-8"))
                readers["readers"][0]["authoritative"].pop(field)
                reader_path = root / f"missing-{field}.json"
                reader_path.write_text(json.dumps(readers), encoding="utf-8")
                result = run_helper(
                    "audit",
                    "--skill-root",
                    str(fixture["skill_root"]),
                    "--project-root",
                    str(fixture["project"]),
                    "--reader-report",
                    str(reader_path),
                    "--live-report",
                    str(fixture["live_path"]),
                    "--json",
                )
                self.assertEqual(result.returncode, 2, field)
                self.assertIn(f"authoritative.{field}", result.stderr)

            readers = json.loads(fixture["reader_path"].read_text(encoding="utf-8"))
            readers["readers"][0].pop("authoritative")
            missing_object = root / "missing-authoritative.json"
            missing_object.write_text(json.dumps(readers), encoding="utf-8")
            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(missing_object),
                "--live-report",
                str(fixture["live_path"]),
                "--json",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("authoritative must be an object", result.stderr)

            readers = json.loads(fixture["reader_path"].read_text(encoding="utf-8"))
            readers["readers"][0]["authoritative"]["commands"] = "not-an-array"
            invalid_array = root / "invalid-authoritative-array.json"
            invalid_array.write_text(json.dumps(readers), encoding="utf-8")
            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(invalid_array),
                "--live-report",
                str(fixture["live_path"]),
                "--json",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("authoritative.commands must be an array", result.stderr)

    def test_target_frontmatter_is_strict_and_matches_target_name(self) -> None:
        variants = {
            "wrong-name": "name: verify-other\ndescription: Verify the tiny CLI.\n",
            "malformed": "name: [verify-tiny\ndescription: Verify the tiny CLI.\n",
            "duplicate": "name: verify-tiny\nname: verify-tiny\ndescription: Verify the tiny CLI.\n",
            "unknown": "name: verify-tiny\ndescription: Verify the tiny CLI.\nowner: qa\n",
            "non-string": "name: verify-tiny\ndescription: null\n",
        }
        with tempfile.TemporaryDirectory(prefix="maintain-verification-frontmatter-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            skill_file = fixture["skill_root"] / "SKILL.md"
            original = skill_file.read_text(encoding="utf-8")
            body = original.split("---\n", 2)[2]
            for label, fields in variants.items():
                skill_file.write_text(f"---\n{fields}---\n{body}", encoding="utf-8")
                result = run_helper(
                    "audit",
                    "--skill-root",
                    str(fixture["skill_root"]),
                    "--project-root",
                    str(fixture["project"]),
                    "--reader-report",
                    str(fixture["reader_path"]),
                    "--live-report",
                    str(fixture["live_path"]),
                    "--json",
                )
                self.assertEqual(result.returncode, 1, label)
                payload = json_output(result)
                self.assertEqual(payload["outcome"], "blocked", label)
                self.assertTrue(
                    any(
                        item["kind"] == "doc-drift" and item.get("path") == "SKILL.md"
                        for item in payload["findings"]
                    ),
                    label,
                )
            skill_file.write_text(original, encoding="utf-8")

    def test_source_entries_need_citations_and_regular_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-sources-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            readers = json.loads(fixture["reader_path"].read_text(encoding="utf-8"))
            readers["readers"][0]["source_entries"] = [{"path": "scratch", "lines": "1"}]
            directory_path = root / "directory-reader.json"
            directory_path.write_text(json.dumps(readers), encoding="utf-8")
            directory_result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(directory_path),
                "--live-report",
                str(fixture["live_path"]),
                "--json",
            )
            self.assertEqual(directory_result.returncode, 1)
            self.assertTrue(any(item["kind"] == "blocked" for item in json_output(directory_result)["findings"]))

            readers["readers"][0]["source_entries"] = [{"path": "app.py"}]
            no_citation_path = root / "no-citation-reader.json"
            no_citation_path.write_text(json.dumps(readers), encoding="utf-8")
            no_citation_result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(no_citation_path),
                "--live-report",
                str(fixture["live_path"]),
                "--json",
            )
            self.assertEqual(no_citation_result.returncode, 2)
            self.assertIn("citation", no_citation_result.stderr)

    def test_doctor_harness_and_evidence_failures_are_not_doc_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-classification-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)

            missing_evidence = json.loads(json.dumps(fixture["live"]))
            missing_evidence["features"][0]["evidence"] = [str(root / "missing.txt")]
            missing_path = root / "missing-evidence.json"
            missing_path.write_text(json.dumps(missing_evidence), encoding="utf-8")
            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(missing_path),
                "--json",
            )
            self.assertEqual(result.returncode, 1)
            findings = json_output(result)["findings"]
            self.assertTrue(any(item["kind"] == "evidence-failure" for item in findings))
            self.assertFalse(any(item["kind"] == "doc-drift" for item in findings))

            no_source_match = json.loads(json.dumps(fixture["live"]))
            no_source_match["features"][0]["source_match"] = False
            no_source_match_path = root / "no-source-match.json"
            no_source_match_path.write_text(json.dumps(no_source_match), encoding="utf-8")
            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(no_source_match_path),
                "--json",
            )
            self.assertEqual(result.returncode, 1)
            findings = json_output(result)["findings"]
            self.assertTrue(any(item["kind"] == "evidence-failure" for item in findings))
            self.assertFalse(any(item["kind"] == "doc-drift" for item in findings))

    def test_doctor_cleanup_and_edit_boundary_are_guarded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-boundaries-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            skill_root = fixture["skill_root"]
            project = fixture["project"]

            doctor_report = root / "doctor.json"
            doctor_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "skill_root": str(skill_root),
                        "session_id": "tiny-run-1",
                        "owned": True,
                        "healthy": True,
                        "target": "tiny-cli",
                        "build": "fixture-v1",
                        "endpoint": "cli",
                        "auth": "not-required",
                        "disposable": True,
                    }
                ),
                encoding="utf-8",
            )
            doctor_result = run_helper("doctor", "--skill-root", str(skill_root), "--report", str(doctor_report), "--json")
            self.assertEqual(doctor_result.returncode, 0, doctor_result.stderr)
            self.assertEqual(json_output(doctor_result)["status"], "pass")

            bad_doctor = root / "bad-doctor.json"
            bad_doctor.write_text(doctor_report.read_text(encoding="utf-8").replace('"owned": true', '"owned": false'), encoding="utf-8")
            bad_doctor_result = run_helper("doctor", "--skill-root", str(skill_root), "--report", str(bad_doctor), "--json")
            self.assertEqual(bad_doctor_result.returncode, 1)

            product_file = project / "product.txt"
            product_file.write_text("must remain untouched\n", encoding="utf-8")
            outside_plan = root / "outside.json"
            outside_plan.write_text(
                json.dumps(
                    {
                        "changes": [
                            {
                                "path": "../../../../product.txt",
                                "kind": "doc-drift",
                                "old": "must remain untouched",
                                "new": "changed",
                                "reason": "invalid boundary",
                                "source": "app.py:1",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            outside_result = run_helper("apply", "--skill-root", str(skill_root), "--patch-plan", str(outside_plan), "--json")
            self.assertEqual(outside_result.returncode, 2)
            self.assertEqual(product_file.read_text(encoding="utf-8"), "must remain untouched\n")

            run_root = root / "run"
            owned = run_root / "state"
            owned.mkdir(parents=True)
            (owned / "attempt.json").write_text("scratch\n", encoding="utf-8")
            cleanup_evidence = project / "artifacts" / "cleanup.txt"
            cleanup_evidence.write_text("proof\n", encoding="utf-8")
            manifest = run_root / "cleanup.json"
            manifest.write_text(
                json.dumps({"owned_paths": ["state/attempt.json"], "evidence_paths": ["artifacts/cleanup.txt"]}),
                encoding="utf-8",
            )
            cleanup_result = run_helper(
                "cleanup",
                "--run-root",
                str(run_root),
                "--manifest",
                str(manifest),
                "--project-root",
                str(project),
                "--json",
            )
            self.assertEqual(cleanup_result.returncode, 0, cleanup_result.stderr)
            self.assertFalse((owned / "attempt.json").exists())
            self.assertEqual(cleanup_evidence.read_text(encoding="utf-8"), "proof\n")

            bad_manifest = run_root / "bad-cleanup.json"
            bad_manifest.write_text(
                json.dumps({"owned_paths": ["../product.txt"], "evidence_paths": ["artifacts/cleanup.txt"]}),
                encoding="utf-8",
            )
            bad_cleanup = run_helper(
                "cleanup",
                "--run-root",
                str(run_root),
                "--manifest",
                str(bad_manifest),
                "--project-root",
                str(project),
                "--json",
            )
            self.assertEqual(bad_cleanup.returncode, 2)
            self.assertEqual(product_file.read_text(encoding="utf-8"), "must remain untouched\n")

    def test_verified_unreachable_is_visible_but_blocks_completion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintain-verification-unreachable-") as temporary:
            root = Path(temporary)
            fixture = create_project(root, stale=False)
            live = fixture["live"]
            check_feature = fixture["skill_root"] / "features" / "check-state.md"
            check_feature.write_text(
                check_feature.read_text(encoding="utf-8")
                + "\n- This route requires operator entitlement is unavailable.\n",
                encoding="utf-8",
            )
            live["features"][0] = {
                "feature_id": "check-state",
                "status": "verified-unreachable",
                "route": "check command",
                "prerequisite": "operator entitlement is unavailable",
            }
            live_path = root / "unreachable-live.json"
            live_path.write_text(json.dumps(live), encoding="utf-8")
            result = run_helper(
                "audit",
                "--skill-root",
                str(fixture["skill_root"]),
                "--project-root",
                str(fixture["project"]),
                "--reader-report",
                str(fixture["reader_path"]),
                "--live-report",
                str(live_path),
                "--json",
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json_output(result)
            self.assertEqual(payload["outcome"], "blocked")
            self.assertTrue(any(item["kind"] == "verified-unreachable" for item in payload["findings"]))

    def test_skill_frontmatter_and_references_are_present(self) -> None:
        skill_root = ROOT / "skills" / "maintain-verification-skill"
        text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: maintain-verification-skill\n"))
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            if target.startswith(("http:", "https:", "#")):
                continue
            self.assertTrue((skill_root / target).is_file(), target)


if __name__ == "__main__":
    unittest.main()
