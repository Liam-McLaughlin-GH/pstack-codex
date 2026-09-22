from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "skills" / "create-verification-skill" / "scripts" / "scaffold.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tiny_app.py"
sys.path.insert(0, str(HELPER.parent))

from scaffold import ScaffoldError, validate_spec  # noqa: E402


def spec_for(state_path: Path) -> dict[str, object]:
    command_prefix = f"{shlex.quote(sys.executable)} {shlex.quote(str(FIXTURE))}"
    return {
        "app": {
            "name": "tiny-fixture",
            "label": "Tiny fixture",
            "description": "A tiny local command-line fixture.",
            "surface": "CLI",
            "harness": "shell",
            "launch": f"{command_prefix} --help",
            "ready": "the help command exits 0 and Doctor reports `tiny fixture ready`",
            "doctor": f"{command_prefix} doctor --state {shlex.quote(str(state_path))}",
            "drive": f"{command_prefix} check --state {shlex.quote(str(state_path))} --expected ready",
            "evidence": "save stdout, stderr, and exit code under artifacts/tiny-fixture/check.txt",
            "cleanup": f"remove the disposable state file at {shlex.quote(str(state_path))}",
        },
        "baseline": [
            f"Write `ready` to the disposable state file at {state_path}.",
            "Run commands from the project root.",
        ],
        "helpers": [
            "Use the fixture command shown in each feature recipe.",
        ],
        "features": [
            {
                "id": "check-state",
                "title": "Check fixture state",
                "summary": "A user can check the current fixture state from a terminal and see a result.",
                "sub_features": [
                    {"id": "check-ready", "description": "reports a matching ready state."},
                    {"id": "check-mismatch", "description": "returns a failure when the expectation is wrong."},
                ],
                "entry_points": [
                    "Run the `check` command from a terminal.",
                ],
                "actors": ["operator"],
                "fixtures": [f"state file containing `ready` at {state_path}"],
                "actions": [
                    {
                        "action": "Check the ready state",
                        "command": f"{command_prefix} check --state {shlex.quote(str(state_path))} --expected ready",
                        "expected_result": "exit code 0 and stdout contains `check passed`",
                        "evidence": "record the command, stdout, stderr, and exit code",
                    }
                ],
                "gotchas": [
                    "A missing state file is a precondition failure, not a passing empty result.",
                ],
            }
        ],
    }


def run_helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class CreateVerificationSkillBehaviorTests(unittest.TestCase):
    def test_scaffold_and_validate_tiny_fixture_then_capture_intentional_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-skill-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            state = project / "scratch" / "state.txt"
            state.parent.mkdir()
            state.write_text("ready\n", encoding="utf-8")
            spec = spec_for(state)
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

            generated = run_helper(
                "scaffold",
                "--spec",
                str(spec_path),
                "--project-root",
                str(project),
                "--json",
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            skill_root = Path(json.loads(generated.stdout)["skill_root"])
            self.assertTrue((skill_root / "SKILL.md").is_file())
            self.assertTrue((skill_root / "features" / "check-state.md").is_file())

            validated = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(validated.returncode, 0, validated.stderr)

            launch = subprocess.run(
                [sys.executable, str(FIXTURE), "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(launch.returncode, 0, launch.stderr)
            doctor = subprocess.run(
                shlex.split(spec["app"]["doctor"]),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)

            command = shlex.split(spec["features"][0]["actions"][0]["command"])
            passing = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
            self.assertIn("check passed", passing.stdout)

            intentional_failure = subprocess.run(
                [*command[:-1], "wrong"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(intentional_failure.returncode, 1)
            self.assertIn("expected='wrong'", intentional_failure.stdout)
            self.assertIn("actual='ready'", intentional_failure.stdout)

            evidence = root / "artifacts" / "tiny-fixture" / "check.txt"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                "PASS\n"
                f"command: {spec['features'][0]['actions'][0]['command']}\n"
                f"stdout: {passing.stdout.strip()}\n"
                "FAIL (intentional)\n"
                f"stdout: {intentional_failure.stdout.strip()}\n",
                encoding="utf-8",
            )
            state.unlink()
            self.assertTrue(evidence.is_file(), "cleanup must not remove proof")

    def test_collision_including_broken_symlink_preserves_existing_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-collision-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            state = root / "state.txt"
            state.write_text("ready\n", encoding="utf-8")
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec_for(state)), encoding="utf-8")

            first = run_helper("scaffold", "--spec", str(spec_path), "--project-root", str(project))
            self.assertEqual(first.returncode, 0, first.stderr)
            target = project / ".agents" / "skills" / "verify-tiny-fixture"
            before = (target / "SKILL.md").read_bytes()
            second = run_helper("scaffold", "--spec", str(spec_path), "--project-root", str(project))
            self.assertEqual(second.returncode, 2)
            self.assertEqual((target / "SKILL.md").read_bytes(), before)

            linked_project = root / "linked-project"
            (linked_project / ".agents" / "skills").mkdir(parents=True)
            linked_target = linked_project / ".agents" / "skills" / "verify-tiny-fixture"
            os.symlink(str(root / "not-there"), linked_target)
            linked_spec = root / "linked-spec.json"
            linked_spec.write_text(json.dumps(spec_for(state)), encoding="utf-8")
            broken = run_helper(
                "scaffold", "--spec", str(linked_spec), "--project-root", str(linked_project)
            )
            self.assertEqual(broken.returncode, 2)
            self.assertTrue(linked_target.is_symlink())

    def test_invalid_spec_and_corrupt_generated_skill_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-invalid-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            state = root / "state.txt"
            state.write_text("ready\n", encoding="utf-8")
            spec = spec_for(state)
            del spec["features"][0]["actions"][0]["evidence"]  # type: ignore[index]
            with self.assertRaises(ScaffoldError):
                validate_spec(spec)

            valid_spec = spec_for(state)
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(valid_spec), encoding="utf-8")
            created = run_helper("scaffold", "--spec", str(spec_path), "--project-root", str(project))
            self.assertEqual(created.returncode, 0, created.stderr)
            skill_root = project / ".agents" / "skills" / "verify-tiny-fixture"
            feature = skill_root / "features" / "check-state.md"
            feature.write_text(feature.read_text(encoding="utf-8").replace("Evidence:", "Proof:"), encoding="utf-8")
            rejected = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("Evidence:", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
