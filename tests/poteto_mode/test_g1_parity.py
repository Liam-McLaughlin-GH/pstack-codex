#!/usr/bin/env python3
"""Offline acceptance checks for G1 playbook and workflow parity."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_DIR = ROOT / "skills" / "poteto-mode" / "playbooks"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

PLAYBOOK_NAMES = {
    "authoring-a-skill.md",
    "autonomous-run.md",
    "autopilot-full.md",
    "autopilot-stack.md",
    "babysit.md",
    "bug-fix.md",
    "eval.md",
    "feature.md",
    "hillclimb.md",
    "investigation.md",
    "multi-phase-plan.md",
    "opening-a-pr.md",
    "orchestrate.md",
    "pause-safely.md",
    "perf-issue.md",
    "prototype.md",
    "refactoring.md",
    "runtime-forensics.md",
    "session-pickup.md",
    "shipping.md",
    "trace-forensics.md",
    "visual-parity.md",
    "worktree-cleanup.md",
}

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


class FakeSideEffectGate:
    """Fixture-only adapter that makes authorization observable."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, operation: str, *, authorized: bool) -> bool:
        if not authorized:
            return False
        self.calls.append(operation)
        return True


def run_autonomous(outcomes: list[str]) -> tuple[str, int]:
    for iteration, outcome in enumerate(outcomes, start=1):
        if outcome == "predicate-met":
            return "PASS", iteration
    return "BLOCKED", len(outcomes)


def shipping_status(verified: bool, authorized: bool, gate: FakeSideEffectGate) -> str:
    if not verified:
        return "BLOCKED"
    if not gate.request("merge", authorized=authorized):
        return "NOT RUN"
    return "PASS"


def pause_checkpoint(explicit_pause: bool) -> bool:
    return explicit_pause


def dry_run_cleanup(candidates: list[dict[str, object]]) -> tuple[list[str], int]:
    proposals = [
        str(candidate["path"])
        for candidate in candidates
        if candidate["merged"] and not candidate["dirty"] and not candidate["active"]
    ]
    return proposals, 0


class G1ParityTests(unittest.TestCase):
    def test_inventory_matches_the_23_upstream_playbook_names(self) -> None:
        actual = {path.name for path in PLAYBOOK_DIR.glob("*.md")}
        self.assertEqual(actual, PLAYBOOK_NAMES)
        self.assertEqual(len(actual), 23)

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
        self.assertFalse(gate.request("merge", authorized=False))
        self.assertFalse(gate.request("deploy", authorized=False))
        self.assertEqual(gate.calls, [])

        self.assertTrue(gate.request("merge", authorized=True))
        self.assertEqual(gate.calls, ["merge"])

    def test_contract_fixture_checks_the_actual_playbooks(self) -> None:
        contracts = contract_data()
        self.assertEqual(set(contracts), PLAYBOOK_NAMES)
        self.assertEqual(len(contracts), 23)
        for filename, markers in contracts.items():
            text = (PLAYBOOK_DIR / filename).read_text(encoding="utf-8")
            for marker in markers:
                self.assertIn(marker, text, f"{filename} is missing {marker!r}")
            self.assertIn("UNSUPPORTED-SEAM:", text, filename)

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
            shipping_status(no_auth["verified"], no_auth["authorized"], gate),
            no_auth["expected_status"],
        )
        self.assertEqual(len(gate.calls), no_auth["expected_merge_calls"])

        authorized = cases["shipping_with_authorization"]
        gate = FakeSideEffectGate()
        self.assertEqual(
            shipping_status(authorized["verified"], authorized["authorized"], gate),
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
