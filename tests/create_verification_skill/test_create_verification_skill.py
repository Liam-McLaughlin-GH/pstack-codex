from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "skills" / "create-verification-skill" / "scripts" / "scaffold.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tiny_app.py"
sys.path.insert(0, str(HELPER.parent))

import scaffold as scaffold_module  # noqa: E402
from scaffold import CollisionError, ScaffoldError, validate_spec  # noqa: E402


def command(*argv: str) -> dict[str, object]:
    return {"executable": sys.executable, "argv": ["tiny_app.py", *argv]}


def context() -> dict[str, list[str]]:
    return {
        "setup": ["Copy the tiny fixture into the project and seed the state file."],
        "environment": ["Run from the project root with the selected Python executable."],
        "seed_auth": ["The disposable state file contains ready; no authentication is required."],
        "isolation": ["Use a dedicated temporary project and scratch state for this run."],
        "ownership": ["This run starts the short-lived fixture process and may remove only its state file."],
        "evidence_scope": ["This proves the CLI output and state-file side effect, not a production integration."],
        "failure_signals": ["A non-zero exit code or an unexpected visible result fails the check."],
    }


def spec_for() -> dict[str, object]:
    app_context = context()
    feature_context = context()
    return {
        "app": {
            "name": "tiny-fixture",
            "label": "Tiny fixture",
            "description": "A tiny local command-line fixture.",
            "surface": "CLI",
            "harness": "shell",
            "launch": command("--help"),
            "ready": "the help command exits 0 and Doctor reports tiny fixture ready",
            "doctor": command("doctor", "--state", "scratch/state.txt"),
            "drive": command("check", "--state", "scratch/state.txt", "--expected", "ready"),
            "cleanup": command("cleanup", "--state", "scratch/state.txt"),
            **app_context,
            "disposable_root": "scratch",
            "evidence_root": "artifacts",
            "disposable_paths": ["scratch/state.txt"],
        },
        "baseline": [
            "Write ready to scratch/state.txt before Doctor.",
            "Run all commands from the project root.",
        ],
        "helpers": [
            "Use the literal argv command shown in each feature recipe.",
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
                    "Run the check command from a terminal.",
                ],
                **feature_context,
                "actors": ["operator"],
                "fixtures": ["state file containing ready at scratch/state.txt"],
                "actions": [
                    {
                        "action": "Check the ready state",
                        "command": command("check", "--state", "scratch/state.txt", "--expected", "ready"),
                        "expected_result": "exit code 0 and stdout contains check passed",
                        "failure_signals": ["exit code is not 0 or stdout lacks check passed"],
                        "evidence": {
                            "path": "artifacts/tiny-fixture/pass.json",
                            "proves": "the passing visible result and the ready state read-only check",
                        },
                    },
                    {
                        "action": "Check an incorrect expectation",
                        "command": command("check", "--state", "scratch/state.txt", "--expected", "wrong"),
                        "expected_result": "exit code 1 and stdout reports expected wrong and actual ready",
                        "failure_signals": ["the intentional mismatch does not return exit code 1"],
                        "evidence": {
                            "path": "artifacts/tiny-fixture/failure.json",
                            "proves": "the intentional failure signal and preserved evidence after cleanup",
                        },
                    },
                ],
                "gotchas": [
                    "A missing state file is a precondition failure, not a passing empty result.",
                ],
            }
        ],
    }


def spec_with_working_directory() -> dict[str, object]:
    spec = spec_for()
    app = spec["app"]  # type: ignore[assignment]
    app["disposable_root"] = "runner/scratch"  # type: ignore[index]
    app["disposable_paths"] = ["runner/scratch/state.txt"]  # type: ignore[index]
    for command_value in (
        app["launch"],  # type: ignore[index]
        app["doctor"],  # type: ignore[index]
        app["drive"],  # type: ignore[index]
        app["cleanup"],  # type: ignore[index]
    ):
        command_value["cwd"] = "runner"  # type: ignore[index]
    for action in spec["features"][0]["actions"]:  # type: ignore[index]
        action["command"]["cwd"] = "runner"  # type: ignore[index]
    return spec


def copy_fixture(project: Path) -> None:
    (project / "tiny_app.py").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")


def run_helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _generated_section_body(markdown: str, section: str) -> str:
    section_match = re.search(
        rf"^## {re.escape(section)}\n(?P<body>.*?)(?=^## |\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    if section_match is None:
        raise AssertionError(f"missing generated section {section}")

    return section_match.group("body")


def _generated_code_value(line: str, label: str) -> str:
    match = re.fullmatch(
        rf"- {re.escape(label)}: (?P<ticks>`+)(?P<value>.*?)(?P=ticks)\s*",
        line.strip(),
    )
    if match is None:
        raise AssertionError(f"could not parse generated {label}: {line}")
    return match.group("value").strip()


def generated_command_context(
    markdown: str, section: str, label: str = "Command"
) -> tuple[list[str], str | None]:
    body = _generated_section_body(markdown, section)
    lines = body.splitlines()
    command_line = next(
        (line for line in lines if line.strip().startswith(f"- {label}: ")),
        None,
    )
    if command_line is None:
        raise AssertionError(f"missing generated {label} in {section}")
    command = shlex.split(_generated_code_value(command_line, label))
    cwd_line = next(
        (line for line in lines if line.strip().startswith("- Working directory: ")),
        None,
    )
    cwd = _generated_code_value(cwd_line, "Working directory") if cwd_line else None
    return command, cwd


def _generated_action_block(skill_root: Path, action_name: str) -> str:
    text = (skill_root / "features" / "check-state.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^- \*\*{re.escape(action_name)}\.\*\*$(.*?)(?=^- \*\*|^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing generated action {action_name}")
    return match.group(1)


def generated_action_context(skill_root: Path, action_name: str) -> tuple[list[str], str | None]:
    block = _generated_action_block(skill_root, action_name)
    lines = block.splitlines()
    line = next((line for line in lines if line.strip().startswith("- Action: ")), None)
    if line is None:
        raise AssertionError(f"missing generated Action for {action_name}")
    command = shlex.split(_generated_code_value(line, "Action"))
    cwd_line = next(
        (line for line in lines if line.strip().startswith("- Working directory: ")),
        None,
    )
    cwd = _generated_code_value(cwd_line, "Working directory") if cwd_line else None
    return command, cwd


def generated_action_evidence_path(skill_root: Path, action_name: str) -> str:
    block = _generated_action_block(skill_root, action_name)
    line = next((line for line in block.splitlines() if line.strip().startswith("- Evidence: ")), None)
    if line is None:
        raise AssertionError(f"missing generated Evidence for {action_name}")
    match = re.match(r"- Evidence: (`+)(.*?)\1(?:,|$)", line.strip())
    if match is None:
        raise AssertionError(f"could not parse generated evidence: {line}")
    return match.group(2).strip()


def generated_disposable_paths(skill_root: Path) -> list[str]:
    text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    line = next((line for line in text.splitlines() if line.startswith("- Disposable paths: ")), None)
    if line is None:
        raise AssertionError("missing generated disposable paths")
    return re.findall(r"`([^`]+)`", line)


def run_literal(
    argv: list[str],
    project: Path,
    *,
    cwd: str | None = None,
) -> dict[str, object]:
    working_directory = project if cwd is None else project / cwd
    completed = subprocess.run(
        argv,
        cwd=working_directory,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "command": argv,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
        "visible_result": (completed.stdout or completed.stderr).strip(),
    }


class CreateVerificationSkillBehaviorTests(unittest.TestCase):
    def seed_generated_disposable_state(self, project: Path, skill_root: Path) -> list[Path]:
        targets = [project / path for path in generated_disposable_paths(skill_root)]
        self.assertTrue(targets)
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ready\n", encoding="utf-8")
        return targets

    def create_skill(
        self,
        root: Path,
        *,
        spec: dict[str, object] | None = None,
        fixture_subdir: str | None = None,
    ) -> tuple[Path, dict[str, object]]:
        project = root / "project"
        project.mkdir()
        fixture_project = project if fixture_subdir is None else project / fixture_subdir
        fixture_project.mkdir(parents=True, exist_ok=True)
        copy_fixture(fixture_project)
        spec = spec or spec_for()
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
        self.seed_generated_disposable_state(project, skill_root)
        return skill_root, spec

    def execute_generated_attempt(
        self,
        project: Path,
        skill_root: Path,
        *,
        action_name: str,
        expected_exit: int,
        expected_text: str,
    ) -> None:
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        launch, launch_cwd = generated_command_context(skill_text, "Launch")
        doctor, doctor_cwd = generated_command_context(skill_text, "Doctor")
        drive, drive_cwd = generated_command_context(skill_text, "Drive")
        cleanup, cleanup_cwd = generated_command_context(skill_text, "Cleanup")
        action, action_cwd = generated_action_context(skill_root, action_name)
        if action_name == "Check the ready state":
            self.assertEqual(action, drive)
            self.assertEqual(action_cwd, drive_cwd)

        command = drive if action_name == "Check the ready state" else action
        command_cwd = drive_cwd if action_name == "Check the ready state" else action_cwd
        records = [
            run_literal(launch, project, cwd=launch_cwd),
            run_literal(doctor, project, cwd=doctor_cwd),
        ]
        drive_record = run_literal(command, project, cwd=command_cwd)
        records.append(drive_record)
        self.assertEqual(records[0]["exit_code"], 0)
        self.assertEqual(records[1]["exit_code"], 0, records[1])
        self.assertEqual(drive_record["exit_code"], expected_exit, drive_record)
        self.assertIn(expected_text, str(drive_record["visible_result"]))
        state_argument = next(
            doctor[index + 1]
            for index, value in enumerate(doctor)
            if value == "--state" and index + 1 < len(doctor)
        )
        state_path = (project if doctor_cwd is None else project / doctor_cwd) / state_argument
        disposable_paths = generated_disposable_paths(skill_root)
        self.assertIn(str(state_path.relative_to(project)), disposable_paths)
        records[-1]["read_only_side_effect"] = state_path.read_text(encoding="utf-8").strip()

        evidence = project / generated_action_evidence_path(skill_root, action_name)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        cleanup_record = run_literal(cleanup, project, cwd=cleanup_cwd)
        self.assertEqual(cleanup_record["exit_code"], 0, cleanup_record)
        self.assertFalse(state_path.exists())
        self.assertTrue(evidence.is_file(), "generated cleanup must not remove proof")
        records.append(cleanup_record)
        evidence.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        retained = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertEqual(retained[-1]["exit_code"], 0)
        self.assertIn("stdout", retained[0])
        self.assertIn("stderr", retained[0])
        self.assertIn("visible_result", retained[2])

    def test_execute_literal_generated_flow_and_retain_pass_and_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-flow-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            project = root / "project"

            validated = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.execute_generated_attempt(
                project,
                skill_root,
                action_name="Check the ready state",
                expected_exit=0,
                expected_text="check passed: ready",
            )

            self.seed_generated_disposable_state(project, skill_root)
            self.execute_generated_attempt(
                project,
                skill_root,
                action_name="Check an incorrect expectation",
                expected_exit=1,
                expected_text="expected='wrong' actual='ready'",
            )

    def test_generated_cwd_is_literal_executable_and_cleanup_cannot_enter_evidence(self) -> None:
        spec = spec_with_working_directory()
        for cleanup_cwd in ("artifacts", "artifacts/run"):
            invalid_spec = spec_with_working_directory()
            invalid_spec["app"]["cleanup"]["cwd"] = cleanup_cwd  # type: ignore[index]
            with self.assertRaises(ScaffoldError):
                validate_spec(invalid_spec)

        with tempfile.TemporaryDirectory(prefix="create-verification-cwd-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root, spec=spec, fixture_subdir="runner")
            skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            for section in ("Launch", "Doctor", "Drive", "Cleanup"):
                self.assertEqual(generated_command_context(skill_text, section)[1], "runner")
            self.assertEqual(
                generated_action_context(skill_root, "Check the ready state")[1],
                "runner",
            )
            validated = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.execute_generated_attempt(
                root / "project",
                skill_root,
                action_name="Check the ready state",
                expected_exit=0,
                expected_text="check passed: ready",
            )

            skill_file = skill_root / "SKILL.md"
            original = skill_file.read_text(encoding="utf-8")
            cleanup_start = original.index("## Cleanup")
            cleanup_end = original.index("## Helpers")
            cleanup = original[cleanup_start:cleanup_end].replace(
                "- Working directory: `runner`",
                "- Working directory: `artifacts`",
                1,
            )
            skill_file.write_text(original[:cleanup_start] + cleanup + original[cleanup_end:], encoding="utf-8")
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("Cleanup.Command.cwd" in error for error in errors), errors)

    def test_existing_tree_revalidates_all_emitted_command_contracts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-command-validation-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            skill_file = skill_root / "SKILL.md"
            original_skill = skill_file.read_text(encoding="utf-8")
            launch_line = next(
                line for line in original_skill.splitlines() if line.startswith("- Command: ")
            )
            skill_file.write_text(
                original_skill.replace(launch_line, launch_line.replace("`", "", 2), 1),
                encoding="utf-8",
            )
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("Launch Command" in error and "inline code" in error for error in errors), errors)
            skill_file.write_text(original_skill, encoding="utf-8")

            for section in ("Doctor", "Drive"):
                section_command = next(
                    line
                    for line in _generated_section_body(original_skill, section).splitlines()
                    if line.strip().startswith("- Command: ")
                )
                skill_file.write_text(
                    original_skill.replace(section_command, section_command.replace("`", "", 2), 1),
                    encoding="utf-8",
                )
                errors = scaffold_module.validate_generated_skill(skill_root)
                self.assertTrue(
                    any(f"{section} Command" in error and "inline code" in error for error in errors),
                    errors,
                )
                skill_file.write_text(original_skill, encoding="utf-8")

            cleanup_line = next(
                line
                for line in _generated_section_body(original_skill, "Cleanup").splitlines()
                if line.strip().startswith("- Command: ")
            )
            skill_file.write_text(
                original_skill.replace(cleanup_line, "- Command: `rm -rf /tmp`", 1),
                encoding="utf-8",
            )
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("Cleanup.Command.argv" in error for error in errors), errors)
            skill_file.write_text(original_skill, encoding="utf-8")

            skill_file.write_text(
                original_skill.replace(cleanup_line, "- Command: `rm -rf artifacts`", 1),
                encoding="utf-8",
            )
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("protected path 'artifacts'" in error for error in errors), errors)
            skill_file.write_text(original_skill, encoding="utf-8")

            feature_file = skill_root / "features" / "check-state.md"
            original_feature = feature_file.read_text(encoding="utf-8")
            action_line = next(
                line for line in original_feature.splitlines() if line.strip().startswith("- Action: ")
            )
            feature_file.write_text(
                original_feature.replace(
                    action_line,
                    f"{action_line[: len(action_line) - len(action_line.lstrip())]}- Action: `sh -c 'echo unsafe'`",
                    1,
                ),
                encoding="utf-8",
            )
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("free-form shell" in error for error in errors), errors)

    def test_no_pyyaml_frontmatter_fallback_rejects_non_string_scalars(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-frontmatter-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            skill_file = skill_root / "SKILL.md"
            original = skill_file.read_text(encoding="utf-8")
            name_line = next(line for line in original.splitlines() if line.startswith("name: "))
            description_line = next(
                line for line in original.splitlines() if line.startswith("description: ")
            )
            replacements = {
                name_line: "name: 42",
                description_line: "description: null",
            }
            for source, replacement in replacements.items():
                skill_file.write_text(original.replace(source, replacement, 1), encoding="utf-8")
                with mock.patch.dict(sys.modules, {"yaml": None}):
                    errors = scaffold_module.validate_generated_skill(skill_root)
                self.assertTrue(any("must be a string" in error for error in errors), errors)
                skill_file.write_text(original, encoding="utf-8")

    def test_collision_including_broken_symlink_preserves_existing_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-collision-") as temporary:
            root = Path(temporary)
            skill_root, spec = self.create_skill(root)
            before = (skill_root / "SKILL.md").read_bytes()
            spec_path = root / "spec.json"
            second = run_helper("scaffold", "--spec", str(spec_path), "--project-root", str(root / "project"))
            self.assertEqual(second.returncode, 2)
            self.assertEqual((skill_root / "SKILL.md").read_bytes(), before)

            linked_project = root / "linked-project"
            linked_project.mkdir()
            copy_fixture(linked_project)
            (linked_project / "scratch").mkdir()
            (linked_project / "scratch" / "state.txt").write_text("ready\n", encoding="utf-8")
            (linked_project / ".agents" / "skills").mkdir(parents=True)
            linked_target = linked_project / ".agents" / "skills" / "verify-tiny-fixture"
            os.symlink(str(root / "not-there"), linked_target)
            broken = run_helper(
                "scaffold", "--spec", str(spec_path), "--project-root", str(linked_project)
            )
            self.assertEqual(broken.returncode, 2)
            self.assertTrue(linked_target.is_symlink())

    def test_invalid_spec_and_complete_feature_contract_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-invalid-") as temporary:
            root = Path(temporary)
            spec = spec_for()
            del spec["features"][0]["actions"][0]["evidence"]  # type: ignore[index]
            with self.assertRaises(ScaffoldError):
                validate_spec(spec)

            skill_root, _ = self.create_skill(root)
            index = skill_root / "features" / "README.md"
            index.write_text(
                index.read_text(encoding="utf-8").replace("./check-state.md", "./missing.md"),
                encoding="utf-8",
            )
            rejected = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("link target does not exist", rejected.stderr)
            self.assertIn("link every feature exactly once", rejected.stderr)

            index.write_text(
                index.read_text(encoding="utf-8").replace("./missing.md", "./check-state.md"),
                encoding="utf-8",
            )
            index.write_text(index.read_text(encoding="utf-8") + "\n[broken][missing-reference]\n", encoding="utf-8")
            rejected = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("unresolved Markdown reference", rejected.stderr)

            index.write_text(
                index.read_text(encoding="utf-8").replace("\n[broken][missing-reference]\n", "\n"),
                encoding="utf-8",
            )
            feature = skill_root / "features" / "check-state.md"
            original_feature = feature.read_text(encoding="utf-8")
            feature.write_text(original_feature.replace("# Check fixture state\n", ""), encoding="utf-8")
            rejected = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("must start with an H1", rejected.stderr)
            feature.write_text(original_feature, encoding="utf-8")

            feature.write_text(
                original_feature.replace(
                    "A user can check the current fixture state from a terminal and see a result.\n",
                    "",
                ),
                encoding="utf-8",
            )
            rejected = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("summary paragraph", rejected.stderr)
            feature.write_text(original_feature, encoding="utf-8")

            feature.write_text(
                original_feature.replace("Preconditions:", "Setup only:"),
                encoding="utf-8",
            )
            rejected = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("must start with Preconditions", rejected.stderr)
            feature.write_text(
                original_feature.replace("Failure signals:", "Signal:"),
                encoding="utf-8",
            )
            rejected = run_helper("validate", "--skill-root", str(skill_root))
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("missing Failure signals", rejected.stderr)

    def test_typed_commands_paths_frontmatter_and_markdown_are_safe(self) -> None:
        spec = spec_for()
        spec["app"]["launch"] = "python tiny_app.py --help"  # type: ignore[index]
        with self.assertRaises(ScaffoldError):
            validate_spec(spec)
        spec = spec_for()
        spec["features"][0]["actions"][0]["command"] = "python tiny_app.py check"  # type: ignore[index]
        with self.assertRaises(ScaffoldError):
            validate_spec(spec)
        spec = spec_for()
        spec["app"]["doctor"] = {"executable": "sh", "argv": ["-c", "echo unsafe"]}  # type: ignore[index]
        with self.assertRaises(ScaffoldError):
            validate_spec(spec)

        spec = spec_for()
        spec["app"]["disposable_root"] = "/tmp"  # type: ignore[index]
        with self.assertRaises(ScaffoldError):
            validate_spec(spec)
        spec = spec_for()
        spec["features"][0]["actions"][0]["evidence"]["path"] = "../proof.json"  # type: ignore[index]
        with self.assertRaises(ScaffoldError):
            validate_spec(spec)

        spec = spec_for()
        spec["app"]["label"] = "Tiny [fixture](javascript:alert(1))"  # type: ignore[index]
        validate_spec(spec)
        spec = spec_for()
        spec["app"]["name"] = "a" * 58  # type: ignore[index]
        with self.assertRaises(ScaffoldError):
            validate_spec(spec)

        with tempfile.TemporaryDirectory(prefix="create-verification-safe-") as temporary:
            root = Path(temporary)
            skill_root, spec = self.create_skill(root)
            feature = spec["features"][0]
            feature["summary"] = "Literal [text](javascript:alert(1)) and `ticks`"  # type: ignore[index]
            spec_path = root / "unsafe-text.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            project = root / "text-project"
            project.mkdir()
            copy_fixture(project)
            (project / "scratch").mkdir()
            (project / "scratch" / "state.txt").write_text("ready\n", encoding="utf-8")
            created = run_helper(
                "scaffold", "--spec", str(spec_path), "--project-root", str(project)
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            generated = project / ".agents" / "skills" / "verify-tiny-fixture"
            self.assertNotRegex(
                (generated / "features" / "check-state.md").read_text(encoding="utf-8"),
                r"\[[^\]]+\]\(javascript:",
            )

            malformed = generated / "SKILL.md"
            malformed.write_text(
                malformed.read_text(encoding="utf-8").replace(
                    'description: "', "description: [unterminated\n# description: \"", 1
                ),
                encoding="utf-8",
            )
            invalid = run_helper("validate", "--skill-root", str(generated))
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("frontmatter", invalid.stderr.lower())

    def test_symlinks_location_collision_race_and_parent_rollback_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-boundary-") as temporary:
            root = Path(temporary)
            skill_root, spec = self.create_skill(root)
            outside = root / "outside"
            outside.mkdir()
            outside_skill = outside / "SKILL.md"
            outside_skill.write_text("outside\n", encoding="utf-8")

            skill_file = skill_root / "SKILL.md"
            original_skill = skill_file.read_bytes()
            skill_file.unlink()
            os.symlink(outside_skill, skill_file)
            self.assertTrue(scaffold_module.validate_generated_skill(skill_root))
            skill_file.unlink()
            skill_file.write_bytes(original_skill)

            feature = skill_root / "features" / "check-state.md"
            original_feature = feature.read_bytes()
            feature.unlink()
            os.symlink(outside_skill, feature)
            self.assertTrue(scaffold_module.validate_generated_skill(skill_root))
            feature.unlink()
            feature.write_bytes(original_feature)

            alias = root / "alias"
            os.symlink(root / "project", alias, target_is_directory=True)
            self.assertTrue(
                scaffold_module.validate_generated_skill(
                    alias / ".agents" / "skills" / "verify-tiny-fixture"
                )
            )
            misplaced = root / "verify-tiny-fixture"
            shutil.copytree(skill_root, misplaced)
            self.assertTrue(scaffold_module.validate_generated_skill(misplaced))

            project = root / "race-project"
            project.mkdir()
            copy_fixture(project)
            target = project / ".agents" / "skills" / "verify-tiny-fixture"
            original_write = scaffold_module._write_tree

            def race_write(current_spec: dict[str, object], staging: Path) -> None:
                original_write(current_spec, staging)
                target.mkdir(parents=True)
                (target / "sentinel").write_text("preserve\n", encoding="utf-8")

            with mock.patch.object(scaffold_module, "_write_tree", race_write):
                with self.assertRaises(CollisionError):
                    scaffold_module.scaffold(spec, project)
            self.assertEqual((target / "sentinel").read_text(encoding="utf-8"), "preserve\n")

            rollback_project = root / "rollback-project"
            rollback_project.mkdir()
            copy_fixture(rollback_project)
            with mock.patch.object(scaffold_module, "validate_generated_skill", return_value=("forced",)):
                with self.assertRaises(ScaffoldError):
                    scaffold_module.scaffold(spec, rollback_project)
            self.assertFalse((rollback_project / ".agents").exists())

            symlink_parent = root / "symlink-parent-project"
            symlink_parent.mkdir()
            copy_fixture(symlink_parent)
            outside_parent = root / "outside-parent"
            outside_parent.mkdir()
            os.symlink(outside_parent, symlink_parent / ".agents", target_is_directory=True)
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            refused = run_helper(
                "scaffold", "--spec", str(spec_path), "--project-root", str(symlink_parent)
            )
            self.assertEqual(refused.returncode, 2)
            self.assertFalse((outside_parent / "skills").exists())


if __name__ == "__main__":
    unittest.main()
