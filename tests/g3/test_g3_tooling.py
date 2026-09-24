from __future__ import annotations

import json
import importlib.util
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_ROOT = ROOT / "skills" / "poteto-mode" / "scripts"
LOG_ROOT = ROOT / "skills" / "show-me-your-work" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
if str(LOG_ROOT) not in sys.path:
    sys.path.insert(0, str(LOG_ROOT))

PACKAGE_NAME = "pstack_g3_helpers"
PACKAGE_SPEC = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    SCRIPT_ROOT / "g3" / "__init__.py",
    submodule_search_locations=[str(SCRIPT_ROOT / "g3")],
)
if PACKAGE_SPEC is None or PACKAGE_SPEC.loader is None:
    raise RuntimeError("could not load G3 helper package")
PACKAGE = importlib.util.module_from_spec(PACKAGE_SPEC)
sys.modules[PACKAGE_NAME] = PACKAGE
PACKAGE_SPEC.loader.exec_module(PACKAGE)

from pstack_g3_helpers.orch import (  # noqa: E402
    FrontierEntry,
    OrchestratorStore,
    StaticStackReader,
    StoreError,
    UnsupportedSideEffect,
)
from pstack_g3_helpers.plan_checker import (  # noqa: E402
    FileRule,
    PlanInputError,
    PlanProfile,
    check_plan,
    completion_profile,
    load_profile,
)
from pstack_g3_helpers.watch import (  # noqa: E402
    CheckRead,
    DeterministicClock,
    FakeReader,
    JsonProviderAdapter,
    PRContext,
    PRFacts,
    PollOptions,
    QueryFailure,
    ReviewThread,
    UnknownProviderData,
    failed_check,
    pending_check,
    read_snapshot,
    run_queued,
    run_simple,
)
from pstack_g3_helpers.worktree_audit import (  # noqa: E402
    AuditInputError,
    audit_worktrees,
    load_metadata,
)
from log import AuditLogError, append_row, sanitize_cell  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures"


class G3WatcherTests(unittest.TestCase):
    def test_ready_uses_recorded_adapter_and_deterministic_clock(self) -> None:
        reader = JsonProviderAdapter.from_file(FIXTURES / "provider-ready.json")
        clock = DeterministicClock()
        result = run_simple(reader, [PRContext("owner", "repo", 24)], clock=clock)
        self.assertEqual(result["kind"], "READY")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(reader.calls, ["pull_request:24", "review_threads:24", "checks:24", "commit_rollups:24"])

    def test_conflict_precedes_review_and_ci_blockers(self) -> None:
        context = PRContext("owner", "repo", 24)
        facts = PRFacts(context, mergeable="CONFLICTING", merge_state_status="CONFLICTING")
        reader = FakeReader(
            facts=facts,
            threads=(ReviewThread("thread-1", "needs work"),),
            checks=CheckRead("recorded", (failed_check("ci"),)),
        )
        result = run_simple(reader, [context], clock=DeterministicClock())
        self.assertEqual(result["kind"], "BLOCKER")
        self.assertEqual(result["blocker"]["kind"], "merge-conflicts")
        self.assertEqual(result["exit_code"], 2)

    def test_unknown_provider_enum_is_refused(self) -> None:
        reader = JsonProviderAdapter.from_file(FIXTURES / "provider-unknown.json")
        with self.assertRaises(UnknownProviderData):
            reader.pull_request(PRContext("owner", "repo", 24))

    def test_provider_fixture_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / "provider.json"
            link.symlink_to(FIXTURES / "provider-ready.json")
            with self.assertRaises(UnknownProviderData):
                JsonProviderAdapter.from_file(link)

    def test_provider_fixture_symlink_parent_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "link").symlink_to(FIXTURES, target_is_directory=True)
            with self.assertRaises(UnknownProviderData):
                JsonProviderAdapter.from_file(root / "link" / "provider-ready.json")

    def test_provider_incomplete_or_contradictory_facts_are_refused(self) -> None:
        payload = json.loads((FIXTURES / "provider-ready.json").read_text())
        for field in ("review_threads", "commit_rollups"):
            candidate = dict(payload)
            candidate.pop(field)
            with self.subTest(field=field), self.assertRaises(UnknownProviderData):
                read_snapshot(JsonProviderAdapter(candidate), PRContext("owner", "repo", 24))
        for field, value in (("review_decision", ""), ("head_ref_oid", None), ("merged_at", "2026-01-01T00:00:00Z")):
            candidate = json.loads(json.dumps(payload))
            candidate["pull_request"][field] = value
            with self.subTest(field=field), self.assertRaises(UnknownProviderData):
                read_snapshot(JsonProviderAdapter(candidate), PRContext("owner", "repo", 24))

    def test_queued_status_only_is_rejected_in_public_cli(self) -> None:
        from pstack_g3_helpers import watch

        with self.assertRaises(SystemExit):
            watch.main(
                [
                    "--fixture",
                    str(FIXTURES / "provider-ready.json"),
                    "--mode",
                    "queued-stack",
                    "--status-only",
                ]
            )

    def test_public_cli_renders_malformed_provider_as_json_error(self) -> None:
        from pstack_g3_helpers import watch

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = watch.main(["--fixture", str(FIXTURES / "provider-unknown.json")])
        self.assertEqual(result, 2)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["kind"], "ERROR")
        self.assertEqual(rendered["error"], "UnknownProviderData")

    def test_recorded_fast_path_falls_back_to_rollup_pages(self) -> None:
        payload = json.loads((FIXTURES / "provider-ready.json").read_text())
        payload["checks"] = {
            "fast_path": {"kind": "unusable", "exit_code": 1, "stderr": "offline"},
            "rollup_pages": [
                {"checks": [{"kind": "passed", "name": "rollup-ci"}]},
                {"checks": []},
            ],
        }
        reader = JsonProviderAdapter(payload)
        snapshot = read_snapshot(reader, PRContext("owner", "repo", 24))
        self.assertIsNotNone(snapshot.ci)
        self.assertEqual(snapshot.ci.source, "graphql-rollup")

    def test_pending_wait_uses_injected_sleep_and_times_out(self) -> None:
        context = PRContext("owner", "repo", 24)
        reader = FakeReader(checks=CheckRead("recorded", (pending_check("ci"),)))
        clock = DeterministicClock()
        result = run_simple(
            reader,
            [context],
            options=PollOptions(interval=2, timeout=5),
            clock=clock,
        )
        self.assertEqual(result["kind"], "TIMEOUT")
        self.assertEqual(clock.sleeps, [2, 2, 1])

    def test_timeout_rejects_non_finite_values_and_bounds_sleep(self) -> None:
        with self.assertRaises(ValueError):
            PollOptions(timeout=float("nan"))
        with self.assertRaises(ValueError):
            PollOptions(timeout=float("inf"))
        context = PRContext("owner", "repo", 24)
        reader = FakeReader(checks=CheckRead("recorded", (pending_check("ci"),)))
        clock = DeterministicClock()
        result = run_simple(
            reader,
            [context],
            options=PollOptions(interval=10, timeout=1),
            clock=clock,
        )
        self.assertEqual(result["kind"], "TIMEOUT")
        self.assertEqual(clock.sleeps, [1])

    def test_queued_complete_reads_state_without_merging(self) -> None:
        contexts = [PRContext("owner", "repo", 23), PRContext("owner", "repo", 24)]
        facts = {
            23: PRFacts(contexts[0], state="MERGED", merged_at="2026-01-01T00:00:00Z"),
            24: PRFacts(contexts[1], state="MERGED", merged_at="2026-01-01T00:01:00Z"),
        }
        reader = FakeReader(facts=facts)
        result = run_queued(reader, contexts, clock=DeterministicClock())
        self.assertEqual(result["kind"], "COMPLETE")
        self.assertNotIn("merge", " ".join(reader.calls))


class G3StoreTests(unittest.TestCase):
    def make_store(self, root: Path, **kwargs: object) -> OrchestratorStore:
        store = OrchestratorStore(root, pid=4100, pid_alive=lambda pid: pid != 9999, **kwargs)
        store.init()
        return store

    def test_units_ledger_gates_frontier_and_status_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reader = StaticStackReader(
                (
                    FrontierEntry(23, "feature/23", "a" * 40, "MERGED"),
                    FrontierEntry(24, "feature/24", "b" * 40, "OPEN"),
                )
            )
            store = self.make_store(root, stack_reader=reader)
            try:
                store.units_add("unit-1", "watch", "=unsafe")
                store.units_set("unit-1", "verified", branch="feature/24", pr=24, sha="b" * 40)
                store.ledger_record(24, "b" * 40, "unit-test-verified", "artifacts/test.txt", "offline")
                store.gate_park("gate-1", "Choose", "a or b", "a")
                frontier = store.frontier_set(root, prs=[23, 24])
                report = store.status_render()
                self.assertEqual(frontier.lowest_unmerged, 24)
                self.assertEqual(store.units_counts(), {"verified": 1})
                self.assertEqual(store.ledger_summary(), {"unit-test-verified": 1})
                self.assertEqual(report.summary["open_gate_ids"], ["gate-1"])
                self.assertIn("'=unsafe", (root / "units.tsv").read_text())
            finally:
                store.close()

    def test_stale_pid_lock_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".orch.lock").write_text("9999\n")
            store = self.make_store(root)
            try:
                self.assertEqual((root / ".orch.lock").read_text(), "4100\n")
            finally:
                store.close()

    def test_active_lock_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".orch.lock").write_text("4101\n")
            blocked = OrchestratorStore(root, pid=4100, pid_alive=lambda pid: True)
            with self.assertRaises(StoreError):
                blocked.init()
            forced = OrchestratorStore(root, pid=4100, pid_alive=lambda pid: True, force=True)
            forced.init()
            forced.close()

    def test_inbox_drain_is_atomic_and_retains_recovery_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(Path(temporary))
            try:
                store.inbox_push("agent-a", "unit-a", "done", "reports/a.txt")
                drained = store.inbox_drain()
                self.assertEqual(len(drained), 1)
                self.assertEqual(store.inbox_count(), 0)
                self.assertTrue(list(store.root.glob(".inbox-drain-*/")))
            finally:
                store.close()

    def test_store_inbox_symlink_is_refused_for_init(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "outside"
            target.mkdir()
            (root / "inbox").symlink_to(target, target_is_directory=True)
            store = OrchestratorStore(root, pid=4100, pid_alive=lambda pid: False)
            with self.assertRaises(StoreError):
                store.init()
            store.close()

    def test_store_root_symlink_parent_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(StoreError):
                OrchestratorStore(link / "nested")

    def test_malformed_tsv_and_unsupported_side_effect_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self.make_store(root)
            try:
                (root / "units.tsv").write_text("bad\n")
                with self.assertRaises(StoreError):
                    store.units_list()
                with self.assertRaises(UnsupportedSideEffect):
                    store.refuse_side_effect("merge")
            finally:
                store.close()


class G3PlanAndAuditTests(unittest.TestCase):
    def test_current_completion_profile_passes_without_upstream_literals(self) -> None:
        report = check_plan(ROOT / "plans" / "completion", completion_profile())
        self.assertTrue(report.ok, report.as_dict())
        self.assertNotIn("grok", json.dumps(report.as_dict()).lower())

    def test_profile_failure_is_driven_by_supplied_markers_and_boxes(self) -> None:
        profile = load_profile(FIXTURES / "fixture-profile.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan.md").write_text("# Fixture plan\n\n- [ ] incomplete\n")
            report = check_plan(root, profile)
            messages = " ".join(issue.message for issue in report.issues)
            self.assertFalse(report.ok)
            self.assertIn("Local marker", messages)
            self.assertIn("unchecked box", messages)

    def test_nested_plan_symlink_is_not_followed(self) -> None:
        profile = PlanProfile(
            "nested-link",
            ("linked/plan.md",),
            (FileRule("linked/plan.md", required_markers=("ok",)),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (target / "plan.md").write_text("# Plan\n\nok\n")
            (root / "linked").symlink_to(target, target_is_directory=True)
            report = check_plan(root, profile)
            self.assertFalse(report.ok)
            self.assertIn("required file pattern has no matches", report.issues[0].message)

    def test_plan_root_symlink_parent_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(PlanInputError):
                check_plan(link / "nested", completion_profile())


class G3WorktreeAndLogTests(unittest.TestCase):
    def test_worktree_audit_reports_unknown_activity_without_fetch_or_scan(self) -> None:
        metadata = load_metadata(FIXTURES / "worktrees.json")
        report = audit_worktrees(metadata, as_of="2026-01-10T00:00:00Z")
        self.assertFalse(report.fetch_attempted)
        self.assertFalse(report.transcript_scan_attempted)
        self.assertFalse(report.deletion_performed)
        rows = {row.path: row for row in report.rows}
        self.assertNotIn("/fixtures/repo", rows)
        self.assertEqual(rows["/fixtures/repo-g3"].activity, "unknown")
        self.assertIn("activity", rows["/fixtures/repo-g3"].unknowns)
        self.assertEqual(rows["/fixtures/repo-g3"].bucket, "hold-wip")
        self.assertEqual(rows["/fixtures/repo-old"].bucket, "unknown-activity")

    def test_metadata_symlink_parent_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "link").symlink_to(FIXTURES, target_is_directory=True)
            with self.assertRaises(AuditInputError):
                load_metadata(root / "link" / "worktrees.json")

    def test_log_appends_utc_rows_and_sanitizes_controls_and_formulas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".audit" / "g3.tsv"
            fixed = lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            append_row(path, "phase\t1", "=decision", "why\nnext", "evidence/proof.txt", "@open", clock=fixed)
            append_row(path, "phase-2", "ok", "safe", "evidence/second.txt", "verified", clock=fixed)
            lines = path.read_text().splitlines()
            self.assertEqual(lines[0], "ts\tphase\tdecision\twhy\tevidence\tresult")
            self.assertEqual(lines[1].split("\t"), [
                "2026-01-02T03:04:05Z",
                "phase 1",
                "'=decision",
                "why next",
                "evidence/proof.txt",
                "'@open",
            ])
            self.assertTrue(all(len(line.split("\t")) == 6 for line in lines))

    def test_log_symlink_parent_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (root / "link").symlink_to(target, target_is_directory=True)
            with self.assertRaises(AuditLogError):
                append_row(root / "link" / "audit.tsv", "phase", "ok", "why", "proof", "PASS")

    def test_log_nested_symlink_parent_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (root / "link").symlink_to(target, target_is_directory=True)
            with self.assertRaises(AuditLogError):
                append_row(
                    root / "link" / "nested" / "audit.tsv",
                    "phase",
                    "ok",
                    "why",
                    "proof",
                    "PASS",
                )

    def test_log_sanitizes_controls_and_rejects_invalid_existing_shape(self) -> None:
        self.assertEqual(sanitize_cell("a\x00\x1b\u2028b"), "a   b")
        self.assertEqual(sanitize_cell(" =1"), "' =1")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.tsv"
            path.write_text("not the expected header\n", encoding="utf-8")
            with self.assertRaises(AuditLogError):
                append_row(path, "phase", "ok", "why", "proof", "PASS")


if __name__ == "__main__":
    unittest.main()
