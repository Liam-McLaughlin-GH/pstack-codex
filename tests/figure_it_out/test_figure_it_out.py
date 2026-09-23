from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "figure-it-out" / "scripts" / "figure_it_out.py"
SKILL = ROOT / "skills" / "figure-it-out" / "SKILL.md"

spec = importlib.util.spec_from_file_location("figure_it_out", SCRIPT)
assert spec and spec.loader
figure_it_out = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = figure_it_out
spec.loader.exec_module(figure_it_out)


def make_plan(
    *,
    phase_count: int = 2,
    max_attempts_per_phase: int = 2,
    max_checkpoints: int = 8,
    max_decisions: int = 32,
) -> dict:
    phases = []
    for index in range(phase_count):
        phase_id = f"phase-{index + 1}"
        phases.append(
            {
                "id": phase_id,
                "name": f"Phase {index + 1}",
                "hypothesis": f"The phase-{index + 1} approach will advance the predicate",
                "output": f"Output for {phase_id}",
                "verification": {
                    "id": f"check-{index + 1}",
                    "method": f"Run the independent check for {phase_id}",
                },
            }
        )
    return {
        "desired_outcome": "A verified bounded workflow fixture",
        "scope": {
            "included": ["src/", "tests/"],
            "excluded": ["deployment", "unrelated files"],
            "allowed_actions": ["read", "write", "run-tests"],
            "allowed_paths": ["src/", "tests/"],
            "logical_actions": ["run-tests"],
            "out_of_scope_actions": ["deploy", "push", "send-message"],
        },
        "constraints": ["Use local evidence", "Do not broaden the task"],
        "terminal_predicate": {
            "description": "Every phase and every named terminal check passes",
            "checks": ["focused-tests", "artifact-inspection"],
        },
        "limits": {
            "max_attempts_per_phase": max_attempts_per_phase,
            "max_checkpoints": max_checkpoints,
            "max_replans": 2,
            "max_decisions": max_decisions,
        },
        "phases": phases,
    }


class WorkflowBehaviorTests(unittest.TestCase):
    def workflow(
        self,
        root: Path,
        *,
        phase_count: int = 2,
        max_attempts_per_phase: int = 2,
        max_checkpoints: int = 8,
        max_decisions: int = 32,
    ):
        return figure_it_out.Workflow.create(
            root / "run.json",
            make_plan(
                phase_count=phase_count,
                max_attempts_per_phase=max_attempts_per_phase,
                max_checkpoints=max_checkpoints,
                max_decisions=max_decisions,
            ),
            workspace_root=root,
        )

    def completed_workflow(self, root: Path):
        workflow = self.workflow(root, phase_count=1)
        workflow.start_phase()
        workflow.complete_phase(
            "completed output",
            {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
        )
        workflow.record_predicate("focused-tests", True, ["tests/focused.txt"])
        workflow.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
        workflow.finish()
        return workflow

    def test_normal_completion_requires_outputs_and_terminal_predicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root)

            self.assertEqual(workflow.state["status"], "planned")
            self.assertEqual(workflow.state["next_action"], "start phase phase-1")
            workflow.guard_action("write", "src/change.py")
            workflow.start_phase()
            workflow.checkpoint("baseline captured", ["artifacts/baseline.txt"], "implement phase-1")
            workflow.complete_phase(
                {"baseline": "captured"},
                {"check_id": "check-1", "passed": True, "evidence": ["tests/baseline-pass.txt"]},
            )
            self.assertEqual(workflow.state["current_phase"], "phase-2")
            self.assertEqual(workflow.state["phases"][1]["status"], "pending")

            workflow.start_phase()
            workflow.complete_phase(
                "implementation output",
                {"check_id": "check-2", "passed": True, "evidence": ["tests/phase-2-pass.txt"]},
            )
            with self.assertRaises(figure_it_out.PredicateError):
                workflow.finish()
            workflow.record_predicate("focused-tests", True, ["tests/focused.txt"])
            workflow.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
            completed = workflow.finish()

            self.assertEqual(completed["status"], "completed")
            self.assertIsNone(completed["next_action"])
            self.assertEqual([phase["status"] for phase in completed["phases"]], ["passed", "passed"])
            audit_path = workflow.export_audit(root / "audit.tsv")
            audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(audit_lines[0], "ts\tphase\tdecision\twhy\tevidence\tresult")
            self.assertGreater(len(audit_lines), 1)
            self.assertTrue(any("phase-1" in line and "verified" in line for line in audit_lines[1:]))
            self.assertTrue(any("checkpoint: baseline captured" in line for line in audit_lines[1:]))
            self.assertTrue(any("artifacts/baseline.txt" in line for line in audit_lines[1:]))

    def test_failed_phase_requires_replan_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            workflow.start_phase()
            failed = workflow.fail_phase(
                "baseline check disagreed",
                ["tests/baseline-fail.txt"],
                "recheck the fixture boundary",
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["phases"][0]["status"], "failed")
            with self.assertRaises(figure_it_out.ReplanRequired):
                workflow.start_phase()

            workflow.replan(
                "Use the fixture boundary as the first check",
                ["tests/baseline-fail.txt"],
                "start the replacement phase",
                {
                    "id": "phase-1",
                    "name": "Replanned phase",
                    "hypothesis": "The fixture boundary will make the check deterministic",
                    "output": "A fixture-scoped output",
                    "verification": {"id": "check-1-replanned", "method": "Run the replacement check"},
                },
            )
            self.assertEqual(workflow.state["status"], "planned")
            self.assertEqual(workflow.state["next_action"], "start replanned phase phase-1")
            workflow.start_phase()
            workflow.complete_phase(
                "replacement output",
                {"check_id": "check-1-replanned", "passed": True, "evidence": ["tests/replanned-pass.txt"]},
            )
            self.assertEqual(workflow.state["phases"][0]["status"], "passed")
            self.assertEqual(len(workflow.state["replans"]), 1)

    def test_interrupted_run_resumes_from_persisted_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            workflow.start_phase()
            workflow.checkpoint("safe stopping point", ["artifacts/checkpoint.txt"], "resume phase-1")
            workflow.interrupt("session ended after the bounded checkpoint")

            resumed = figure_it_out.Workflow.load(root / "run.json", workspace_root=root)
            self.assertEqual(resumed.state["status"], "interrupted")
            self.assertEqual(resumed.state["phases"][0]["status"], "interrupted")
            self.assertEqual(resumed.state["checkpoints"][0]["id"], "cp-1")
            resumed.resume()
            resumed.complete_phase(
                "resumed output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/resumed-pass.txt"]},
            )
            resumed.record_predicate("focused-tests", True, ["tests/focused.txt"])
            resumed.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
            resumed.finish()

            final_state = json.loads((root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(final_state["status"], "completed")
            self.assertEqual(final_state["resume_count"], 1)
            self.assertTrue(any(event["event"] == "interrupted" for event in final_state["events"]))
            self.assertTrue(any(event["event"] == "resumed" for event in final_state["events"]))

    def test_blocked_state_keeps_evidence_and_next_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            blocked = workflow.block(
                "required fixture is not available",
                ["fixtures/missing.json"],
                "obtain the fixture or record an owner decision",
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["blocker"]["evidence"], ["fixtures/missing.json"])
            self.assertEqual(blocked["next_action"], "obtain the fixture or record an owner decision")
            self.assertEqual(blocked["decisions"][-1]["result"], "blocked")

    def test_checkpoint_budget_blocks_instead_of_growing_without_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1, max_checkpoints=1)
            workflow.start_phase()
            workflow.checkpoint("one bounded checkpoint", ["artifacts/one.txt"], "continue phase-1")
            with self.assertRaises(figure_it_out.StateError):
                workflow.checkpoint("second checkpoint", ["artifacts/two.txt"], "continue phase-1")
            self.assertEqual(workflow.state["status"], "blocked")
            self.assertIn("checkpoint limit", workflow.state["blocker"]["reason"])
            self.assertTrue(workflow.state["blocker"]["next_action"])
            self.assertEqual(workflow.state["decisions"][-1]["status"], "blocked")
            audit = workflow.export_audit(root / "checkpoint-audit.tsv").read_text(encoding="utf-8")
            self.assertIn("checkpoint limit reached", audit)

    def test_attempt_budget_persists_a_valid_blocked_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1, max_attempts_per_phase=1)
            workflow.start_phase()
            workflow.fail_phase("check disagreed", ["tests/failure.txt"], "replan the phase")
            workflow.replan("change the bounded approach", ["tests/replan.txt"], "retry phase-1")

            with self.assertRaises(figure_it_out.StateError):
                workflow.start_phase()

            persisted = figure_it_out.Workflow.load(root / "run.json", workspace_root=root)
            self.assertEqual(persisted.state["status"], "blocked")
            self.assertEqual(persisted.state["phases"][0]["status"], "blocked")
            self.assertEqual(persisted.state["events"][-1]["event"], "blocked")
            self.assertEqual(
                persisted.state["blocker"]["evidence"],
                [
                    {
                        "kind": "reference",
                        "ref": "phase:phase-1",
                        "detail": "attempts exhausted at 1",
                    }
                ],
            )

    def test_out_of_scope_action_is_refused_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            with self.assertRaises(figure_it_out.ScopeRefusal) as context:
                workflow.guard_action("deploy", "production")
            refusal = context.exception.refusal
            self.assertEqual(refusal["status"], "refused")
            self.assertIn("next_action", refusal)
            self.assertEqual(workflow.state["decisions"][-1]["result"], "blocked")
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("write", "../outside.py")
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("write", r"src\..\outside.py")
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("write", "src/ok.py\nforged")
            self.assertEqual(workflow.state["decisions"][-1]["status"], "refused")
            self.assertIn("reason", workflow.state["decisions"][-1])
            self.assertIn("next_action", workflow.state["decisions"][-1])

            with self.assertRaises(figure_it_out.ScopeRefusal) as invalid_action:
                workflow.guard_action("bad action", "src/file.py")
            self.assertEqual(invalid_action.exception.refusal["status"], "refused")
            self.assertTrue(invalid_action.exception.refusal["reason"])
            self.assertTrue(invalid_action.exception.refusal["next_action"])
            self.assertEqual(workflow.state["decisions"][-1]["status"], "refused")

            unsafe_targets = [
                "ftp://host/repo",
                "file:///tmp/out",
                "mailto:user@example.com",
                "git+ssh://host/repo",
                "foo:bar",
                r"\\server\share",
                r"src\..\outside.py",
                "src/ok.py\tforged",
                "src/ok.py\vforged",
                "src/ok.py\u200bforged",
                "C:relative.txt",
                "C:/absolute.txt",
                "//server/share",
                "token@host",
            ]
            for target in unsafe_targets:
                with self.assertRaises(figure_it_out.ScopeRefusal, msg=target) as refusal_context:
                    workflow.guard_action("write", target)
                self.assertEqual(refusal_context.exception.refusal["status"], "refused")
                self.assertTrue(refusal_context.exception.refusal["reason"])
                self.assertTrue(refusal_context.exception.refusal["next_action"])

    def test_guard_action_refuses_stopped_blocked_failed_and_completed_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stopped_status in ("interrupted", "blocked", "failed", "completed"):
                with self.subTest(status=stopped_status):
                    run_root = root / stopped_status
                    workflow = self.workflow(run_root, phase_count=1)
                    if stopped_status == "interrupted":
                        workflow.start_phase()
                        workflow.checkpoint("safe stop", ["artifacts/checkpoint.txt"], "resume phase-1")
                        workflow.interrupt("session stopped")
                    elif stopped_status == "blocked":
                        workflow.block("fixture unavailable", ["fixtures/missing.json"], "obtain the fixture")
                    elif stopped_status == "failed":
                        workflow.start_phase()
                        workflow.fail_phase("check disagreed", ["tests/failure.txt"], "replan the phase")
                    else:
                        workflow.start_phase()
                        workflow.complete_phase(
                            "completed output",
                            {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
                        )
                        workflow.record_predicate("focused-tests", True, ["tests/focused.txt"])
                        workflow.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
                        workflow.finish()

                    with self.assertRaises(figure_it_out.ScopeRefusal) as context:
                        workflow.guard_action("write", "src/after-stop.py")

                    self.assertEqual(context.exception.refusal["status"], "refused")
                    persisted = figure_it_out.Workflow.load(run_root / "run.json", workspace_root=run_root)
                    self.assertEqual(persisted.state["status"], stopped_status)
                    decision = persisted.state["decisions"][-1]
                    self.assertEqual(decision["status"], "refused")
                    self.assertEqual(decision["result"], "blocked")
                    self.assertTrue(decision["reason"])
                    self.assertTrue(decision["next_action"])
                    self.assertTrue(decision["evidence"])
                    self.assertTrue(
                        any(
                            event["event"] == "action_refused" and event["target"] == "src/after-stop.py"
                            for event in persisted.state["events"]
                        )
                    )
                    self.assertFalse(
                        any(
                            event["event"] == "action_allowed" and event.get("target") == "src/after-stop.py"
                            for event in persisted.state["events"]
                        )
                    )

    def test_stopped_refusal_sanitizes_unsafe_targets_before_persistence(self):
        unsafe_targets = ["token@host", "https://example.test/result", "src/../outside.py"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            workflow.block("fixture unavailable", ["fixtures/missing.json"], "obtain the fixture")

            for target in unsafe_targets:
                with self.assertRaises(figure_it_out.ScopeRefusal) as context:
                    workflow.guard_action("write", target)
                self.assertEqual(context.exception.refusal["target"], "<invalid-target>")

            persisted = figure_it_out.Workflow.load(root / "run.json", workspace_root=root)
            refusal_events = [event for event in persisted.state["events"] if event["event"] == "action_refused"]
            self.assertEqual([event["target"] for event in refusal_events], ["<invalid-target>"] * len(unsafe_targets))
            self.assertTrue(all("@" not in json.dumps(event) for event in refusal_events))

    def test_invalid_action_refusal_does_not_persist_the_raw_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            with self.assertRaises(figure_it_out.ScopeRefusal) as refusal:
                workflow.guard_action("deploy token@host", "token@host")
            self.assertNotIn("token@host", json.dumps(refusal.exception.refusal))
            self.assertNotIn("deploy token@host", json.dumps(workflow.state))
            event = next(event for event in workflow.state["events"] if event["event"] == "action_refused")
            self.assertEqual(event["action"], "invalid-action")

    def test_completed_run_cannot_mutate_terminal_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            workflow.start_phase()
            workflow.complete_phase(
                "completed output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
            )
            workflow.record_predicate("focused-tests", True, ["tests/focused.txt"])
            workflow.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
            workflow.finish()
            terminal_before = deepcopy(workflow.state["terminal"])
            predicate_results_before = deepcopy(workflow.state["predicate_results"])
            decisions_before = deepcopy(workflow.state["decisions"])

            with self.assertRaises(figure_it_out.PredicateError):
                workflow.record_predicate("focused-tests", False, ["tests/failed-after-completion.txt"], status="failed")
            with self.assertRaises(figure_it_out.StateError):
                workflow.record_decision("run", "contradict completion", "should be refused", ["tests/late.txt"], "failed")

            self.assertEqual(workflow.state["terminal"], terminal_before)
            self.assertEqual(workflow.state["predicate_results"], predicate_results_before)
            self.assertEqual(workflow.state["decisions"], decisions_before)
            persisted = figure_it_out.Workflow.load(root / "run.json", workspace_root=root)
            self.assertEqual(persisted.state["terminal"], terminal_before)
            self.assertEqual(persisted.state["predicate_results"], predicate_results_before)
            self.assertEqual(persisted.state["decisions"], decisions_before)

    def test_reopened_completed_run_is_rejected_on_load_and_before_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persisted = self.completed_workflow(root / "persisted")
            state_path = root / "persisted" / "run.json"
            forged = json.loads(state_path.read_text(encoding="utf-8"))
            forged["status"] = "running"
            forged["terminal"] = None
            forged["next_action"] = "finish"
            for result in forged["predicate_results"].values():
                result["evidence"] = ["tests/forged-late-evidence.txt"]
            state_path.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(state_path, workspace_root=root / "persisted")

            in_memory = self.completed_workflow(root / "in-memory")
            in_memory.state["status"] = "running"
            in_memory.state["terminal"] = None
            in_memory.state["next_action"] = "finish"
            with self.assertRaises(figure_it_out.StateError):
                in_memory.finish()

    def test_persisted_lifecycle_rejects_terminal_cross_field_edits(self):
        mutations = {
            "completed_without_event": lambda state: state["events"].pop(),
            "completed_without_terminal": lambda state: state.update(terminal=None),
            "terminal_snapshot_mismatch": lambda state: next(
                event for event in state["events"] if event["event"] == "completed"
            )["terminal_snapshot"].update(predicate="forged predicate"),
            "terminal_checks_mismatch": lambda state: state["terminal"]["checks"]["focused-tests"].update(
                evidence=["tests/forged-terminal.txt"]
            ),
            "predicate_and_terminal_evidence_forged": lambda state: (
                state["predicate_results"]["focused-tests"].update(evidence=["tests/forged-late.txt"]),
                state["terminal"]["checks"]["focused-tests"].update(evidence=["tests/forged-late.txt"]),
            ),
            "predicate_event_removed": lambda state: state["events"].__setitem__(
                slice(None),
                [event for event in state["events"] if not (event["event"] == "predicate_recorded" and event["check_id"] == "focused-tests")],
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, mutate in mutations.items():
                with self.subTest(case=label):
                    workflow = self.completed_workflow(root / label)
                    state_path = root / label / "run.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    mutate(state)
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(figure_it_out.StateError):
                        figure_it_out.Workflow.load(state_path, workspace_root=root / label)

            ready = self.workflow(root / "ready", phase_count=1)
            ready.start_phase()
            ready.complete_phase(
                "ready output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
            )
            ready.record_predicate("focused-tests", True, ["tests/focused.txt"])
            ready.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
            ready.state["next_action"] = "finish"
            (root / "ready" / "run.json").write_text(json.dumps(ready.state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(root / "ready" / "run.json", workspace_root=root / "ready")

    def test_malformed_persisted_state_is_rejected_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            state_path = root / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))

            missing_status = deepcopy(state)
            del missing_status["status"]
            state_path.write_text(json.dumps(missing_status), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError) as missing_error:
                figure_it_out.Workflow.load(state_path, workspace_root=root)
            self.assertIn("missing required fields", str(missing_error.exception))
            self.assertNotIn("KeyError", str(missing_error.exception))

            invalid_run_status = deepcopy(state)
            invalid_run_status["status"] = {"unexpected": "shape"}
            state_path.write_text(json.dumps(invalid_run_status), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError) as run_status_error:
                figure_it_out.Workflow.load(state_path, workspace_root=root)
            self.assertIn("status must be one of", str(run_status_error.exception))

            invalid_phase_status = deepcopy(state)
            invalid_phase_status["phases"][0]["status"] = "finished"
            state_path.write_text(json.dumps(invalid_phase_status), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError) as phase_error:
                figure_it_out.Workflow.load(state_path, workspace_root=root)
            self.assertIn("status is not supported", str(phase_error.exception))

            valid_workflow = self.workflow(root / "in-memory", phase_count=1)
            valid_workflow.state.pop("status")
            with self.assertRaises(figure_it_out.StateError) as start_error:
                valid_workflow.start_phase()
            self.assertIn("missing required fields", str(start_error.exception))

    def test_persisted_state_rejects_incomplete_phase_predicate_and_checkpoint_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            workflow.start_phase()
            workflow.checkpoint("checkpoint", ["artifacts/checkpoint.txt"], "complete phase-1")
            workflow.complete_phase(
                "completed output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
            )
            workflow.record_predicate("focused-tests", True, ["tests/focused.txt"])
            workflow.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
            state_path = root / "run.json"
            valid_state = json.loads(state_path.read_text(encoding="utf-8"))

            malformed_cases = {
                "passed phase without output": lambda state: state["phases"][0].update(output_value=None),
                "passed phase without verification": lambda state: state["phases"][0].update(verification_result=None),
                "phase verification with empty evidence": lambda state: state["phases"][0]["verification_result"].update(evidence=[]),
                "verified predicate with empty evidence": lambda state: state["predicate_results"]["focused-tests"].update(evidence=[]),
                "verified predicate with placeholder evidence": lambda state: state["predicate_results"]["focused-tests"].update(evidence=["checked"]),
                "checkpoint with empty evidence": lambda state: state["checkpoints"][0].update(evidence=[]),
                "phase with incomplete checkpoint reference": lambda state: state["phases"][0].update(checkpoints=["cp-missing"]),
            }
            for label, mutate in malformed_cases.items():
                with self.subTest(case=label):
                    malformed = deepcopy(valid_state)
                    mutate(malformed)
                    state_path.write_text(json.dumps(malformed), encoding="utf-8")
                    with self.assertRaises(figure_it_out.StateError):
                        figure_it_out.Workflow.load(state_path, workspace_root=root)

            workflow.state["checkpoints"][0]["evidence"] = []
            with self.assertRaises(figure_it_out.StateError):
                workflow.finish()

    def test_cli_audit_rejects_existing_non_regular_destinations_without_traceback_or_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            state_path = root / "run.json"
            destinations = {
                "directory": root / "audit-directory",
                "fifo": root / "audit-fifo",
            }
            destinations["directory"].mkdir()
            (destinations["directory"] / "marker.txt").write_text("preserve", encoding="utf-8")
            os.mkfifo(destinations["fifo"])

            for kind, destination in destinations.items():
                with self.subTest(destination=kind):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT),
                            "audit",
                            "--state",
                            str(state_path),
                            "--workspace-root",
                            str(root),
                            "--output",
                            str(destination),
                        ],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    error = json.loads(result.stderr)
                    self.assertEqual(error["status"], "refused")
                    self.assertEqual(error["refusal"]["status"], "refused")
                    self.assertTrue(error["refusal"]["next_action"])
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(result.stdout, "")

            self.assertTrue((destinations["directory"] / "marker.txt").is_file())
            self.assertTrue(stat.S_ISDIR(destinations["directory"].stat().st_mode))
            self.assertTrue(stat.S_ISFIFO(destinations["fifo"].stat().st_mode))

    def test_cli_replan_rejects_invalid_json_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            workflow.start_phase()
            workflow.fail_phase("check disagreed", ["tests/failure.txt"], "replan the phase")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "replan",
                    "--state",
                    str(root / "run.json"),
                    "--workspace-root",
                    str(root),
                    "--reason",
                    "change the approach",
                    "--evidence",
                    "tests/failure.txt",
                    "--next-action",
                    "start the replacement",
                    "--replacement",
                    "{not valid json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stderr)
            self.assertEqual(error["status"], "error")
            self.assertIn("valid JSON", error["error"])
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(
                figure_it_out.Workflow.load(root / "run.json", workspace_root=root).state["status"],
                "failed",
            )

    def test_targetless_logical_actions_are_explicit_in_path_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            self.assertEqual(workflow.guard_action("run-tests")["status"], "allowed")
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("write")

    def test_optional_scope_fields_are_normalized_or_rejected_before_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            scope = workflow.state["plan"]["scope"]
            scope.pop("logical_actions")
            scope.pop("out_of_scope_actions")
            scope.pop("allowed_paths")
            self.assertEqual(workflow.guard_action("write", "src/file.py")["status"], "allowed")

            malformed = self.workflow(root / "malformed", phase_count=1)
            malformed.state["plan"]["scope"]["out_of_scope_actions"] = {}
            with self.assertRaises(figure_it_out.StateError):
                malformed.guard_action("read", "src/file.py")

            malformed.state["plan"]["scope"]["out_of_scope_actions"] = []
            malformed.state["plan"]["scope"]["logical_actions"] = {"run-tests": True}
            with self.assertRaises(figure_it_out.StateError):
                malformed.guard_action("read", "src/file.py")

    def test_evidence_rejects_placeholders_and_terminal_nonpassing_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            workflow.start_phase()
            with self.assertRaises(figure_it_out.PlanError):
                workflow.complete_phase(
                    "output",
                    {"check_id": "check-1", "passed": True, "evidence": ["checked"]},
                )
            with self.assertRaises(figure_it_out.PlanError):
                workflow.complete_phase(
                    "output",
                    {"check_id": "check-1", "passed": True, "evidence": ["agent report"]},
                )
            workflow.complete_phase(
                "output",
                {
                    "check_id": "check-1",
                    "passed": True,
                    "evidence": [{"kind": "test", "ref": "tests/phase-1.txt", "line": 1, "result": "passed"}],
                },
            )
            workflow.record_predicate(
                "focused-tests",
                False,
                ["tests/not-run.txt"],
                observed="NOT RUN: test command was unavailable",
                status="not_run",
            )
            workflow.record_predicate(
                "artifact-inspection",
                False,
                ["artifacts/inconclusive.txt"],
                observed="INCONCLUSIVE: artifact was incomplete",
                status="inconclusive",
            )
            self.assertEqual(workflow.state["predicate_results"]["focused-tests"]["status"], "not_run")
            self.assertEqual(workflow.state["predicate_results"]["artifact-inspection"]["status"], "inconclusive")
            with self.assertRaises(figure_it_out.PredicateError):
                workflow.finish()
            with self.assertRaises(figure_it_out.PredicateError):
                workflow.record_predicate(
                    "focused-tests",
                    True,
                    ["tests/focused.txt"],
                    status="not_run",
                )

    def test_decision_limit_blocks_and_persists_auditable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1, max_decisions=1)
            workflow.record_decision("phase-1", "first decision", "bounded test", ["tests/decision.txt"], "open")
            with self.assertRaises(figure_it_out.StateError):
                workflow.record_decision("phase-1", "second decision", "budget check", ["tests/decision-2.txt"], "open")
            persisted = figure_it_out.Workflow.load(root / "run.json", workspace_root=root)
            self.assertEqual(persisted.state["status"], "blocked")
            self.assertIn("decision limit", persisted.state["blocker"]["reason"])
            self.assertEqual(persisted.state["decisions"][-1]["status"], "blocked")
            audit = persisted.export_audit(root / "decision-audit.tsv").read_text(encoding="utf-8")
            self.assertIn("decision limit reached", audit)

    def test_replan_revalidates_all_phase_verification_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root)
            workflow.start_phase()
            workflow.fail_phase("phase-1 failed", ["tests/phase-1-fail.txt"], "replace phase-1")
            with self.assertRaises(figure_it_out.PlanError):
                workflow.replan(
                    "reuse would collide",
                    ["tests/replan.txt"],
                    "start phase-1",
                    {
                        "id": "phase-1",
                        "name": "Replacement",
                        "hypothesis": "The replacement is bounded",
                        "output": "Replacement output",
                        "verification": {"id": "check-2", "method": "Reuse phase-2 check"},
                    },
                )
            self.assertEqual(workflow.state["status"], "failed")
            self.assertEqual(
                len({phase["verification"]["id"] for phase in workflow.state["plan"]["phases"]}),
                2,
            )

    def test_replan_without_replacement_is_atomic_and_reloadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root / "reloadable", phase_count=1)
            workflow.start_phase()
            workflow.fail_phase("phase check disagreed", ["tests/failure.txt"], "try the bounded approach again")

            replanned = workflow.replan(
                "the first check used the wrong fixture boundary",
                ["tests/failure.txt"],
                "start phase-1 again",
            )
            self.assertEqual(
                replanned["replans"][0]["replacement"],
                replanned["plan"]["phases"][0],
            )
            reloaded = figure_it_out.Workflow.load(root / "reloadable" / "run.json", workspace_root=root / "reloadable")
            self.assertEqual(reloaded.state["status"], "planned")
            self.assertEqual(reloaded.state["replans"][0]["replacement"], replanned["replans"][0]["replacement"])

            atomic = self.workflow(root / "atomic", phase_count=1)
            atomic.start_phase()
            atomic.fail_phase("phase check disagreed", ["tests/failure.txt"], "try again")
            before_state = deepcopy(atomic.state)
            before_bytes = (root / "atomic" / "run.json").read_bytes()

            def fail_persist():
                raise OSError("simulated persist failure")

            atomic._persist = fail_persist
            with self.assertRaises(OSError):
                atomic.replan("change the approach", ["tests/failure.txt"], "start again")
            self.assertEqual(atomic.state, before_state)
            self.assertEqual((root / "atomic" / "run.json").read_bytes(), before_bytes)

    def test_evidence_requires_concrete_local_references_for_bare_and_structured_values(self):
        invalid_references = [
            "not-run",
            "not_run",
            "https://example.test/result",
            "file:///tmp/result",
            "foo:bar",
            "tests/test_workflow.py:not-a-line",
            "check:../outside",
            "check:",
        ]
        for reference in invalid_references:
            with self.subTest(kind="bare", reference=reference):
                with self.assertRaises(figure_it_out.PlanError):
                    figure_it_out._evidence([reference], "evidence")
            with self.subTest(kind="structured", reference=reference):
                with self.assertRaises(figure_it_out.PlanError):
                    figure_it_out._evidence(
                        [{"kind": "reference", "ref": reference}],
                        "evidence",
                    )

        self.assertEqual(figure_it_out._evidence(["check:terminal-predicate"]), ["check:terminal-predicate"])
        self.assertEqual(
            figure_it_out._evidence(
                [{"kind": "test", "ref": "tests/test_workflow.py:42"}],
            ),
            [{"kind": "test", "ref": "tests/test_workflow.py:42"}],
        )

    def test_persisted_lifecycle_rejects_zero_attempts_and_forged_terminal_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed_phase = self.workflow(root / "zero-attempt", phase_count=1)
            completed_phase.start_phase()
            completed_phase.complete_phase(
                "phase output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
            )
            zero_attempt = json.loads((root / "zero-attempt" / "run.json").read_text(encoding="utf-8"))
            zero_attempt["phases"][0]["attempt"] = 0
            (root / "zero-attempt" / "run.json").write_text(json.dumps(zero_attempt), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(root / "zero-attempt" / "run.json", workspace_root=root / "zero-attempt")

            forged = self.workflow(root / "forged-terminal", phase_count=1)
            forged.start_phase()
            forged.complete_phase(
                "phase output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
            )
            forged_state = json.loads((root / "forged-terminal" / "run.json").read_text(encoding="utf-8"))
            for check_id in forged_state["predicate_results"]:
                forged_state["predicate_results"][check_id] = {
                    "check_id": check_id,
                    "passed": True,
                    "status": "verified",
                    "evidence": ["tests/forged-terminal.txt"],
                    "observed": "passed",
                    "at": "2026-09-23T00:00:00+00:00",
                }
            forged_state["next_action"] = "finish when all terminal checks pass"
            (root / "forged-terminal" / "run.json").write_text(json.dumps(forged_state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(root / "forged-terminal" / "run.json", workspace_root=root / "forged-terminal")

    def test_persisted_lifecycle_rejects_deleted_event_snapshots(self):
        def phase_passed(workflow):
            workflow.start_phase()
            workflow.complete_phase(
                "phase output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
            )

        def checkpointed(workflow):
            workflow.start_phase()
            workflow.checkpoint("safe point", ["artifacts/checkpoint.txt"], "resume phase-1")

        def failed(workflow):
            workflow.start_phase()
            workflow.fail_phase("check disagreed", ["tests/failure.txt"], "replan the phase")

        def replanned(workflow):
            failed(workflow)
            workflow.replan("change the approach", ["tests/replan.txt"], "start phase-1 again")

        def interrupted(workflow):
            checkpointed(workflow)
            workflow.interrupt("session stopped")

        def blocked(workflow):
            workflow.block("fixture unavailable", ["fixtures/missing.json"], "obtain the fixture")

        def predicate_recorded(workflow):
            phase_passed(workflow)
            workflow.record_predicate("focused-tests", True, ["tests/focused.txt"])

        def completed(workflow):
            phase_passed(workflow)
            workflow.record_predicate("focused-tests", True, ["tests/focused.txt"])
            workflow.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
            workflow.finish()

        cases = [
            ("phase-output", phase_passed, "phase_passed", "output_snapshot"),
            ("phase-verification", phase_passed, "phase_passed", "verification_snapshot"),
            ("checkpoint", checkpointed, "checkpoint", "checkpoint_snapshot"),
            ("failure", failed, "phase_failed", "failure_snapshot"),
            ("replan", replanned, "replanned", "replan_snapshot"),
            ("interruption", interrupted, "interrupted", "interruption_snapshot"),
            ("blocker", blocked, "blocked", "blocker_snapshot"),
            ("predicate", predicate_recorded, "predicate_recorded", "result_snapshot"),
            ("terminal", completed, "completed", "terminal_snapshot"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, setup, event_name, snapshot_field in cases:
                with self.subTest(snapshot=snapshot_field):
                    run_root = root / name
                    workflow = self.workflow(run_root, phase_count=1)
                    setup(workflow)
                    state_path = run_root / "run.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    event = next(item for item in state["events"] if item["event"] == event_name)
                    del event[snapshot_field]
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(figure_it_out.StateError):
                        figure_it_out.Workflow.load(state_path, workspace_root=run_root)

    def test_persisted_lifecycle_rejects_forged_phase_output_checkpoint_and_replan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            output_workflow = self.workflow(root / "output", phase_count=1)
            output_workflow.start_phase()
            output_workflow.complete_phase(
                "real output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/phase-pass.txt"]},
            )
            output_state_path = root / "output" / "run.json"
            output_state = json.loads(output_state_path.read_text(encoding="utf-8"))
            output_state["phases"][0]["output_value"] = "forged output"
            output_state_path.write_text(json.dumps(output_state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(output_state_path, workspace_root=root / "output")

            checkpoint_workflow = self.workflow(root / "checkpoint", phase_count=1)
            checkpoint_workflow.start_phase()
            checkpoint_workflow.checkpoint("real checkpoint", ["artifacts/checkpoint.txt"], "continue phase-1")
            checkpoint_state_path = root / "checkpoint" / "run.json"
            checkpoint_state = json.loads(checkpoint_state_path.read_text(encoding="utf-8"))
            checkpoint_state["checkpoints"][0]["label"] = "forged checkpoint"
            checkpoint_state_path.write_text(json.dumps(checkpoint_state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(checkpoint_state_path, workspace_root=root / "checkpoint")

            replan_workflow = self.workflow(root / "replan", phase_count=1)
            replan_workflow.start_phase()
            replan_workflow.fail_phase("check disagreed", ["tests/failure.txt"], "replan the phase")
            replan_workflow.replan("real replan", ["tests/replan.txt"], "start phase-1 again")
            replan_state_path = root / "replan" / "run.json"
            replan_state = json.loads(replan_state_path.read_text(encoding="utf-8"))
            replan_state["replans"][0]["reason"] = "forged replan"
            replan_state_path.write_text(json.dumps(replan_state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(replan_state_path, workspace_root=root / "replan")

    def test_refusal_persistence_restores_live_state_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            before_state = deepcopy(workflow.state)
            before_bytes = (root / "run.json").read_bytes()

            with patch.object(figure_it_out.os, "replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(OSError):
                    workflow.guard_action("deploy", "production")

            self.assertEqual(workflow.state, before_state)
            self.assertEqual((root / "run.json").read_bytes(), before_bytes)

    def test_start_and_finish_restore_live_state_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            starting = self.workflow(root / "start", phase_count=1)
            before_state = deepcopy(starting.state)
            before_bytes = (root / "start" / "run.json").read_bytes()
            with patch.object(figure_it_out.os, "replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(OSError):
                    starting.start_phase()
            self.assertEqual(starting.state, before_state)
            self.assertEqual((root / "start" / "run.json").read_bytes(), before_bytes)

            finishing = self.workflow(root / "finish", phase_count=1)
            finishing.start_phase()
            finishing.complete_phase(
                "output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/pass.txt"]},
            )
            finishing.record_predicate("focused-tests", True, ["tests/focused.txt"])
            finishing.record_predicate("artifact-inspection", True, ["tests/artifact.txt"])
            before_state = deepcopy(finishing.state)
            before_bytes = (root / "finish" / "run.json").read_bytes()
            with patch.object(figure_it_out.os, "replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(OSError):
                    finishing.finish()
            self.assertEqual(finishing.state, before_state)
            self.assertEqual((root / "finish" / "run.json").read_bytes(), before_bytes)

    def test_persisted_graph_requires_exact_reverse_lifecycle_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            cases = {
                "orphan-checkpoint": (
                    lambda workflow: (workflow.start_phase(), workflow.checkpoint("safe point", ["artifacts/cp.txt"], "continue")),
                    lambda state: state["checkpoints"].append({**deepcopy(state["checkpoints"][0]), "id": "cp-orphan", "event_sequence": 99}),
                ),
                "missing-checkpoint-reverse-reference": (
                    lambda workflow: (workflow.start_phase(), workflow.checkpoint("safe point", ["artifacts/cp.txt"], "continue")),
                    lambda state: state["phases"][0].update(checkpoints=[]),
                ),
                "missing-checkpoint-event": (
                    lambda workflow: (workflow.start_phase(), workflow.checkpoint("safe point", ["artifacts/cp.txt"], "continue")),
                    lambda state: state["events"].__setitem__(
                        slice(None), [event for event in state["events"] if event["event"] != "checkpoint"]
                    ),
                ),
                "orphan-replan": (
                    lambda workflow: (
                        workflow.start_phase(),
                        workflow.fail_phase("check disagreed", ["tests/failure.txt"], "replan the phase"),
                        workflow.replan("change approach", ["tests/replan.txt"], "start again"),
                    ),
                    lambda state: state["replans"].append({**deepcopy(state["replans"][0]), "number": 2, "event_sequence": 99}),
                ),
                "missing-replan-event": (
                    lambda workflow: (
                        workflow.start_phase(),
                        workflow.fail_phase("check disagreed", ["tests/failure.txt"], "replan the phase"),
                        workflow.replan("change approach", ["tests/replan.txt"], "start again"),
                    ),
                    lambda state: state["events"].__setitem__(
                        slice(None), [event for event in state["events"] if event["event"] != "replanned"]
                    ),
                ),
            }
            for name, (setup, mutate) in cases.items():
                with self.subTest(case=name):
                    run_root = root / name
                    workflow = self.workflow(run_root, phase_count=1)
                    setup(workflow)
                    state_path = run_root / "run.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    mutate(state)
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(figure_it_out.StateError):
                        figure_it_out.Workflow.load(state_path, workspace_root=run_root)

            phase_pass = self.workflow(root / "phase-pass-decision", phase_count=1)
            phase_pass.start_phase()
            phase_pass.complete_phase(
                "output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/pass.txt"]},
            )
            state_path = root / "phase-pass-decision" / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            pass_event = next(event for event in state["events"] if event["event"] == "phase_passed")
            state["decisions"] = [
                decision for decision in state["decisions"] if decision["event_sequence"] != pass_event["sequence"]
            ]
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(state_path, workspace_root=root / "phase-pass-decision")

            forged = self.workflow(root / "forged-phase-pass-decision", phase_count=1)
            forged.start_phase()
            forged.complete_phase(
                "output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/pass.txt"]},
            )
            state_path = root / "forged-phase-pass-decision" / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            pass_event = next(event for event in state["events"] if event["event"] == "phase_passed")
            pass_event["decision"]["why"] = "forged decision"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(state_path, workspace_root=root / "forged-phase-pass-decision")

    def test_event_sequence_preserves_append_order_and_exports_overflow_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1, max_decisions=1)
            workflow.start_phase()
            timestamp = "2026-09-23T00:00:00+00:00"
            with patch.object(figure_it_out, "_now", return_value=timestamp):
                workflow.record_decision("phase-1", "first decision", "bounded test", ["tests/decision.txt"], "open")
                workflow.checkpoint("after decision", ["artifacts/checkpoint.txt"], "continue phase-1")
                for _ in range(2):
                    with self.assertRaises(figure_it_out.ScopeRefusal):
                        workflow.guard_action("deploy", "production")

            self.assertEqual([event["sequence"] for event in workflow.state["events"]], list(range(1, len(workflow.state["events"]) + 1)))
            audit_path = workflow.export_audit(root / "audit.tsv")
            with audit_path.open(encoding="utf-8", newline="") as handle:
                audit_rows = list(csv.reader(handle, delimiter="\t"))
            self.assertEqual(audit_rows[1][2], "first decision")
            self.assertEqual(audit_rows[2][2], "checkpoint: after decision")
            refusals = [row for row in audit_rows if row[2] == "refused action deploy"]
            self.assertEqual(len(refusals), 1)
            expected_audit_decisions = []
            for event in workflow.state["events"]:
                if event["event"] == "checkpoint":
                    expected_audit_decisions.append(f"checkpoint: {event['checkpoint_snapshot']['label']}")
                elif event["event"] in figure_it_out.DECISION_EVENTS:
                    expected_audit_decisions.append(event["decision"]["decision"])
            self.assertEqual([row[2] for row in audit_rows[1:]], expected_audit_decisions)
            self.assertEqual(workflow.state["events"][-1]["decision_in_list"], True)
            self.assertEqual(
                len([event for event in workflow.state["events"] if event["event"] == "action_refused"]),
                len(refusals),
            )

            forged = json.loads((root / "run.json").read_text(encoding="utf-8"))
            forged["events"][1]["sequence"] = forged["events"][0]["sequence"]
            (root / "run.json").write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(root / "run.json", workspace_root=root)

    def test_overflow_and_checkpoint_order_forgery_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overflow = self.workflow(root / "overflow", phase_count=1, max_decisions=2)
            with self.assertRaises(figure_it_out.ScopeRefusal):
                overflow.guard_action("deploy", "production")
            overflow_state_path = root / "overflow" / "run.json"
            overflow_state = json.loads(overflow_state_path.read_text(encoding="utf-8"))
            refusal_event = next(event for event in overflow_state["events"] if event["event"] == "action_refused")
            overflow_state["decisions"] = []
            refusal_event["decision_in_list"] = False
            refusal_event["decision_overflow"] = True
            overflow_state_path.write_text(json.dumps(overflow_state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(overflow_state_path, workspace_root=root / "overflow")

            checkpoints = self.workflow(root / "checkpoints", phase_count=1)
            checkpoints.start_phase()
            checkpoints.checkpoint("first", ["artifacts/first.txt"], "continue")
            checkpoints.checkpoint("second", ["artifacts/second.txt"], "continue")
            checkpoint_state_path = root / "checkpoints" / "run.json"
            checkpoint_state = json.loads(checkpoint_state_path.read_text(encoding="utf-8"))
            checkpoint_state["checkpoints"].reverse()
            checkpoint_state_path.write_text(json.dumps(checkpoint_state), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(checkpoint_state_path, workspace_root=root / "checkpoints")

    def test_multiple_overflow_refusals_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1, max_decisions=2)
            workflow.record_decision("phase-1", "first decision", "first", ["tests/first.txt"], "open")
            workflow.record_decision("phase-1", "second decision", "second", ["tests/second.txt"], "open")
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("deploy", "production")
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("deploy", "production")

            state_path = root / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            refusal_event = next(event for event in state["events"] if event["event"] == "action_refused")
            forged_overflow = deepcopy(refusal_event)
            forged_overflow["sequence"] = len(state["events"]) + 1
            forged_overflow["decision"]["event_sequence"] = forged_overflow["sequence"]
            forged_overflow["decision_in_list"] = False
            forged_overflow["decision_overflow"] = True
            state["events"].append(forged_overflow)
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(state_path, workspace_root=root)

    def test_decision_overflow_cannot_be_attached_to_a_generic_decision_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1, max_decisions=1)
            workflow.record_decision("phase-1", "first", "fills budget", ["tests/first.txt"], "open")
            state_path = root / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            forged = deepcopy(state["decisions"][0])
            forged["event_sequence"] = len(state["events"]) + 1
            forged.update(
                decision="forged overflow decision",
                why="forged overflow",
                result="blocked",
                status="refused",
                reason="forged overflow",
                next_action="stop",
            )
            state["decisions"].append(forged)
            state["events"].append(
                {
                    "sequence": len(state["events"]) + 1,
                    "at": forged["at"],
                    "event": "decision_recorded",
                    "phase": "phase-1",
                    "decision": deepcopy(forged),
                    "decision_in_list": True,
                    "decision_overflow": True,
                }
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(state_path, workspace_root=root)

    def test_refusal_decision_timestamps_must_be_timezone_aware_iso_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("deploy", "production")
            state_path = root / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            refusal_event = next(event for event in state["events"] if event["event"] == "action_refused")
            refusal_event["decision"]["at"] = "secret@host"
            state["decisions"][0]["at"] = "secret@host"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(state_path, workspace_root=root)

    def test_schema_one_state_is_explicitly_migrated_to_schema_two(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.completed_workflow(root)
            state_path = root / "run.json"
            legacy = json.loads(state_path.read_text(encoding="utf-8"))
            legacy["schema_version"] = 1
            legacy["plan"]["schema_version"] = 1
            for event in legacy["events"]:
                event.pop("sequence", None)
                event.pop("decision", None)
                event.pop("decision_in_list", None)
                event.pop("decision_overflow", None)
                if event["event"] == "phase_passed":
                    event.pop("output_snapshot", None)
                    event.pop("verification_snapshot", None)
                elif event["event"] == "predicate_recorded":
                    event.pop("result_snapshot", None)
                elif event["event"] == "completed":
                    event.pop("terminal_snapshot", None)
            for decision in legacy["decisions"]:
                decision.pop("event_sequence", None)
            for result in legacy["predicate_results"].values():
                result.pop("event_sequence", None)
            state_path.write_text(json.dumps(legacy), encoding="utf-8")

            migrated = figure_it_out.Workflow.load(state_path, workspace_root=root)
            self.assertEqual(migrated.state["schema_version"], 2)
            self.assertEqual(migrated.state["plan"]["schema_version"], 2)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 2)
            self.assertTrue(all("sequence" in event for event in persisted["events"]))
            self.assertTrue(all("event_sequence" in decision for decision in persisted["decisions"]))

    def test_original_schema_one_shapes_migrate_and_malformed_nested_shapes_use_cli_errors(self):
        def legacy_state(state):
            legacy = deepcopy(state)
            legacy["schema_version"] = 1
            legacy["plan"]["schema_version"] = 1
            legacy["plan"]["scope"].pop("logical_actions", None)
            for event in legacy["events"]:
                event.pop("sequence", None)
                event.pop("decision", None)
                event.pop("decision_in_list", None)
                event.pop("decision_overflow", None)
                for field in (
                    "checkpoint_snapshot",
                    "output_snapshot",
                    "verification_snapshot",
                    "failure_snapshot",
                    "interruption_snapshot",
                    "blocker_snapshot",
                    "result_snapshot",
                    "terminal_snapshot",
                ):
                    event.pop(field, None)
            for decision in legacy["decisions"]:
                decision.pop("event_sequence", None)
                decision.pop("status", None)
                decision.pop("reason", None)
                decision.pop("next_action", None)
            for result in legacy["predicate_results"].values():
                if result is not None:
                    result.pop("status", None)
                    result.pop("event_sequence", None)
            return legacy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            replanned = self.workflow(root / "replanned", phase_count=1)
            replanned.start_phase()
            replanned.fail_phase("check disagreed", ["tests/failure.txt"], "retry the phase")
            replanned.replan("change the bounded approach", ["tests/replan.txt"], "start again")
            replan_state_path = root / "replanned" / "run.json"
            replan_legacy = legacy_state(json.loads(replan_state_path.read_text(encoding="utf-8")))
            replan_legacy["replans"][0]["replacement"] = replan_legacy["phases"][0]["verification"]
            replan_state_path.write_text(json.dumps(replan_legacy), encoding="utf-8")
            migrated_replan = figure_it_out.Workflow.load(replan_state_path, workspace_root=root / "replanned")
            self.assertEqual(migrated_replan.state["status"], "planned")
            self.assertEqual(
                migrated_replan.state["replans"][0]["replacement"],
                migrated_replan.state["plan"]["phases"][0],
            )

            refused = self.workflow(root / "refused", phase_count=1)
            with self.assertRaises(figure_it_out.ScopeRefusal):
                refused.guard_action("deploy", "token@host")
            refusal_state_path = root / "refused" / "run.json"
            refusal_legacy = legacy_state(json.loads(refusal_state_path.read_text(encoding="utf-8")))
            refusal_event = refusal_legacy["events"][0]
            refusal_event.pop("event", None)
            refusal_event["target"] = "token@host"
            refusal_event["private"] = {"target": "token@host"}
            refusal_decision = refusal_legacy["decisions"][0]
            refusal_decision["evidence"] = ["scope:deploy", "target:token@host"]
            refusal_legacy["decisions"] = [refusal_decision]
            refusal_state_path.write_text(json.dumps(refusal_legacy), encoding="utf-8")
            migrated_refusal = figure_it_out.Workflow.load(refusal_state_path, workspace_root=root / "refused")
            self.assertEqual(migrated_refusal.state["events"][0]["event"], "action_refused")
            self.assertEqual(migrated_refusal.state["events"][0]["target"], "<invalid-target>")
            self.assertEqual(len(migrated_refusal.state["decisions"]), 1)
            self.assertNotIn("token@host", json.dumps(migrated_refusal.state))

            orphan_legacy = legacy_state(json.loads(refusal_state_path.read_text(encoding="utf-8")))
            orphan_legacy["events"] = []
            orphan_decision = orphan_legacy["decisions"][0]
            orphan_decision["evidence"] = ["scope:deploy", "target:token@host"]
            orphan_decision["why"] = "target token@host was out of scope"
            orphan_decision["reason"] = "target token@host was out of scope"
            orphan_decision["private"] = {"target": "token@host"}
            orphan_path = root / "orphan-refusal.json"
            orphan_path.write_text(json.dumps(orphan_legacy), encoding="utf-8")
            migrated_orphan = figure_it_out.Workflow.load(orphan_path, workspace_root=root)
            audit_path = migrated_orphan.export_audit(root / "orphan-refusal.tsv")
            migrated_orphan_text = json.dumps(migrated_orphan.state)
            audit_text = audit_path.read_text(encoding="utf-8")
            self.assertNotIn("token@host", migrated_orphan_text)
            self.assertNotIn("token@host", audit_text)

            mislabeled_legacy = legacy_state(json.loads(refusal_state_path.read_text(encoding="utf-8")))
            mislabeled_legacy["events"] = [{
                "at": "token@host",
                "event": "decision_recorded",
                "phase": "token@host",
                "target": "token@host",
                "reason": "target token@host was out of scope",
                "next_action": "next target token@host",
                "action": "deploy token@host",
                "evidence": ["target:token@host"],
                "why": "why token@host",
                "private": {"target": "token@host"},
            }]
            mislabeled_decision = mislabeled_legacy["decisions"][0]
            mislabeled_decision["evidence"] = ["scope:deploy", "target:token@host"]
            mislabeled_decision["why"] = "target token@host was out of scope"
            mislabeled_decision["reason"] = "target token@host was out of scope"
            mislabeled_path = root / "mislabeled-refusal.json"
            mislabeled_path.write_text(json.dumps(mislabeled_legacy), encoding="utf-8")
            migrated_mislabeled = figure_it_out.Workflow.load(mislabeled_path, workspace_root=root)
            mislabeled_audit = migrated_mislabeled.export_audit(root / "mislabeled-refusal.tsv").read_text(encoding="utf-8")
            self.assertNotIn("token@host", json.dumps(migrated_mislabeled.state))
            self.assertNotIn("token@host", mislabeled_audit)
            self.assertNotIn("next_action", migrated_mislabeled.state["events"][0])

            explicit_legacy = legacy_state(json.loads(refusal_state_path.read_text(encoding="utf-8")))
            explicit_event = explicit_legacy["events"][0]
            explicit_event.update(
                {
                    "event": "action_refused",
                    "phase": "phase-1",
                    "action": "deploy token@host",
                    "target": "token@host",
                }
            )
            explicit_path = root / "explicit-raw-action.json"
            explicit_path.write_text(json.dumps(explicit_legacy), encoding="utf-8")
            migrated_explicit = figure_it_out.Workflow.load(explicit_path, workspace_root=root)
            self.assertNotIn("token@host", json.dumps(migrated_explicit.state))
            self.assertEqual(migrated_explicit.state["events"][0]["action"], "deploy")

            malformed_cases = {
                "predicate-results": lambda state: state.update(predicate_results=[]),
                "phase-checkpoints": lambda state: state["phases"][0].update(checkpoints={}),
                "checkpoint-id": lambda state: state["events"].__setitem__(
                    0, {"event": "checkpoint", "checkpoint": []}
                ),
            }
            for name, mutate in malformed_cases.items():
                with self.subTest(case=name):
                    malformed = deepcopy(refusal_legacy)
                    mutate(malformed)
                    malformed_path = root / f"malformed-{name}.json"
                    malformed_path.write_text(json.dumps(malformed), encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT),
                            "guard-action",
                            "--state",
                            str(malformed_path),
                            "--workspace-root",
                            str(root),
                            "--action",
                            "write",
                            "--target",
                            "src/file.py",
                        ],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertTrue(json.loads(result.stderr)["error"])
                    self.assertEqual(result.stdout, "")

    def test_original_schema_one_multiple_interruption_cycles_migrate_from_event_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            workflow.start_phase()
            workflow.checkpoint("first safe point", ["artifacts/first.txt"], "resume from first")
            workflow.interrupt("first stop")
            workflow.resume()
            workflow.checkpoint("second safe point", ["artifacts/second.txt"], "resume from second")
            workflow.interrupt("second stop")
            workflow.resume()
            workflow.complete_phase(
                "resumed output",
                {"check_id": "check-1", "passed": True, "evidence": ["tests/resumed.txt"]},
            )
            workflow.record_predicate("focused-tests", True, ["tests/focused.txt"])
            workflow.record_predicate("artifact-inspection", True, ["artifacts/inspection.txt"])
            workflow.finish()

            state_path = root / "run.json"
            legacy = json.loads(state_path.read_text(encoding="utf-8"))
            legacy["schema_version"] = 1
            legacy["plan"]["schema_version"] = 1
            legacy["plan"]["scope"].pop("logical_actions", None)
            for event in legacy["events"]:
                event.pop("sequence", None)
                event.pop("decision", None)
                event.pop("decision_in_list", None)
                event.pop("decision_overflow", None)
                event.pop("reason", None)
                for field in (
                    "checkpoint_snapshot",
                    "output_snapshot",
                    "verification_snapshot",
                    "failure_snapshot",
                    "interruption_snapshot",
                    "blocker_snapshot",
                    "result_snapshot",
                    "terminal_snapshot",
                ):
                    event.pop(field, None)
            for checkpoint in legacy["checkpoints"]:
                checkpoint.pop("event_sequence", None)
            for replan in legacy["replans"]:
                replan.pop("event_sequence", None)
            for decision in legacy["decisions"]:
                decision.pop("event_sequence", None)
                decision.pop("status", None)
                decision.pop("reason", None)
                decision.pop("next_action", None)
            for result in legacy["predicate_results"].values():
                result.pop("event_sequence", None)
                result.pop("status", None)
            malformed = deepcopy(legacy)
            next(event for event in malformed["events"] if event["event"] == "interrupted")["checkpoint"] = "cp-missing"
            malformed_path = root / "malformed.json"
            malformed_path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(malformed_path, workspace_root=root)

            state_path.write_text(json.dumps(legacy), encoding="utf-8")

            migrated = figure_it_out.Workflow.load(state_path, workspace_root=root)
            interruptions = [
                event["interruption_snapshot"]
                for event in migrated.state["events"]
                if event["event"] == "interrupted"
            ]
            self.assertEqual([snapshot["checkpoint"] for snapshot in interruptions], ["cp-1", "cp-2"])
            self.assertEqual(
                [snapshot["reason"] for snapshot in interruptions],
                ["legacy interruption reason unavailable", "second stop"],
            )
            self.assertEqual(migrated.state["resume_count"], 2)
            self.assertEqual(migrated.state["status"], "completed")

    def test_schema_one_orphaned_refusal_uses_sanitized_event_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("deploy", "production")
            state_path = root / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 1
            state["plan"]["schema_version"] = 1
            for event in state["events"]:
                event.pop("sequence", None)
                event.pop("decision", None)
                event.pop("decision_in_list", None)
                event.pop("decision_overflow", None)
            for decision in state["decisions"]:
                decision.pop("event_sequence", None)
            refusal = state["decisions"][0]
            refusal["at"] = "orphaned-secret@host"
            refusal["phase"] = "orphaned-secret-phase"
            state["events"] = [
                event for event in state["events"] if event["event"] != "action_refused"
            ]
            state_path.write_text(json.dumps(state), encoding="utf-8")

            migrated = figure_it_out.Workflow.load(state_path, workspace_root=root)
            synthetic = next(
                event for event in migrated.state["events"] if event["event"] == "decision_recorded"
            )
            self.assertNotEqual(synthetic["at"], "orphaned-secret@host")
            self.assertEqual(synthetic["phase"], "run")
            self.assertEqual(synthetic["phase"], synthetic["decision"]["phase"])

    def test_schema_two_refusal_rejects_untrusted_action_and_extra_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("deploy", "production")
            state_path = root / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            refusal = next(event for event in state["events"] if event["event"] == "action_refused")
            refusal["action"] = "deploy token@host"
            refusal["private"] = "secret"
            refusal["evidence"] = ["https://example.invalid/leak"]
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(state_path, workspace_root=root)

            for location in ("event", "row"):
                with self.subTest(location=location):
                    nested_state = json.loads(state_path.read_text(encoding="utf-8"))
                    refusal_event = next(
                        event for event in nested_state["events"] if event["event"] == "action_refused"
                    )
                    target = refusal_event["decision"] if location == "event" else nested_state["decisions"][0]
                    target["private"] = "secret"
                    nested_path = root / f"run-{location}.json"
                    nested_path.write_text(json.dumps(nested_state), encoding="utf-8")

                    with self.assertRaises(figure_it_out.StateError):
                        figure_it_out.Workflow.load(nested_path, workspace_root=root)

            nested_state = json.loads(state_path.read_text(encoding="utf-8"))
            refusal_event = next(
                event for event in nested_state["events"] if event["event"] == "action_refused"
            )
            forged_evidence = ["target:token@host"]
            refusal_event["decision"]["evidence"] = forged_evidence
            nested_state["decisions"][0]["evidence"] = deepcopy(forged_evidence)
            nested_path = root / "run-noncanonical-evidence.json"
            nested_path.write_text(json.dumps(nested_state), encoding="utf-8")

            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(nested_path, workspace_root=root)

    def test_schema_two_refusal_rejects_forged_reason_and_next_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            with self.assertRaises(figure_it_out.ScopeRefusal):
                workflow.guard_action("deploy", "production")
            state_path = root / "run.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            refusal_event = next(event for event in state["events"] if event["event"] == "action_refused")
            refusal_event["reason"] = "secret@host"
            refusal_event["next_action"] = "send secret@host"
            refusal_event["decision"]["why"] = refusal_event["reason"]
            refusal_event["decision"]["reason"] = refusal_event["reason"]
            refusal_event["decision"]["next_action"] = refusal_event["next_action"]
            state["decisions"][0]["why"] = refusal_event["reason"]
            state["decisions"][0]["reason"] = refusal_event["reason"]
            state["decisions"][0]["next_action"] = refusal_event["next_action"]
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaises(figure_it_out.StateError):
                figure_it_out.Workflow.load(state_path, workspace_root=root)

    def test_persisted_allowed_actions_must_match_lifecycle_status(self):
        def failed(workflow):
            workflow.start_phase()
            workflow.fail_phase("check disagreed", ["tests/failure.txt"], "replan the phase")

        def interrupted(workflow):
            workflow.start_phase()
            workflow.checkpoint("safe stop", ["artifacts/checkpoint.txt"], "resume phase-1")
            workflow.interrupt("session stopped")

        def blocked(workflow):
            workflow.block("fixture unavailable", ["fixtures/missing.json"], "obtain the fixture")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for status, setup in (("failed", failed), ("interrupted", interrupted), ("blocked", blocked)):
                with self.subTest(status=status):
                    run_root = root / status
                    workflow = self.workflow(run_root, phase_count=1)
                    setup(workflow)
                    state_path = run_root / "run.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["events"].append(
                        {
                            "at": "2026-09-23T00:00:00+00:00",
                            "event": "action_allowed",
                            "action": "write",
                            "target": "src/after-stop.py",
                        }
                    )
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(figure_it_out.StateError):
                        figure_it_out.Workflow.load(state_path, workspace_root=run_root)

    def test_cli_rejects_non_regular_inputs_and_invalid_utf8_plan_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_destinations = {"directory": root / "state-directory", "fifo": root / "state-fifo"}
            state_destinations["directory"].mkdir()
            os.mkfifo(state_destinations["fifo"])
            for kind, destination in state_destinations.items():
                with self.subTest(input=f"state-{kind}"):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT),
                            "start-phase",
                            "--state",
                            str(destination),
                            "--workspace-root",
                            str(root),
                        ],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=2,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertTrue(json.loads(result.stderr)["error"])

            plan_path = root / "valid-plan.json"
            plan_path.write_text(json.dumps(make_plan(phase_count=1)), encoding="utf-8")
            workspace_destinations = {"file": root / "workspace-file", "fifo": root / "workspace-fifo"}
            workspace_destinations["file"].write_text("workspace", encoding="utf-8")
            os.mkfifo(workspace_destinations["fifo"])
            for kind, destination in workspace_destinations.items():
                with self.subTest(input=f"workspace-{kind}"):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT),
                            "init",
                            "--plan",
                            str(plan_path),
                            "--state",
                            "run.json",
                            "--workspace-root",
                            str(destination),
                        ],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=2,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn("Traceback", result.stderr)
                    error = json.loads(result.stderr)
                    self.assertIn("workspace root must be a directory", error["error"])

            plan_destinations = {"directory": root / "plan-directory", "fifo": root / "plan-fifo"}
            plan_destinations["directory"].mkdir()
            os.mkfifo(plan_destinations["fifo"])
            for kind, destination in plan_destinations.items():
                with self.subTest(input=f"plan-{kind}"):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT),
                            "init",
                            "--plan",
                            str(destination),
                            "--state",
                            str(root / f"{kind}-state.json"),
                            "--workspace-root",
                            str(root),
                        ],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=2,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertTrue(json.loads(result.stderr)["error"])

            invalid_utf8 = root / "invalid-utf8.json"
            invalid_utf8.write_bytes(b"{\xff")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "init",
                    "--plan",
                    str(invalid_utf8),
                    "--state",
                    str(root / "utf8-state.json"),
                    "--workspace-root",
                    str(root),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stderr)
            self.assertIn("valid UTF-8", error["error"])
            self.assertNotIn("Traceback", result.stderr)

    def test_plan_rejects_uri_and_drive_relative_scope_prefixes(self):
        for prefix in ("https://example.test/", "file:///tmp/", "C:relative/", "C:/absolute/", "./https://example.test/"):
            with self.subTest(prefix=prefix):
                plan = make_plan()
                plan["scope"]["allowed_paths"] = [prefix]
                with self.assertRaises(figure_it_out.PlanError):
                    figure_it_out.validate_plan(plan)

    def test_audit_refuses_state_path_without_overwriting_resume_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.workflow(root, phase_count=1)
            before = (root / "run.json").read_bytes()
            with self.assertRaises(figure_it_out.ScopeRefusal) as refusal:
                workflow.export_audit(root / "run.json")
            self.assertEqual(refusal.exception.refusal["status"], "refused")
            self.assertTrue(refusal.exception.refusal["next_action"])
            self.assertEqual((root / "run.json").read_bytes(), before)
            self.assertEqual(
                figure_it_out.Workflow.load(root / "run.json", workspace_root=root).state["status"],
                "planned",
            )

    def test_validation_rejects_unbounded_or_incomplete_plans(self):
        plan = make_plan()
        del plan["limits"]["max_checkpoints"]
        with self.assertRaises(figure_it_out.PlanError):
            figure_it_out.validate_plan(plan)
        plan = make_plan()
        plan["phases"][0]["verification"] = {"id": "check-1"}
        with self.assertRaises(figure_it_out.PlanError):
            figure_it_out.validate_plan(plan)

    def test_cli_init_writes_a_loadable_state_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(make_plan(phase_count=1)), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "init", "--plan", str(plan_path), "--state", "run.json", "--workspace-root", str(root)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads((root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "planned")
            self.assertEqual(json.loads(result.stdout)["run_id"], state["run_id"])


class SkillContractTests(unittest.TestCase):
    def test_frontmatter_and_local_links_are_valid(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        end = text.find("\n---", 4)
        self.assertGreater(end, 0)
        frontmatter = text[4:end]
        fields = {}
        for line in frontmatter.splitlines():
            if not line.strip():
                continue
            key, separator, value = line.partition(":")
            self.assertEqual(separator, ":")
            fields[key.strip()] = value.strip()
        self.assertEqual(set(fields), {"name", "description"})
        self.assertEqual(fields["name"], "figure-it-out")
        self.assertTrue(fields["description"])

        links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
        self.assertGreaterEqual(len(links), 8)
        for destination in links:
            if re.match(r"^[a-z]+://", destination):
                continue
            target = destination.split("#", 1)[0]
            self.assertTrue((SKILL.parent / target).is_file(), destination)

    def test_skill_has_no_runtime_specific_seams(self):
        text = SKILL.read_text(encoding="utf-8")
        for marker in (".cursor/", "agent-transcripts", "subagent_type", "run_in_background", "playbooks/"):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
