from __future__ import annotations

import json
import errno
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
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

    def test_cleanup_targets_are_normalized_and_bound_to_disposable_state(self) -> None:
        rejected = (
            ["-rf", "./artifacts"],
            ["-rf", "././artifacts"],
            ["-rf", "."],
            ["-rf", "unrelated"],
            ["-rf", "scratch/other"],
            ["-rf", "scratch/../scratch"],
            ["-rf", r"scratch\state.txt"],
            ["-rf", "C:relative-state"],
            ["-rf", "//server/share/state.txt"],
        )
        for argv in rejected:
            with self.subTest(argv=argv):
                spec = spec_for()
                spec["app"]["cleanup"] = {"executable": "rm", "argv": argv}  # type: ignore[index]
                with self.assertRaises(ScaffoldError):
                    validate_spec(spec)

        for argv in (["-rf", "./scratch/./state.txt"], ["-rf", "scratch"]):
            with self.subTest(accepted_argv=argv):
                spec = spec_for()
                spec["app"]["cleanup"] = {"executable": "rm", "argv": argv}  # type: ignore[index]
                validate_spec(spec)

    def test_existing_tree_revalidates_cleanup_policy_and_root_separation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-cleanup-policy-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            skill_file = skill_root / "SKILL.md"
            original_skill = skill_file.read_text(encoding="utf-8")
            cleanup_section = _generated_section_body(original_skill, "Cleanup")
            cleanup_line = next(
                line for line in cleanup_section.splitlines() if line.strip().startswith("- Command: ")
            )
            for rendered in ("rm -rf ./artifacts", "rm -rf scratch/other", "rm -rf ."):
                with self.subTest(rendered=rendered):
                    skill_file.write_text(
                        original_skill.replace(cleanup_line, f"- Command: `{rendered}`", 1),
                        encoding="utf-8",
                    )
                    errors = scaffold_module.validate_generated_skill(skill_root)
                    self.assertTrue(any("Cleanup.Command.argv" in error for error in errors), errors)
                    skill_file.write_text(original_skill, encoding="utf-8")

            index = skill_root / "features" / "README.md"
            original_index = index.read_text(encoding="utf-8")
            skill_file.write_text(
                original_skill.replace("- Disposable root: `scratch`", "- Disposable root: `artifacts`", 1),
                encoding="utf-8",
            )
            index.write_text(
                original_index.replace("- Disposable root: `scratch`", "- Disposable root: `artifacts`", 1),
                encoding="utf-8",
            )
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("roots must not overlap" in error for error in errors), errors)

    def test_indirect_shell_and_interpreter_code_boundaries_are_rejected(self) -> None:
        cases = (
            ("env", ["sh", "-c", "echo unsafe"]),
            ("busybox", ["sh", "-c", "echo unsafe"]),
            ("xargs", ["sh", "-c", "echo unsafe"]),
            ("python", ["-c", "print('unsafe')"]),
            ("py", ["-c", "print('unsafe')"]),
            ("pythonw", ["-c", "print('unsafe')"]),
            ("pythonw.exe", ["--command", "print('unsafe')"]),
            ("node", ["-e", "console.log('unsafe')"]),
            ("node", ["-p", "process.env.SECRET"]),
            ("node", ["--print", "process.env.SECRET"]),
        )
        for executable, argv in cases:
            with self.subTest(executable=executable):
                spec = spec_for()
                spec["app"]["doctor"] = {"executable": executable, "argv": argv}  # type: ignore[index]
                with self.assertRaises(ScaffoldError):
                    validate_spec(spec)

    def test_versioned_and_platform_suffixed_command_boundaries_are_rejected(self) -> None:
        cases = (
            ("pypy3", ["-c", "unsafe"]),
            ("ruby3.2", ["-e", "unsafe"]),
            ("python3.exe", ["-c", "unsafe"]),
            ("bash.exe", []),
            ("powershell.exe", ["-command", "unsafe"]),
            ("env.exe", ["python3.exe", "-c", "unsafe"]),
        )
        for executable, argv in cases:
            with self.subTest(executable=executable):
                spec = spec_for()
                spec["app"]["doctor"] = {"executable": executable, "argv": argv}  # type: ignore[index]
                with self.assertRaises(ScaffoldError):
                    validate_spec(spec)

    def test_command_arguments_reject_windows_and_traversal_path_forms(self) -> None:
        unsafe_values = (
            r"scratch\state.txt",
            "C:relative-state",
            "C:/absolute-state",
            r"\\server\share\state.txt",
            "//server/share/state.txt",
            "../outside/state.txt",
            "scratch/../outside/state.txt",
        )
        for value in unsafe_values:
            with self.subTest(value=value):
                spec = spec_for()
                spec["app"]["doctor"] = command("doctor", "--state", value)  # type: ignore[index]
                with self.assertRaises(ScaffoldError):
                    validate_spec(spec)

    def test_optional_yaml_non_string_mapping_keys_are_bounded_errors(self) -> None:
        class FakeSafeLoader:
            @classmethod
            def add_constructor(cls, *_args: object) -> None:
                return None

        fake_yaml = SimpleNamespace(
            SafeLoader=FakeSafeLoader,
            resolver=SimpleNamespace(BaseResolver=SimpleNamespace(DEFAULT_MAPPING_TAG="mapping")),
            load=lambda _source, Loader: {
                1: "not a valid frontmatter key",
                "name": "verify-tiny-fixture",
                "description": "bounded test",
            },
        )
        with tempfile.TemporaryDirectory(prefix="create-verification-frontmatter-key-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            with mock.patch.dict(sys.modules, {"yaml": fake_yaml}):
                errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(
                any("frontmatter mapping keys must be strings" in error for error in errors),
                errors,
            )
            self.assertFalse(any("Traceback" in error for error in errors), errors)

    def test_invalid_utf8_json_spec_is_a_bounded_scaffold_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-invalid-json-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            spec_path = root / "invalid.json"
            spec_path.write_bytes(b"{\xff")
            result = run_helper(
                "scaffold",
                "--spec",
                str(spec_path),
                "--project-root",
                str(project),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("could not read JSON spec", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_cleanup_rejects_unrecognized_bare_arguments_for_new_and_existing_specs(self) -> None:
        rejected_argv = (
            ["tiny_app.py", "cleanup", "scratch/state.txt", "unrelated"],
            ["tiny_app.py", "cleanup", "scratch", "unrelated"],
            ["tiny_app.py", "cleanup", "--state", "scratch/state.txt", "unrelated"],
        )
        for argv in rejected_argv:
            with self.subTest(argv=argv):
                spec = spec_for()
                spec["app"]["cleanup"] = {"executable": sys.executable, "argv": argv}  # type: ignore[index]
                with self.assertRaises(ScaffoldError):
                    validate_spec(spec)

        with tempfile.TemporaryDirectory(prefix="create-verification-cleanup-bare-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            skill_file = skill_root / "SKILL.md"
            original = skill_file.read_text(encoding="utf-8")
            cleanup_line = next(
                line
                for line in _generated_section_body(original, "Cleanup").splitlines()
                if line.strip().startswith("- Command: ")
            )
            rendered = f"{shlex.quote(sys.executable)} tiny_app.py cleanup --state scratch/state.txt unrelated"
            skill_file.write_text(
                original.replace(cleanup_line, f"- Command: `{rendered}`", 1),
                encoding="utf-8",
            )
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("must target declared disposable state" in error for error in errors), errors)

    def test_cleanup_accepts_bounded_python_unbuffered_and_module_prefixes(self) -> None:
        accepted_argv = (
            ["-u", "tiny_app.py", "cleanup", "--state", "scratch/state.txt"],
            ["-m", "tiny_app", "cleanup", "--state", "scratch/state.txt"],
            ["-u", "-m", "tiny_app", "cleanup", "--state", "scratch/state.txt"],
        )
        for argv in accepted_argv:
            with self.subTest(argv=argv):
                spec = spec_for()
                spec["app"]["cleanup"] = {"executable": sys.executable, "argv": argv}  # type: ignore[index]
                validate_spec(spec)


    def test_existing_tree_rejects_blank_context_and_evidence_proof(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-context-validation-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            skill_file = skill_root / "SKILL.md"
            original_skill = skill_file.read_text(encoding="utf-8")

            skill_lines = original_skill.splitlines()
            setup_index = next(index for index, line in enumerate(skill_lines) if line == "- Setup:")
            skill_lines[setup_index + 1] = "  - "
            skill_file.write_text("\n".join(skill_lines) + "\n", encoding="utf-8")
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("Setup must have a non-empty value" in error for error in errors), errors)
            skill_file.write_text(original_skill, encoding="utf-8")

            feature_file = skill_root / "features" / "check-state.md"
            original_feature = feature_file.read_text(encoding="utf-8")
            for label in ("Expected result", "Failure signals"):
                with self.subTest(label=label):
                    feature_lines = original_feature.splitlines()
                    line_index = next(
                        index
                        for index, line in enumerate(feature_lines)
                        if line.startswith("  - ") and line.strip().startswith(f"- {label}:")
                    )
                    indent = feature_lines[line_index][: len(feature_lines[line_index]) - len(feature_lines[line_index].lstrip())]
                    feature_lines[line_index] = f"{indent}- {label}:"
                    feature_file.write_text("\n".join(feature_lines) + "\n", encoding="utf-8")
                    errors = scaffold_module.validate_generated_skill(skill_root)
                    self.assertTrue(any(f"{label} must have a non-empty value" in error for error in errors), errors)
                    feature_file.write_text(original_feature, encoding="utf-8")

            feature_lines = original_feature.splitlines()
            evidence_index = next(
                index for index, line in enumerate(feature_lines) if line.strip().startswith("- Evidence: ")
            )
            evidence_line = feature_lines[evidence_index]
            feature_lines[evidence_index] = evidence_line.split(",", 1)[0] + ","
            feature_file.write_text("\n".join(feature_lines) + "\n", encoding="utf-8")
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("artifact path and proof" in error for error in errors), errors)

    def test_no_pyyaml_fallback_rejects_plain_yaml_mapping_syntax(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-frontmatter-syntax-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            skill_file = skill_root / "SKILL.md"
            original = skill_file.read_text(encoding="utf-8")
            description_line = next(
                line for line in original.splitlines() if line.startswith("description: ")
            )
            for replacement in ("description: foo: bar", "description: :"):
                with self.subTest(replacement=replacement):
                    skill_file.write_text(original.replace(description_line, replacement, 1), encoding="utf-8")
                    with mock.patch.dict(sys.modules, {"yaml": None}):
                        errors = scaffold_module.validate_generated_skill(skill_root)
                    self.assertTrue(any("frontmatter" in error for error in errors), errors)
                    skill_file.write_text(original, encoding="utf-8")

    def test_invalid_utf8_is_reported_as_a_validation_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-invalid-utf8-") as temporary:
            root = Path(temporary)
            skill_root, _ = self.create_skill(root)
            skill_file = skill_root / "SKILL.md"
            skill_file.write_bytes(skill_file.read_bytes() + b"\xff")
            errors = scaffold_module.validate_generated_skill(skill_root)
            self.assertTrue(any("UTF-8" in error for error in errors), errors)

    def test_child_publication_races_preserve_competing_file_and_symlink(self) -> None:
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"create-verification-child-race-{kind}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                spec = spec_for()
                target = project / ".agents" / "skills" / "verify-tiny-fixture"
                attacker_file = root / "attacker.txt"
                attacker_file.write_text("attacker\n", encoding="utf-8")
                original_lexists = os.path.lexists
                injected = False

                def race_lexists(path: str | os.PathLike[str]) -> bool:
                    nonlocal injected
                    present = original_lexists(path)
                    if Path(path) == target / "SKILL.md" and not present and not injected:
                        injected = True
                        if kind == "file":
                            Path(path).write_text("preserve\n", encoding="utf-8")
                        else:
                            os.symlink(attacker_file, path)
                    return present

                with mock.patch.object(os.path, "lexists", side_effect=race_lexists):
                    with self.assertRaises(CollisionError):
                        scaffold_module.scaffold(spec, project)
                self.assertTrue(injected)
                competing = target / "SKILL.md"
                if kind == "file":
                    self.assertEqual(competing.read_text(encoding="utf-8"), "preserve\n")
                else:
                    self.assertTrue(competing.is_symlink())
                    self.assertEqual(os.readlink(competing), str(attacker_file))

    def test_target_symlink_swap_during_publication_never_writes_attacker_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-target-race-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            outside = root / "attacker"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")
            target = project / ".agents" / "skills" / "verify-tiny-fixture"
            detached = root / "detached-reservation"
            swapped = False
            original_lexists = os.path.lexists

            def swap_target_after_probe(path: str | os.PathLike[str]) -> bool:
                nonlocal swapped
                present = original_lexists(path)
                if Path(path) == target / "SKILL.md" and not present and not swapped:
                    swapped = True
                    target.rename(detached)
                    os.symlink(outside, target, target_is_directory=True)
                return present

            with mock.patch.object(os.path, "lexists", side_effect=swap_target_after_probe):
                with self.assertRaises(CollisionError):
                    scaffold_module.scaffold(spec_for(), project)

            self.assertTrue(swapped)
            self.assertTrue(target.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
            self.assertFalse((outside / "SKILL.md").exists())
            self.assertFalse((outside / "features").exists())
            self.assertFalse((detached / "SKILL.md").exists())

    def test_directory_publication_binds_identity_to_open_descriptor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-directory-identity-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            target = project / ".agents" / "skills" / "verify-tiny-fixture"
            detached = root / "detached-features"
            replaced = False
            original_open = scaffold_module._open_directory_descriptor

            def race_open(
                name: Path | str,
                label: str,
                *,
                dir_fd: int | None = None,
                collision: bool = False,
            ) -> int:
                nonlocal replaced
                descriptor = original_open(name, label, dir_fd=dir_fd, collision=collision)
                if label == "published directory" and name == "features" and not replaced:
                    replaced = True
                    (target / "features").rename(detached)
                    (target / "features").mkdir()
                    (target / "features" / "sentinel.txt").write_text(
                        "preserve\n",
                        encoding="utf-8",
                    )
                return descriptor

            with mock.patch.object(scaffold_module, "_open_directory_descriptor", side_effect=race_open):
                with self.assertRaises(CollisionError):
                    scaffold_module.scaffold(spec_for(), project)

            self.assertTrue(replaced)
            self.assertEqual((target / "features" / "sentinel.txt").read_text(encoding="utf-8"), "preserve\n")
            self.assertTrue(detached.is_dir())
            self.assertFalse((detached / "README.md").exists())
            self.assertFalse((target / "SKILL.md").exists())

    def test_published_child_identity_failure_removes_empty_child(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-child-identity-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            target = project / ".agents" / "skills" / "verify-tiny-fixture"
            original_identity = scaffold_module._published_identity
            feature_calls = 0
            injected = False

            def fail_child_identity(
                parent_fd: int,
                name: str,
                *,
                is_directory: bool,
                entry_fd: int | None = None,
            ) -> scaffold_module.PublishedEntry:
                nonlocal feature_calls, injected
                if name == "features" and is_directory:
                    feature_calls += 1
                    if feature_calls == 2:
                        injected = True
                        raise ScaffoldError("forced child identity failure")
                return original_identity(
                    parent_fd,
                    name,
                    is_directory=is_directory,
                    entry_fd=entry_fd,
                )

            with mock.patch.object(
                scaffold_module,
                "_published_identity",
                side_effect=fail_child_identity,
            ):
                with self.assertRaises(ScaffoldError):
                    scaffold_module.scaffold(spec_for(), project)

            self.assertTrue(injected)
            self.assertFalse(target.exists())
            self.assertFalse((project / ".agents").exists())

    def test_rollback_refuses_a_replaced_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-staging-rollback-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            detached = root / "detached-staging"
            replacement: Path | None = None
            original_write = scaffold_module._write_tree

            def race_write(
                current_spec: dict[str, object],
                staging: Path,
                *,
                root_fd: int,
            ) -> None:
                nonlocal replacement
                original_write(current_spec, staging, root_fd=root_fd)
                staging.rename(detached)
                staging.mkdir()
                (staging / "sentinel.txt").write_text("preserve\n", encoding="utf-8")
                replacement = staging

            with mock.patch.object(scaffold_module, "_write_tree", side_effect=race_write):
                with self.assertRaises(ScaffoldError):
                    scaffold_module.scaffold(spec_for(), project)

            if replacement is None:
                self.fail("the staging replacement race did not run")
            self.assertEqual(
                (replacement / "sentinel.txt").read_text(encoding="utf-8"),
                "preserve\n",
            )
            self.assertTrue((detached / "SKILL.md").is_file())
            self.assertFalse((project / ".agents" / "skills" / "verify-tiny-fixture").exists())

    def test_staging_writes_stay_on_bound_descriptor_after_path_swap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-staging-write-race-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            outside = root / "outside"
            outside.mkdir()
            detached = root / "detached-staging"
            original_write = scaffold_module._write_tree
            swapped = False

            def race_write(
                current_spec: dict[str, object],
                staging: Path,
                *,
                root_fd: int,
            ) -> None:
                nonlocal swapped
                staging.rename(detached)
                os.symlink(outside, staging, target_is_directory=True)
                swapped = True
                original_write(current_spec, staging, root_fd=root_fd)

            with mock.patch.object(scaffold_module, "_write_tree", side_effect=race_write):
                with self.assertRaises(ScaffoldError):
                    scaffold_module.scaffold(spec_for(), project)

            self.assertTrue(swapped)
            self.assertTrue((detached / "SKILL.md").is_file())
            self.assertTrue((detached / "features" / "README.md").is_file())
            self.assertTrue((project / ".agents" / "skills").is_dir())
            staging_link = next(
                (project / ".agents" / "skills").glob(".verify-tiny-fixture.staging-*")
            )
            self.assertTrue(staging_link.is_symlink())
            self.assertFalse((outside / "SKILL.md").exists())
            self.assertFalse((outside / "features").exists())

    def test_staging_creation_stays_on_open_parent_when_parent_is_replaced_or_symlinked(self) -> None:
        for kind in ("replacement", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"create-verification-staging-parent-{kind}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                outside = root / "outside"
                outside.mkdir()
                detached = root / "detached-agents"
                original_create = scaffold_module._create_staging_directory
                created_in_detached: Path | None = None
                replaced = False

                def race_create(
                    parent_fd: int,
                    prefix: str,
                ) -> tuple[str, int, scaffold_module.PublishedEntry]:
                    nonlocal created_in_detached, replaced
                    agents = project / ".agents"
                    agents.rename(detached)
                    if kind == "replacement":
                        agents.mkdir()
                    else:
                        os.symlink(outside, agents, target_is_directory=True)
                    replaced = True
                    result = original_create(parent_fd, prefix)
                    created_in_detached = detached / "skills" / result[0]
                    self.assertTrue(created_in_detached.is_dir())
                    self.assertFalse((outside / "skills" / result[0]).exists())
                    return result

                with mock.patch.object(scaffold_module, "_create_staging_directory", side_effect=race_create):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(replaced)
                self.assertIsNotNone(created_in_detached)
                self.assertTrue(
                    (project / ".agents").is_symlink()
                    if kind == "symlink"
                    else (project / ".agents").is_dir()
                )
                self.assertFalse((outside / "skills").exists())

    def test_staging_descriptor_failure_cleans_own_reservation_and_preserves_competitor(self) -> None:
        for preserve_competing in (False, True):
            with self.subTest(preserve_competing=preserve_competing), tempfile.TemporaryDirectory(
                prefix="create-verification-staging-descriptor-failure-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                target = project / ".agents" / "skills" / "verify-tiny-fixture"
                staging_detached = root / "detached-staging"
                staging_sentinel = root / "staging-competitor.txt"
                staging_sentinel.write_text("preserve\n", encoding="utf-8")
                injected = False
                original_open = scaffold_module._open_directory_descriptor

                def fail_staging_descriptor(
                    name: Path | str,
                    label: str,
                    *,
                    dir_fd: int | None = None,
                    collision: bool = False,
                ) -> int:
                    nonlocal injected
                    if label == "staging directory" and not injected:
                        injected = True
                        staging = project / ".agents" / "skills" / str(name)
                        if preserve_competing:
                            staging.rename(staging_detached)
                            staging.mkdir()
                            (staging / "sentinel.txt").write_text(
                                staging_sentinel.read_text(encoding="utf-8"),
                                encoding="utf-8",
                            )
                        raise ScaffoldError("forced staging descriptor failure")
                    return original_open(name, label, dir_fd=dir_fd, collision=collision)

                with mock.patch.object(
                    scaffold_module,
                    "_open_directory_descriptor",
                    side_effect=fail_staging_descriptor,
                ):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                self.assertFalse(target.exists())
                if preserve_competing:
                    competing = project / ".agents" / "skills"
                    competing_staging = next(competing.glob(".verify-tiny-fixture.staging-*"))
                    self.assertEqual(
                        (competing_staging / "sentinel.txt").read_text(
                            encoding="utf-8"
                        ),
                        "preserve\n",
                    )
                    self.assertTrue(staging_detached.is_dir())
                else:
                    self.assertFalse((project / ".agents").exists())

    def test_new_parent_descriptor_failure_preserves_competitor(self) -> None:
        for component in ("agents", "skills"):
            with self.subTest(component=component), tempfile.TemporaryDirectory(
                prefix=f"create-verification-parent-descriptor-{component}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                detached = root / f"detached-{component}"
                injected = False
                attempts = 0
                original_open = scaffold_module._open_directory_descriptor
                failing_label = (
                    "output parent .agents"
                    if component == "agents"
                    else "output parent .agents/skills"
                )

                def fail_parent_descriptor(
                    name: Path | str,
                    label: str,
                    *,
                    dir_fd: int | None = None,
                    collision: bool = False,
                ) -> int:
                    nonlocal attempts, injected
                    if label == failing_label:
                        attempts += 1
                    if label == failing_label and attempts == 2:
                        injected = True
                        entry = project / ".agents"
                        if component == "skills":
                            entry = entry / "skills"
                        entry.rename(detached)
                        entry.mkdir()
                        (entry / "sentinel.txt").write_text("preserve\n", encoding="utf-8")
                        raise ScaffoldError("forced parent descriptor failure")
                    return original_open(name, label, dir_fd=dir_fd, collision=collision)

                with mock.patch.object(
                    scaffold_module,
                    "_open_directory_descriptor",
                    side_effect=fail_parent_descriptor,
                ):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                entry = project / ".agents"
                if component == "skills":
                    entry = entry / "skills"
                self.assertEqual((entry / "sentinel.txt").read_text(encoding="utf-8"), "preserve\n")
                self.assertTrue(detached.is_dir())
                self.assertFalse((entry / "verify-tiny-fixture").exists())

    def test_new_parent_identity_failure_removes_owned_parents(self) -> None:
        for component in ("agents", "skills"):
            with self.subTest(component=component), tempfile.TemporaryDirectory(
                prefix=f"create-verification-parent-identity-{component}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                injected = False
                original_identity = scaffold_module._published_identity

                def fail_parent_identity(
                    parent_fd: int,
                    name: str,
                    *,
                    is_directory: bool,
                    entry_fd: int | None = None,
                ) -> scaffold_module.PublishedEntry:
                    nonlocal injected
                    if name == (".agents" if component == "agents" else "skills") and not injected:
                        injected = True
                        raise ScaffoldError("forced parent identity failure")
                    return original_identity(
                        parent_fd,
                        name,
                        is_directory=is_directory,
                        entry_fd=entry_fd,
                    )

                with mock.patch.object(
                    scaffold_module,
                    "_published_identity",
                    side_effect=fail_parent_identity,
                ):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                self.assertFalse((project / ".agents").exists())

    def test_initial_directory_identity_failure_removes_owned_entry(self) -> None:
        for phase in ("staging", "target"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                prefix=f"create-verification-initial-identity-{phase}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                injected = False
                original_stat = os.stat

                def fail_initial_stat(
                    path: str | os.PathLike[str],
                    *args: object,
                    **kwargs: object,
                ) -> os.stat_result:
                    nonlocal injected
                    value = os.fspath(path)
                    matches = (
                        phase == "target" and value == "verify-tiny-fixture"
                    ) or (
                        phase == "staging" and value.startswith(".verify-tiny-fixture.staging-")
                    )
                    if kwargs.get("dir_fd") is not None and matches and not injected:
                        injected = True
                        raise OSError(errno.EIO, "forced identity capture failure")
                    return original_stat(path, *args, **kwargs)

                with mock.patch.object(scaffold_module.os, "stat", side_effect=fail_initial_stat):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                self.assertFalse((project / ".agents").exists())

    def test_reserved_target_descriptor_failure_cleans_own_reservation_and_preserves_competitor(self) -> None:
        for preserve_competing in (False, True):
            with self.subTest(preserve_competing=preserve_competing), tempfile.TemporaryDirectory(
                prefix="create-verification-target-descriptor-failure-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                target = project / ".agents" / "skills" / "verify-tiny-fixture"
                target_detached = root / "detached-target"
                injected = False
                original_open = scaffold_module._open_directory_descriptor

                def fail_target_descriptor(
                    name: Path | str,
                    label: str,
                    *,
                    dir_fd: int | None = None,
                    collision: bool = False,
                ) -> int:
                    nonlocal injected
                    if label == "reserved target" and not injected:
                        injected = True
                        if preserve_competing:
                            target.rename(target_detached)
                            target.mkdir()
                            (target / "sentinel.txt").write_text("preserve\n", encoding="utf-8")
                        raise ScaffoldError("forced target descriptor failure")
                    return original_open(name, label, dir_fd=dir_fd, collision=collision)

                with mock.patch.object(
                    scaffold_module,
                    "_open_directory_descriptor",
                    side_effect=fail_target_descriptor,
                ):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                if preserve_competing:
                    self.assertEqual(
                        (target / "sentinel.txt").read_text(encoding="utf-8"),
                        "preserve\n",
                    )
                    self.assertFalse((target / "SKILL.md").exists())
                    self.assertTrue(target_detached.is_dir())
                else:
                    self.assertFalse(target.exists())
                    self.assertFalse((project / ".agents").exists())

    def test_new_directory_binding_rejects_a_replacement_after_path_stat(self) -> None:
        for phase in ("staging", "target"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                prefix=f"create-verification-bind-race-{phase}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                detached = root / f"detached-{phase}"
                injected = False
                original_open = scaffold_module._open_directory_descriptor

                def replace_before_open(
                    name: Path | str,
                    label: str,
                    *,
                    dir_fd: int | None = None,
                    collision: bool = False,
                ) -> int:
                    nonlocal injected
                    if not injected and (
                        (phase == "staging" and label == "staging directory")
                        or (phase == "target" and label == "reserved target")
                    ):
                        injected = True
                        live = project / ".agents" / "skills" / os.fspath(name)
                        live.rename(detached)
                        live.mkdir()
                        (live / "competitor.txt").write_text("keep\n", encoding="utf-8")
                    return original_open(name, label, dir_fd=dir_fd, collision=collision)

                with mock.patch.object(
                    scaffold_module,
                    "_open_directory_descriptor",
                    side_effect=replace_before_open,
                ):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                live = project / ".agents" / "skills"
                entry = next(live.glob(".verify-tiny-fixture.staging-*")) if phase == "staging" else live / "verify-tiny-fixture"
                self.assertEqual((entry / "competitor.txt").read_text(encoding="utf-8"), "keep\n")
                self.assertTrue(detached.is_dir())

    def test_source_replacement_after_enumeration_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-source-enumeration-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            detached = root / "detached-source"
            injected = False
            original_publish = scaffold_module._publish_entry_no_replace

            def replace_before_publish(*args: object, **kwargs: object) -> list[scaffold_module.PublishedEntry]:
                nonlocal injected
                source_name = kwargs.get("source_name")
                if source_name == "SKILL.md" and not injected:
                    injected = True
                    staging = next((project / ".agents" / "skills").glob(".verify-tiny-fixture.staging-*"))
                    source = staging / "SKILL.md"
                    source.rename(detached)
                    source.write_text("competitor\n", encoding="utf-8")
                return original_publish(*args, **kwargs)

            with mock.patch.object(
                scaffold_module,
                "_publish_entry_no_replace",
                side_effect=replace_before_publish,
            ):
                with self.assertRaises(ScaffoldError):
                    scaffold_module.scaffold(spec_for(), project)

            self.assertTrue(injected)
            self.assertFalse((project / ".agents" / "skills" / "verify-tiny-fixture").exists())
            self.assertTrue(detached.is_file())

    def test_staging_rollback_preserves_an_unrecorded_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-staging-manifest-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            outside = root / "outside.txt"
            outside.write_text("competitor\n", encoding="utf-8")
            injected = False

            def fail_validation(skill_root: Path, **_: object) -> tuple[str]:
                nonlocal injected
                if not injected:
                    injected = True
                    os.link(outside, skill_root / "unexpected.txt")
                return ("forced validation failure",)

            with mock.patch.object(scaffold_module, "validate_generated_skill", side_effect=fail_validation):
                with self.assertRaises(ScaffoldError):
                    scaffold_module.scaffold(spec_for(), project)

            self.assertTrue(injected)
            staging = next((project / ".agents" / "skills").glob(".verify-tiny-fixture.staging-*"))
            self.assertEqual((staging / "unexpected.txt").read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual(outside.stat().st_nlink, 2)

    def test_success_cleanup_preserves_an_unrecorded_staging_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-success-cleanup-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            original_assert = scaffold_module._assert_reserved_target_identity
            injected = False

            def inject_competitor(
                parent_fd: int,
                target_name: str,
                target: Path,
                target_fd: int,
                identity: tuple[int, int],
            ) -> None:
                nonlocal injected
                original_assert(parent_fd, target_name, target, target_fd, identity)
                if not injected and (target / "SKILL.md").exists():
                    staging = next((project / ".agents" / "skills").glob(".verify-tiny-fixture.staging-*"))
                    (staging / "unexpected.txt").write_text("competitor\n", encoding="utf-8")
                    injected = True

            with mock.patch.object(
                scaffold_module,
                "_assert_reserved_target_identity",
                side_effect=inject_competitor,
            ):
                with self.assertRaises(CollisionError):
                    scaffold_module.scaffold(spec_for(), project)

            self.assertTrue(injected)
            staging = next((project / ".agents" / "skills").glob(".verify-tiny-fixture.staging-*"))
            self.assertEqual((staging / "unexpected.txt").read_text(encoding="utf-8"), "competitor\n")
            self.assertFalse((project / ".agents" / "skills" / "verify-tiny-fixture").exists())

    def test_post_link_identity_failure_rolls_back_the_child(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-post-link-identity-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            copy_fixture(project)
            target = project / ".agents" / "skills" / "verify-tiny-fixture"
            original_identity = scaffold_module._published_identity
            injected = False

            def fail_after_regular_identity(
                parent_fd: int,
                name: str,
                *,
                is_directory: bool,
                entry_fd: int | None = None,
            ) -> scaffold_module.PublishedEntry:
                nonlocal injected
                observed = original_identity(
                    parent_fd,
                    name,
                    is_directory=is_directory,
                    entry_fd=entry_fd,
                )
                if not is_directory and not injected:
                    injected = True
                    scaffold_module._close_published([observed])
                    raise ScaffoldError("forced post-link identity failure")
                return observed

            with mock.patch.object(
                scaffold_module,
                "_published_identity",
                side_effect=fail_after_regular_identity,
            ):
                with self.assertRaises(ScaffoldError):
                    scaffold_module.scaffold(spec_for(), project)

            self.assertTrue(injected)
            self.assertFalse(target.exists())
            self.assertFalse((project / ".agents").exists())

    def test_write_failure_does_not_remove_same_name_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-write-race-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            generated = parent / "generated.txt"
            competitor = root / "competitor.txt"
            parent_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            def race_write(descriptor: int, data: bytes) -> int:
                generated.unlink()
                competitor.write_text("competitor\n", encoding="utf-8")
                os.link(competitor, generated)
                raise OSError(errno.EIO, "forced write failure")

            try:
                with mock.patch.object(scaffold_module.os, "write", side_effect=race_write):
                    with self.assertRaises(OSError):
                        scaffold_module._write_file_at(parent_fd, "generated.txt", "owned\n")
            finally:
                os.close(parent_fd)

            self.assertEqual(generated.read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual(competitor.stat().st_nlink, 2)

    def test_source_swap_after_link_removes_only_private_competitor_link(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-link-race-") as temporary:
            root = Path(temporary)
            source_dir = root / "source"
            destination_dir = root / "destination"
            source_dir.mkdir()
            destination_dir.mkdir()
            source_path = source_dir / "source.txt"
            source_path.write_text("original\n", encoding="utf-8")
            competitor = root / "competitor.txt"
            competitor.write_text("competitor\n", encoding="utf-8")
            original_metadata = os.lstat(source_path)
            source_fd = os.open(
                source_dir,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            destination_fd = os.open(
                destination_dir,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            original_link = scaffold_module.os.link
            injected = False

            def race_link(*args: object, **kwargs: object) -> object:
                nonlocal injected
                if not injected:
                    injected = True
                    source_path.unlink()
                    os.link(competitor, source_path)
                return original_link(*args, **kwargs)

            try:
                with mock.patch.object(scaffold_module.os, "link", side_effect=race_link):
                    with self.assertRaises(CollisionError):
                        scaffold_module._publish_entry_no_replace(
                            source_path,
                            destination_dir / "published.txt",
                            destination_dir_fd=destination_fd,
                            source_dir_fd=source_fd,
                            source_name="source.txt",
                            source_metadata=original_metadata,
                        )
            finally:
                os.close(source_fd)
                os.close(destination_fd)

            self.assertTrue(injected)
            self.assertEqual(source_path.read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual(competitor.stat().st_nlink, 2)
            self.assertEqual(list(destination_dir.iterdir()), [])

    def test_publication_swap_after_identity_observation_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-publish-observation-race-") as temporary:
            root = Path(temporary)
            source_dir = root / "source"
            destination_dir = root / "destination"
            source_dir.mkdir()
            destination_dir.mkdir()
            source_path = source_dir / "source.txt"
            source_path.write_text("original\n", encoding="utf-8")
            competitor = root / "competitor.txt"
            competitor.write_text("competitor\n", encoding="utf-8")
            detached = root / "detached-original.txt"
            source_fd = os.open(source_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            destination_fd = os.open(destination_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            original_identity = scaffold_module._published_identity
            injected = False

            def replace_after_observation(
                parent_fd: int,
                name: str,
                *,
                is_directory: bool,
                entry_fd: int | None = None,
            ) -> scaffold_module.PublishedEntry:
                nonlocal injected
                observed = original_identity(
                    parent_fd,
                    name,
                    is_directory=is_directory,
                    entry_fd=entry_fd,
                )
                if name == "published.txt" and not injected:
                    destination = destination_dir / name
                    destination.rename(detached)
                    os.link(competitor, destination)
                    injected = True
                return observed

            try:
                with mock.patch.object(
                    scaffold_module,
                    "_published_identity",
                    side_effect=replace_after_observation,
                ):
                    with self.assertRaises(CollisionError):
                        scaffold_module._publish_entry_no_replace(
                            source_path,
                            destination_dir / "published.txt",
                            destination_dir_fd=destination_fd,
                            source_dir_fd=source_fd,
                            source_name="source.txt",
                            source_metadata=os.lstat(source_path),
                        )
            finally:
                os.close(source_fd)
                os.close(destination_fd)

            self.assertTrue(injected)
            self.assertEqual((destination_dir / "published.txt").read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual(detached.read_text(encoding="utf-8"), "original\n")

    def test_source_replacement_after_descriptor_binding_never_publishes_competitor(self) -> None:
        for kind in ("hardlink", "symlink", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"create-verification-source-race-{kind}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                outside = root / "competitor.txt"
                outside.write_text("competitor\n", encoding="utf-8")
                detached = root / "detached-source.txt"
                target = project / ".agents" / "skills" / "verify-tiny-fixture"
                original_open = scaffold_module._open_regular_descriptor
                injected = False
                detached_content: str | None = None

                def race_source_open(
                    name: Path | str,
                    label: str,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal detached_content, injected
                    descriptor = original_open(name, label, dir_fd=dir_fd)
                    if label == "staging source file" and not injected:
                        injected = True
                        staging_root = project / ".agents" / "skills"
                        source_path = next(staging_root.glob(".verify-tiny-fixture.staging-*"))
                        source_path = source_path / str(name)
                        detached_content = source_path.read_text(encoding="utf-8")
                        source_path.rename(detached)
                        if kind == "hardlink":
                            os.link(outside, source_path)
                        elif kind == "symlink":
                            os.symlink(outside, source_path)
                        else:
                            os.mkfifo(source_path)
                    return descriptor

                with mock.patch.object(
                    scaffold_module,
                    "_open_regular_descriptor",
                    side_effect=race_source_open,
                ):
                    with self.assertRaises((ScaffoldError, OSError)):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                self.assertFalse(target.exists())
                self.assertEqual(outside.read_text(encoding="utf-8"), "competitor\n")
                self.assertIsNotNone(detached_content)
                self.assertEqual(detached.read_text(encoding="utf-8"), detached_content)

    def test_cleanup_identity_races_preserve_replacement_entries(self) -> None:
        for kind in ("file", "empty-directory", "tree"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"create-verification-cleanup-race-{kind}-"
            ) as temporary:
                root = Path(temporary)
                parent = root / "parent"
                parent.mkdir()
                victim = parent / "victim"
                replacement = parent / "replacement"
                detached = root / "detached"
                if kind == "file":
                    victim.write_text("owned\n", encoding="utf-8")
                    replacement.write_text("competitor\n", encoding="utf-8")
                elif kind == "empty-directory":
                    victim.mkdir()
                    replacement.mkdir()
                    (replacement / "sentinel.txt").write_text("competitor\n", encoding="utf-8")
                else:
                    victim.mkdir()
                    (victim / "owned.txt").write_text("owned\n", encoding="utf-8")
                    replacement.mkdir()
                    (replacement / "sentinel.txt").write_text("competitor\n", encoding="utf-8")

                expected = os.lstat(victim)
                parent_fd = os.open(
                    parent,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                original_atomic = scaffold_module._atomic_rename_noreplace
                injected = False

                def race_quarantine(
                    old_dir_fd: int,
                    old_name: str,
                    new_dir_fd: int,
                    new_name: str,
                ) -> None:
                    nonlocal injected
                    if old_dir_fd == parent_fd and old_name == "victim" and not injected:
                        injected = True
                        victim.rename(detached)
                        replacement.rename(victim)
                    original_atomic(old_dir_fd, old_name, new_dir_fd, new_name)

                try:
                    with mock.patch.object(
                        scaffold_module,
                        "_atomic_rename_noreplace",
                        side_effect=race_quarantine,
                    ):
                        with self.assertRaises((ScaffoldError, OSError)):
                            if kind == "file":
                                scaffold_module._remove_owned_entry(
                                    parent_fd,
                                    "victim",
                                    expected.st_dev,
                                    expected.st_ino,
                                    is_directory=False,
                                )
                            elif kind == "empty-directory":
                                scaffold_module._remove_owned_empty_directory(
                                    parent_fd,
                                    "victim",
                                    expected.st_dev,
                                    expected.st_ino,
                                )
                            else:
                                scaffold_module._remove_tree_at(
                                    parent_fd,
                                    "victim",
                                    expected.st_dev,
                                    expected.st_ino,
                                )
                finally:
                    os.close(parent_fd)

                self.assertTrue(injected)
                self.assertEqual(victim.read_text(encoding="utf-8") if kind == "file" else (victim / "sentinel.txt").read_text(encoding="utf-8"), "competitor\n")
                self.assertTrue(detached.exists())
                if kind == "file":
                    self.assertEqual(detached.read_text(encoding="utf-8"), "owned\n")
                else:
                    self.assertTrue(detached.is_dir())

    def test_quarantine_identity_recheck_preserves_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-quarantine-race-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            victim = parent / "victim"
            detached = root / "detached-owned"
            victim.write_text("owned\n", encoding="utf-8")
            expected = os.lstat(victim)
            parent_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            original_unlink = scaffold_module._native_unlink_at
            original_create = scaffold_module._create_private_directory
            quarantine_directory: Path | None = None
            injected = False

            def capture_quarantine(directory_fd: int) -> tuple[str, int]:
                nonlocal quarantine_directory
                name, quarantine_fd = original_create(directory_fd)
                quarantine_directory = parent / name
                return name, quarantine_fd

            def replace_quarantine_entry(
                directory_fd: int,
                name: str,
                *,
                is_directory: bool,
                expected_identity: tuple[int, int] | None = None,
            ) -> None:
                nonlocal injected
                if expected_identity is not None and not is_directory and not injected:
                    if quarantine_directory is None:
                        raise AssertionError("quarantine directory was not captured")
                    entry = quarantine_directory / name
                    entry.rename(detached)
                    entry.write_text("competitor\n", encoding="utf-8")
                    injected = True
                original_unlink(
                    directory_fd,
                    name,
                    is_directory=is_directory,
                    expected_identity=expected_identity,
                )

            try:
                with mock.patch.object(
                    scaffold_module,
                    "_create_private_directory",
                    side_effect=capture_quarantine,
                ):
                    with mock.patch.object(
                        scaffold_module,
                        "_native_unlink_at",
                        side_effect=replace_quarantine_entry,
                    ):
                        with self.assertRaises((ScaffoldError, OSError)):
                            scaffold_module._remove_owned_entry(
                                parent_fd,
                                "victim",
                                expected.st_dev,
                                expected.st_ino,
                                is_directory=False,
                            )
            finally:
                os.close(parent_fd)

            self.assertTrue(injected)
            self.assertEqual(victim.read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual(detached.read_text(encoding="utf-8"), "owned\n")

    def test_private_quarantine_descriptor_failure_removes_private_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-quarantine-descriptor-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            victim = parent / "victim"
            victim.write_text("owned\n", encoding="utf-8")
            expected = os.lstat(victim)
            parent_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            original_open = scaffold_module._open_directory_descriptor
            injected = False

            def fail_private_descriptor(
                name: Path | str,
                label: str,
                *,
                dir_fd: int | None = None,
                collision: bool = False,
            ) -> int:
                nonlocal injected
                if label == "private cleanup directory" and not injected:
                    injected = True
                    raise ScaffoldError("forced private descriptor failure")
                return original_open(name, label, dir_fd=dir_fd, collision=collision)

            try:
                with mock.patch.object(
                    scaffold_module,
                    "_open_directory_descriptor",
                    side_effect=fail_private_descriptor,
                ):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module._remove_owned_entry(
                            parent_fd,
                            "victim",
                            expected.st_dev,
                            expected.st_ino,
                            is_directory=False,
                        )
            finally:
                os.close(parent_fd)

            self.assertTrue(injected)
            self.assertEqual(victim.read_text(encoding="utf-8"), "owned\n")
            self.assertEqual(list(parent.glob(".verify-dispose-*")), [])

    def test_cleanup_failure_restores_owned_entry_without_unbounded_residue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="create-verification-quarantine-failure-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            victim = parent / "victim"
            victim.write_text("owned\n", encoding="utf-8")
            expected = os.lstat(victim)
            parent_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            original_unlink = scaffold_module._native_unlink_at
            injected = False

            def fail_owned_unlink(
                directory_fd: int,
                name: str,
                *,
                is_directory: bool,
                expected_identity: tuple[int, int] | None = None,
            ) -> None:
                nonlocal injected
                if expected_identity == (expected.st_dev, expected.st_ino) and not is_directory:
                    injected = True
                    raise ScaffoldError("forced cleanup failure")
                original_unlink(
                    directory_fd,
                    name,
                    is_directory=is_directory,
                    expected_identity=expected_identity,
                )

            try:
                with mock.patch.object(
                    scaffold_module,
                    "_native_unlink_at",
                    side_effect=fail_owned_unlink,
                ):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module._remove_owned_entry(
                            parent_fd,
                            "victim",
                            expected.st_dev,
                            expected.st_ino,
                            is_directory=False,
                        )
            finally:
                os.close(parent_fd)

            self.assertTrue(injected)
            self.assertEqual(victim.read_text(encoding="utf-8"), "owned\n")
            self.assertEqual(list(parent.glob(".verify-dispose-*")), [])

    def test_reservation_identity_capture_failure_cleans_only_owned_entry(self) -> None:
        for phase in ("staging", "target"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                prefix=f"create-verification-reservation-capture-{phase}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                original_identity = scaffold_module._published_identity
                injected = False

                def fail_reservation_capture(
                    parent_fd: int,
                    name: str,
                    *,
                    is_directory: bool,
                    entry_fd: int | None = None,
                ) -> scaffold_module.PublishedEntry:
                    nonlocal injected
                    is_target = name == "verify-tiny-fixture"
                    is_staging = name.startswith(".verify-tiny-fixture.staging-")
                    if (phase == "target" and is_target) or (phase == "staging" and is_staging):
                        injected = True
                        raise ScaffoldError("forced reservation identity capture failure")
                    return original_identity(
                        parent_fd,
                        name,
                        is_directory=is_directory,
                        entry_fd=entry_fd,
                    )

                with mock.patch.object(
                    scaffold_module,
                    "_published_identity",
                    side_effect=fail_reservation_capture,
                ):
                    with self.assertRaises(ScaffoldError):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                self.assertFalse((project / ".agents").exists())

    def test_reservation_identity_mismatch_preserves_competitor_and_owned_detached_entry(self) -> None:
        for phase in ("staging", "target"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                prefix=f"create-verification-reservation-race-{phase}-"
            ) as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                copy_fixture(project)
                skills_parent = project / ".agents" / "skills"
                target = skills_parent / "verify-tiny-fixture"
                detached = root / f"detached-{phase}"
                original_identity = scaffold_module._published_identity
                injected = False

                def replace_before_capture(
                    parent_fd: int,
                    name: str,
                    *,
                    is_directory: bool,
                    entry_fd: int | None = None,
                ) -> scaffold_module.PublishedEntry:
                    nonlocal injected
                    is_target = name == "verify-tiny-fixture"
                    is_staging = name.startswith(".verify-tiny-fixture.staging-")
                    if (phase == "target" and is_target) or (phase == "staging" and is_staging):
                        injected = True
                        entry = target if phase == "target" else skills_parent / name
                        entry.rename(detached)
                        entry.mkdir()
                        (entry / "sentinel.txt").write_text("competitor\n", encoding="utf-8")
                    return original_identity(
                        parent_fd,
                        name,
                        is_directory=is_directory,
                        entry_fd=entry_fd,
                    )

                with mock.patch.object(
                    scaffold_module,
                    "_published_identity",
                    side_effect=replace_before_capture,
                ):
                    with self.assertRaises((ScaffoldError, OSError)):
                        scaffold_module.scaffold(spec_for(), project)

                self.assertTrue(injected)
                entry = target if phase == "target" else next(
                    skills_parent.glob(".verify-tiny-fixture.staging-*")
                )
                self.assertEqual((entry / "sentinel.txt").read_text(encoding="utf-8"), "competitor\n")
                self.assertTrue(detached.is_dir())

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

            def race_write(
                current_spec: dict[str, object],
                staging: Path,
                *,
                root_fd: int,
            ) -> None:
                original_write(current_spec, staging, root_fd=root_fd)
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
