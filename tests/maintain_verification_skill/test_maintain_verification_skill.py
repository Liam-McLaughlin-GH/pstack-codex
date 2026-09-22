from __future__ import annotations

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

            run_root = project / "run"
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
