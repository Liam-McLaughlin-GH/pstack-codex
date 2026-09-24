#!/usr/bin/env python3
"""Offline acceptance checks for G1 playbook and workflow parity."""

from __future__ import annotations

import importlib.util
import json
import posixpath
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_DIR = ROOT / "skills" / "poteto-mode" / "playbooks"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

PLAYBOOK_INVENTORY = json.loads(
    (FIXTURES / "playbook_inventory.json").read_text(encoding="utf-8")
)
PLAYBOOK_NAMES = set(PLAYBOOK_INVENTORY["names"])

WORKFLOW_PATHS = (
    ROOT / "skills" / "poteto-mode" / "SKILL.md",
    ROOT / "skills" / "how" / "SKILL.md",
    ROOT / "skills" / "why" / "SKILL.md",
    ROOT / "skills" / "architect" / "SKILL.md",
    ROOT / "skills" / "arena" / "SKILL.md",
    ROOT / "skills" / "swarm" / "SKILL.md",
    ROOT / "skills" / "interrogate" / "SKILL.md",
    ROOT / "skills" / "show-me-your-work" / "SKILL.md",
    ROOT / "skills" / "no-comments" / "SKILL.md",
)


def load_verifier():
    path = ROOT / "scripts" / "verify_port.py"
    spec = importlib.util.spec_from_file_location("g1_verify_port", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load scripts/verify_port.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixture_data() -> dict[str, object]:
    return json.loads(
        (FIXTURES / "representative_runs.json").read_text(encoding="utf-8")
    )


def contract_data() -> dict[str, list[str]]:
    return json.loads(
        (FIXTURES / "playbook_contracts.json").read_text(encoding="utf-8")
    )


def side_effect_contract_data() -> dict[str, dict[str, object]]:
    return json.loads(
        (FIXTURES / "side_effect_contracts.json").read_text(encoding="utf-8")
    )


def diagnostic_contract_data() -> dict[str, dict[str, object]]:
    return json.loads(
        (FIXTURES / "diagnostic_contracts.json").read_text(encoding="utf-8")
    )


class FakeSideEffectGate:
    """Fixture-only adapter that makes authorization observable."""

    OPERATIONS = {
        "pr-create", "pr-edit", "pr-ready", "push", "comment", "merge",
        "auto-merge", "deployment", "message", "watcher", "agent-spawn",
        "worktree-removal", "simulator", "cache", "runtime", "repository",
    }
    DELETION_OPERATIONS = {
        "worktree-removal", "simulator", "cache", "runtime", "repository",
    }

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.contracts = side_effect_contract_data()

    def request(
        self,
        operation: str,
        *,
        authorization: "AuthorizationRecord | None" = None,
        target: str | None = None,
        evidence: str | None = None,
    ) -> bool:
        if operation not in self.OPERATIONS:
            raise ValueError(f"unsupported fixture operation: {operation}")
        if not isinstance(authorization, AuthorizationRecord):
            return False
        if authorization.operation != operation:
            return False
        if not isinstance(target, str) or not target:
            raise ValueError(f"{operation} requires an exact target and evidence")
        if not isinstance(evidence, str) or not evidence:
            raise ValueError(f"{operation} requires an exact target and evidence")
        contract = self.contracts[operation]
        valid_target = str(contract["valid_target"])
        valid_evidence = str(contract["valid_evidence"])
        if authorization.target != valid_target or authorization.evidence != valid_evidence:
            return False
        if authorization.target != target or authorization.evidence != evidence:
            raise ValueError(f"{operation} authorization does not match the exact request")
        if operation in self.DELETION_OPERATIONS:
            target_prefix = str(contract["target_prefix"])
            if not target.startswith(target_prefix):
                raise ValueError(f"{operation} target has the wrong kind")
            target_path = target[len(target_prefix):]
            if ".." in target_path.split("/"):
                raise ValueError(f"{operation} target may not traverse directories")
            normalized_target = posixpath.normpath(target_path)
            inventory_paths = {
                posixpath.normpath(str(path))
                for path in contract["inventory_paths"]
            }
            if normalized_target not in inventory_paths:
                raise ValueError(f"{operation} target is outside the fixture inventory")
        if target != valid_target:
            raise ValueError(f"{operation} target is not the exact fixture target")
        if evidence != valid_evidence:
            raise ValueError(f"{operation} evidence is not the exact fixture receipt")
        self.calls.append(operation)
        return True


@dataclass(frozen=True)
class AuthorizationRecord:
    operation: str
    target: str
    evidence: str


def fixture_authorization(operation: str) -> AuthorizationRecord:
    contract = side_effect_contract_data()[operation]
    return AuthorizationRecord(
        operation=operation,
        target=str(contract["valid_target"]),
        evidence=str(contract["valid_evidence"]),
    )


def run_autonomous(outcomes: list[str]) -> tuple[str, int]:
    for iteration, outcome in enumerate(outcomes, start=1):
        if outcome == "predicate-met":
            return "PASS", iteration
    return "BLOCKED", len(outcomes)


def shipping_status(
    verified: object,
    authorization: AuthorizationRecord | None,
    gate: FakeSideEffectGate,
) -> str:
    if verified is not True:
        return "BLOCKED"
    if not isinstance(authorization, AuthorizationRecord):
        return "NOT RUN"
    if not gate.request(
        "merge",
        authorization=authorization,
        target=authorization.target,
        evidence=authorization.evidence,
    ):
        return "NOT RUN"
    return "PASS"


def pause_checkpoint(explicit_pause: object) -> bool:
    return explicit_pause if isinstance(explicit_pause, bool) else False


def dry_run_cleanup(candidates: list[dict[str, object]]) -> tuple[list[str], int]:
    proposals = [
        str(candidate["path"])
        for candidate in candidates
        if candidate["merged"] and not candidate["dirty"] and not candidate["active"]
    ]
    return proposals, 0


def start_autopilot(go: object) -> tuple[str, int]:
    return ("RUN", 1) if go is True else ("NOT RUN", 0)


def perf_status(surface_available: object) -> tuple[str, int]:
    return ("RUN", 1) if surface_available is True else ("BLOCKED", 0)


DIAGNOSTIC_CONTRACTS = diagnostic_contract_data()


def diagnostic_state(
    workflow: str,
    capability_available: object,
    artifact_supplied: object,
    artifact_matches: object,
    authorized: object,
    gate: FakeSideEffectGate,
    *,
    capture_ready: object = True,
) -> tuple[str, int]:
    contract = DIAGNOSTIC_CONTRACTS.get(workflow)
    if contract is None:
        raise ValueError(f"unsupported diagnostic workflow: {workflow}")
    artifact_required = contract["artifact_required"]
    if authorized is not True or capture_ready is not True:
        return "NOT RUN", 0
    if capability_available is not True:
        return "BLOCKED", 0
    if artifact_supplied is not True:
        return "BLOCKED", 0
    if artifact_matches is not True:
        return "INCONCLUSIVE", 0
    return "PASS", 0


def pr_handoff(
    change_retained: bool,
    authorized: object,
    evidence: dict[str, object],
    *,
    expected_target: str | None = None,
    expected_head_sha: str | None = None,
    expected_checks: str | None = None,
) -> dict[str, object]:
    if change_retained is not True:
        return {"status": "NOT RUN", "local_handoff_ready": False}
    canonical = fixture_data()["pr_handoff_contract"]
    if any(
        supplied is not None and supplied != canonical[field]
        for supplied, field in (
            (expected_target, "target"),
            (expected_head_sha, "head_sha"),
            (expected_checks, "checks"),
        )
    ):
        return {"status": "BLOCKED", "local_handoff_ready": False}
    local_handoff_ready = all(
        isinstance(evidence.get(field), str) and bool(evidence.get(field))
        for field in ("diff", "head_sha", "checks", "body", "target")
    )
    local_handoff_ready = local_handoff_ready and (
        evidence["target"] == canonical["target"]
        and evidence["head_sha"] == canonical["head_sha"]
        and evidence["checks"] == canonical["checks"]
    )
    if not local_handoff_ready:
        return {"status": "BLOCKED", "local_handoff_ready": False}
    return {
        "status": "PASS" if authorized is True else "NOT RUN",
        "local_handoff_ready": True,
    }


class G1ParityTests(unittest.TestCase):
    def test_inventory_matches_the_23_upstream_playbook_names(self) -> None:
        actual = {path.name for path in PLAYBOOK_DIR.glob("*.md")}
        self.assertEqual(actual, PLAYBOOK_NAMES)
        self.assertEqual(len(actual), 23)
        self.assertEqual(
            PLAYBOOK_INVENTORY["source_commit"],
            "53e579f1481697931fc44f5445171397cfa2b24b",
        )
        self.assertEqual(
            PLAYBOOK_INVENTORY["source_path"],
            "pstack/skills/poteto-mode/playbooks",
        )

    def test_targeted_links_and_repository_verifier_are_clean(self) -> None:
        verifier = load_verifier()
        errors: list[str] = []
        documents = list(PLAYBOOK_DIR.glob("*.md")) + list(WORKFLOW_PATHS)
        for document in documents:
            verifier._check_links(
                ROOT,
                document,
                document.read_text(encoding="utf-8"),
                errors,
            )
        self.assertEqual(errors, [])
        self.assertEqual(verifier.verify_port(ROOT, run_helpers=False), [])

    def test_every_playbook_names_an_explicit_unsupported_boundary(self) -> None:
        boundary_words = (
            "forge",
            "watcher",
            "browser",
            "worker",
            "deployment",
            "deletion",
            "surface",
            "external",
            "private",
            "transcript",
            "adapter",
        )
        for path in sorted(PLAYBOOK_DIR.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            self.assertIn("UNSUPPORTED-SEAM:", text, path.name)
            self.assertTrue(
                any(word in text.casefold() for word in boundary_words),
                path.name,
            )

    def test_external_operations_stop_without_authorization(self) -> None:
        gate = FakeSideEffectGate()
        self.assertFalse(gate.request("merge"))
        self.assertFalse(gate.request("deployment"))
        self.assertEqual(gate.calls, [])

        self.assertTrue(
            gate.request(
                "merge",
                authorization=fixture_authorization("merge"),
                target="pr:24",
                evidence="head-verdict:sha=abc123;verdict=pass",
            )
        )
        self.assertEqual(gate.calls, ["merge"])
        with self.assertRaises(ValueError):
            gate.request("unexpected-operation", target="x", evidence="y")

    def test_side_effect_gate_rejects_missing_and_mismatched_fields(self) -> None:
        contracts = side_effect_contract_data()
        for operation, contract in contracts.items():
            valid_target = str(contract["valid_target"])
            valid_evidence = str(contract["valid_evidence"])
            for target, evidence in (
                (None, valid_evidence),
                (valid_target, None),
                ("wrong:target", valid_evidence),
                (valid_target, "wrong:evidence"),
            ):
                gate = FakeSideEffectGate()
                with self.assertRaises(ValueError, msg=operation):
                    gate.request(
                        operation,
                        authorization=fixture_authorization(operation),
                        target=target,
                        evidence=evidence,
                    )
                self.assertEqual(gate.calls, [], operation)

    def test_side_effect_gate_rejects_same_prefix_and_empty_values(self) -> None:
        for operation, contract in side_effect_contract_data().items():
            target_prefix = str(contract["target_prefix"])
            evidence_prefix = str(contract["evidence_prefix"])
            fields = [str(field) for field in contract["evidence_fields"]]
            attacker_evidence = evidence_prefix + ";".join(
                f"{field}=ATTACKER-VALUE" for field in fields
            )
            empty_evidence = evidence_prefix + ";".join(
                f"{field}=" for field in fields
            )
            for target, evidence in (
                (target_prefix + "ATTACKER-TARGET", str(contract["valid_evidence"])),
                (str(contract["valid_target"]), attacker_evidence),
                (str(contract["valid_target"]), empty_evidence),
            ):
                gate = FakeSideEffectGate()
                with self.assertRaises(ValueError, msg=operation):
                    gate.request(
                        operation,
                        authorization=fixture_authorization(operation),
                        target=target,
                        evidence=evidence,
                    )
                self.assertEqual(gate.calls, [], operation)

    def test_deletion_gate_rejects_traversal_and_uninventoried_paths(self) -> None:
        deletion_operations = {
            "worktree-removal",
            "simulator",
            "cache",
            "runtime",
            "repository",
        }
        for operation in deletion_operations:
            contract = side_effect_contract_data()[operation]
            target_prefix = str(contract["target_prefix"])
            evidence = str(contract["valid_evidence"])
            for target in (
                target_prefix + "/tmp/fixture/../../etc/passwd",
                target_prefix + "/tmp/not-in-inventory",
            ):
                gate = FakeSideEffectGate()
                with self.assertRaises(ValueError, msg=operation):
                    gate.request(
                        operation,
                        authorization=fixture_authorization(operation),
                        target=target,
                        evidence=evidence,
                    )
                self.assertEqual(gate.calls, [], operation)

    def test_side_effect_fixture_binds_named_playbooks_to_the_gate(self) -> None:
        contracts = side_effect_contract_data()
        expected_operations = {
            "pr-create", "pr-edit", "pr-ready", "push", "comment", "merge",
            "auto-merge", "deployment", "message", "watcher", "agent-spawn",
            "worktree-removal", "simulator", "cache", "runtime", "repository",
        }
        self.assertEqual(set(contracts), expected_operations)
        for operation, contract in contracts.items():
            filename = contract["playbook"]
            text = (PLAYBOOK_DIR / filename).read_text(encoding="utf-8").casefold()
            self.assertIn("unsupported-seam:", text, filename)
            for marker in contract["markers"]:
                self.assertIn(marker, text, f"{filename} lacks {operation} marker {marker!r}")
            gate = FakeSideEffectGate()
            self.assertFalse(gate.request(operation))
            self.assertEqual(gate.calls, [])
            self.assertTrue(
                gate.request(
                    operation,
                    authorization=fixture_authorization(operation),
                    target=str(contract["valid_target"]),
                    evidence=str(contract["valid_evidence"]),
                )
            )
            self.assertEqual(gate.calls, [operation])

    def test_authorization_records_cannot_be_reused_across_operations(self) -> None:
        operations = tuple(side_effect_contract_data())
        for operation in operations:
            for other in operations:
                if operation == other:
                    continue
                contract = side_effect_contract_data()[operation]
                gate = FakeSideEffectGate()
                self.assertFalse(
                    gate.request(
                        other,
                        authorization=fixture_authorization(operation),
                        target=str(side_effect_contract_data()[other]["valid_target"]),
                        evidence=str(side_effect_contract_data()[other]["valid_evidence"]),
                    ),
                    f"{operation} authorization reused for {other}",
                )
                self.assertEqual(gate.calls, [])

    def test_malformed_boolean_state_never_runs_or_writes(self) -> None:
        gate = FakeSideEffectGate()
        self.assertEqual(shipping_status("false", fixture_authorization("merge"), gate), "BLOCKED")
        self.assertEqual(gate.calls, [])
        self.assertEqual(start_autopilot("false"), ("NOT RUN", 0))
        self.assertEqual(perf_status("false"), ("BLOCKED", 0))
        self.assertFalse(pause_checkpoint("false"))
        for workflow in DIAGNOSTIC_CONTRACTS:
            self.assertEqual(
                diagnostic_state(workflow, "true", True, True, True, gate),
                ("BLOCKED", 0),
            )
            self.assertEqual(
                diagnostic_state(workflow, True, True, True, True, gate, capture_ready="false"),
                ("NOT RUN", 0),
            )
        self.assertEqual(gate.calls, [])

    def test_handoff_expectations_cannot_define_a_new_contract(self) -> None:
        attacker = {
            "target": "attacker-target",
            "diff": "diff",
            "head_sha": "attacker-head",
            "checks": "attacker-checks",
            "body": "body",
        }
        self.assertEqual(
            pr_handoff(
                True,
                True,
                attacker,
                expected_target="attacker-target",
                expected_head_sha="attacker-head",
                expected_checks="attacker-checks",
            ),
            {"status": "BLOCKED", "local_handoff_ready": False},
        )

    def test_contract_fixture_checks_the_actual_playbooks(self) -> None:
        contracts = contract_data()
        self.assertEqual(set(contracts), PLAYBOOK_NAMES)
        self.assertEqual(len(contracts), 23)
        for filename, markers in contracts.items():
            text = (PLAYBOOK_DIR / filename).read_text(encoding="utf-8")
            for marker in markers:
                self.assertIn(marker, text, f"{filename} is missing {marker!r}")
            self.assertIn("UNSUPPORTED-SEAM:", text, filename)

    def test_diagnostic_contract_requires_artifacts_for_every_workflow(self) -> None:
        contracts = diagnostic_contract_data()
        self.assertEqual(
            set(contracts),
            {"investigation", "runtime-forensics", "trace-forensics", "perf-issue"},
        )
        for workflow, contract in contracts.items():
            self.assertIs(contract["artifact_required"], True, workflow)
            self.assertEqual(contract["playbook"], f"{workflow}.md", workflow)
            gate = FakeSideEffectGate()
            self.assertEqual(
                diagnostic_state(workflow, True, False, False, True, gate),
                ("BLOCKED", 0),
                workflow,
            )
            self.assertEqual(gate.calls, [], workflow)

    def test_pr_handoff_rejects_failing_or_wrong_identity_evidence(self) -> None:
        valid = {
            "target": "pr:24",
            "diff": "diff",
            "head_sha": "abc123",
            "checks": "pass",
            "body": "body",
        }
        for evidence in (
            {**valid, "checks": "FAIL"},
            {**valid, "head_sha": "not-the-head"},
            {**valid, "target": "pr:25"},
            {**valid, "head_sha": ""},
            {**valid, "checks": ""},
        ):
            self.assertEqual(
                pr_handoff(
                    True,
                    True,
                    evidence,
                ),
                {"status": "BLOCKED", "local_handoff_ready": False},
            )
        self.assertEqual(
            pr_handoff(
                True,
                "authorized",
                valid,
            ),
            {"status": "NOT RUN", "local_handoff_ready": True},
        )

    def test_representative_state_and_stop_paths(self) -> None:
        cases = fixture_data()
        autonomous = cases["autonomous"]
        self.assertEqual(
            run_autonomous(autonomous["outcomes"]),
            ("PASS", autonomous["expected_iterations"]),
        )
        dead_end = cases["autonomous_dead_end"]
        self.assertEqual(
            run_autonomous(dead_end["outcomes"]),
            ("BLOCKED", dead_end["expected_iterations"]),
        )

        no_auth = cases["shipping_without_authorization"]
        gate = FakeSideEffectGate()
        self.assertEqual(
            shipping_status(
                no_auth["verified"],
                fixture_authorization("merge") if no_auth["authorized"] else None,
                gate,
            ),
            no_auth["expected_status"],
        )
        self.assertEqual(len(gate.calls), no_auth["expected_merge_calls"])

        authorized = cases["shipping_with_authorization"]
        gate = FakeSideEffectGate()
        self.assertEqual(
            shipping_status(
                authorized["verified"],
                fixture_authorization("merge") if authorized["authorized"] else None,
                gate,
            ),
            authorized["expected_status"],
        )
        self.assertEqual(len(gate.calls), authorized["expected_merge_calls"])

        self.assertEqual(
            pause_checkpoint(cases["pause"]["explicit_pause"]),
            cases["pause"]["expected_checkpoint"],
        )
        self.assertEqual(
            pause_checkpoint(cases["normal_continuation"]["explicit_pause"]),
            cases["normal_continuation"]["expected_checkpoint"],
        )

        for name in ("autopilot_without_go", "autopilot_with_go"):
            case = cases[name]
            self.assertEqual(
                start_autopilot(case["go"]),
                (case["expected_status"], case["expected_owner_starts"]),
            )
        perf = cases["perf_missing_surface"]
        self.assertEqual(
            perf_status(perf["surface_available"]),
            (perf["expected_status"], perf["expected_changes"]),
        )
        for case in cases["diagnostic_states"]:
            gate = FakeSideEffectGate()
            self.assertEqual(
                diagnostic_state(
                    case["workflow"],
                    case["capability"],
                    case["artifact"],
                    case["matches"],
                    case["authorized"],
                    gate,
                    capture_ready=case["capture_ready"],
                ),
                (case["expected"], 0),
                case["workflow"],
            )
            self.assertEqual(gate.calls, [], case["workflow"])
        handoff_contract = cases["pr_handoff_contract"]
        for name in ("pr_handoff_without_authorization", "pr_handoff_with_authorization"):
            case = cases[name]
            result = pr_handoff(
                case["change_retained"],
                case["authorized"],
                case["evidence"],
            )
            self.assertEqual(result["status"], case["expected_status"])
            self.assertEqual(
                result["local_handoff_ready"], case["expected_local_handoff_ready"]
            )
        for case in cases["pr_handoff_invalid_cases"]:
            result = pr_handoff(
                True,
                True,
                case["evidence"],
            )
            self.assertEqual(
                result,
                {"status": case["expected_status"], "local_handoff_ready": False},
                case["name"],
            )
        self.assertEqual(
            pr_handoff(
                True,
                False,
                {
                    "target": "pr:24",
                    "diff": "",
                    "head_sha": "abc",
                    "checks": "ok",
                    "body": "body",
                },
            ),
            {"status": "BLOCKED", "local_handoff_ready": False},
        )

        cleanup = cases["cleanup_dry_run"]
        proposals, deletions = dry_run_cleanup(cleanup["candidates"])
        self.assertEqual(proposals, cleanup["expected_proposals"])
        self.assertEqual(deletions, cleanup["expected_deletions"])

    def test_log_helper_sanitizes_fixture_cells_without_external_writes(self) -> None:
        helper = ROOT / "skills" / "show-me-your-work" / "scripts" / "log.sh"
        with tempfile.TemporaryDirectory(prefix="g1-audit-") as directory:
            logfile = Path(directory) / "decisions.tsv"
            import subprocess

            result = subprocess.run(
                [
                    "bash",
                    str(helper),
                    str(logfile),
                    "fixture",
                    "tabs\nand\tformula",
                    "local",
                    "=unsafe",
                    "PASS",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = logfile.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            self.assertNotIn("\t", rows[1].split("\t")[2])
            self.assertTrue(rows[1].split("\t")[4].startswith("'="))


if __name__ == "__main__":
    unittest.main()
