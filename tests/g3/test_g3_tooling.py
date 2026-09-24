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
    LEDGER_HEADER,
    OrchestratorStore,
    StaticStackReader,
    StoreError,
    UNIT_HEADER,
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
    Check,
    CheckRead,
    CommitRollup,
    DeterministicClock,
    FakeReader,
    JsonProviderAdapter,
    PRContext,
    PRFacts,
    PollOptions,
    QueryFailure,
    ReviewThread,
    UnknownProviderData,
    classify_pr,
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

    def test_open_pr_requires_an_approved_review_decision(self) -> None:
        context = PRContext("owner", "repo", 24)
        for decision in (None, "REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            with self.subTest(decision=decision):
                reader = FakeReader(
                    facts=PRFacts(context, review_decision=decision),
                    commits=(CommitRollup("head", "SUCCESS"),),
                )
                result = run_simple(reader, [context], clock=DeterministicClock())
                self.assertEqual(result["kind"], "BLOCKER")
                self.assertEqual(result["blocker"]["kind"], "merge-gate")
                self.assertNotEqual(result["kind"], "READY")

    def test_code_review_gate_non_success_states_are_not_ready(self) -> None:
        context = PRContext("owner", "repo", 24)
        for state in ("PENDING", "FAILURE", "ERROR"):
            with self.subTest(state=state):
                reader = FakeReader(
                    checks=CheckRead("recorded", (Check("code-review-gate", "review", state),)),
                    commits=(CommitRollup("head", "SUCCESS"),),
                )
                if state == "PENDING":
                    snapshot = read_snapshot(reader, context)
                    self.assertEqual(snapshot.ci.kind, "ci-pending")
                    result = run_simple(
                        reader,
                        [context],
                        options=PollOptions(timeout=1, interval=1),
                        clock=DeterministicClock(),
                    )
                    self.assertEqual(result["kind"], "TIMEOUT")
                else:
                    result = run_simple(reader, [context], clock=DeterministicClock())
                    self.assertEqual(result["kind"], "BLOCKER")
                    self.assertEqual(result["blocker"]["kind"], "failing-checks")

    def test_conflict_precedes_review_and_ci_blockers(self) -> None:
        context = PRContext("owner", "repo", 24)
        facts = PRFacts(context, mergeable="CONFLICTING", merge_state_status="CONFLICTING")
        reader = FakeReader(
            facts=facts,
            threads=(ReviewThread("thread-1", "needs work"),),
            checks=CheckRead("recorded", (failed_check("ci"),)),
            commits=(CommitRollup("head", "SUCCESS"),),
        )
        result = run_simple(reader, [context], clock=DeterministicClock())
        self.assertEqual(result["kind"], "BLOCKER")
        self.assertEqual(result["blocker"]["kind"], "merge-conflicts")
        self.assertEqual(result["exit_code"], 2)

    def test_unknown_provider_enum_is_refused(self) -> None:
        reader = JsonProviderAdapter.from_file(FIXTURES / "provider-unknown.json")
        with self.assertRaises(UnknownProviderData):
            reader.pull_request(PRContext("owner", "repo", 24))

    def test_unknown_merge_conditions_fail_closed(self) -> None:
        context = PRContext("owner", "repo", 24)
        for field in ("mergeable", "merge_state_status"):
            payload = json.loads((FIXTURES / "provider-ready.json").read_text())
            payload["pull_request"][field] = "UNKNOWN"
            with self.subTest(field=field), self.assertRaises(UnknownProviderData):
                read_snapshot(JsonProviderAdapter(payload), context)

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

    def test_merged_provider_timestamp_is_validated(self) -> None:
        payload = json.loads((FIXTURES / "provider-ready.json").read_text())
        payload["pull_request"]["state"] = "MERGED"
        payload["pull_request"]["merged_at"] = "not-a-timestamp"
        with self.assertRaises(UnknownProviderData):
            JsonProviderAdapter(payload).pull_request(PRContext("owner", "repo", 24))

    def test_closed_provider_cannot_claim_a_merged_timestamp(self) -> None:
        payload = json.loads((FIXTURES / "provider-ready.json").read_text())
        payload["pull_request"]["state"] = "CLOSED"
        payload["pull_request"]["merged_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(UnknownProviderData):
            JsonProviderAdapter(payload).pull_request(PRContext("owner", "repo", 24))

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

    def test_public_cli_normalizes_hostile_provider_enum_shapes(self) -> None:
        from pstack_g3_helpers import watch

        payload = json.loads((FIXTURES / "provider-ready.json").read_text())
        payload["checks"]["checks"][0]["kind"] = []
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "hostile.json"
            fixture.write_text(json.dumps(payload), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = watch.main(["--fixture", str(fixture)])
        self.assertEqual(result, 2)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["error"], "UnknownProviderData")

    def test_public_cli_normalizes_value_errors_as_json_domain_errors(self) -> None:
        from pstack_g3_helpers import watch

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = watch.main(
                ["--fixture", str(FIXTURES / "provider-ready.json"), "--timeout", "nan"]
            )
        self.assertEqual(result, 2)
        rendered = json.loads(output.getvalue())
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
        reader = FakeReader(
            checks=CheckRead("recorded", (pending_check("ci"),)),
            commits=(CommitRollup("head", "SUCCESS"),),
        )
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
        reader = FakeReader(
            checks=CheckRead("recorded", (pending_check("ci"),)),
            commits=(CommitRollup("head", "SUCCESS"),),
        )
        clock = DeterministicClock()
        result = run_simple(
            reader,
            [context],
            options=PollOptions(interval=10, timeout=1),
            clock=clock,
        )
        self.assertEqual(result["kind"], "TIMEOUT")
        self.assertEqual(clock.sleeps, [1])

    def test_retry_timeout_does_not_read_after_deadline(self) -> None:
        context = PRContext("owner", "repo", 24)

        class RetryReader(FakeReader):
            def __init__(self) -> None:
                super().__init__(commits=(CommitRollup("head", "SUCCESS"),))
                self.attempts = 0

            def pull_request(self, context: PRContext) -> PRFacts:
                self.attempts += 1
                if self.attempts == 1:
                    raise QueryFailure("temporary", retryable=True)
                return super().pull_request(context)

        reader = RetryReader()
        clock = DeterministicClock()
        result = run_simple(
            reader,
            [context],
            options=PollOptions(interval=2, timeout=1),
            clock=clock,
        )
        self.assertEqual(result["kind"], "TIMEOUT")
        self.assertEqual(reader.attempts, 1)
        self.assertEqual(clock.sleeps, [1])

    def test_query_failure_after_deadline_is_timeout_even_when_terminal(self) -> None:
        context = PRContext("owner", "repo", 24)
        for retryable, max_query_errors in ((False, 5), (True, 1)):
            with self.subTest(retryable=retryable, max_query_errors=max_query_errors):
                clock = DeterministicClock()

                class LateFailureReader(FakeReader):
                    def pull_request(self, context: PRContext) -> PRFacts:
                        del context
                        clock.elapsed = 2
                        raise QueryFailure("provider failed", retryable=retryable)

                result = run_simple(
                    LateFailureReader(),
                    [context],
                    options=PollOptions(timeout=1, max_query_errors=max_query_errors),
                    clock=clock,
                )
                self.assertEqual(result["kind"], "TIMEOUT")
                self.assertEqual(result["exit_code"], 5)

    def test_simple_poll_does_not_read_or_return_ready_after_deadline(self) -> None:
        context = PRContext("owner", "repo", 24)

        class ChangingReader(FakeReader):
            def __init__(self) -> None:
                super().__init__(commits=(CommitRollup("head", "SUCCESS"),))
                self.check_reads = 0

            def checks(self, context: PRContext) -> CheckRead:
                self.check_reads += 1
                return CheckRead(
                    "recorded",
                    (pending_check("ci"),)
                    if self.check_reads == 1
                    else (Check("passed", "ci", "SUCCESS"),),
                )

        reader = ChangingReader()
        result = run_simple(
            reader,
            [context],
            options=PollOptions(interval=10, timeout=1),
            clock=DeterministicClock(),
        )
        self.assertEqual(result["kind"], "TIMEOUT")
        self.assertEqual(reader.check_reads, 1)

    def test_queued_poll_does_not_read_or_complete_after_deadline(self) -> None:
        context = PRContext("owner", "repo", 24)

        class ChangingReader(FakeReader):
            def __init__(self) -> None:
                super().__init__()
                self.check_reads = 0

            def checks(self, context: PRContext) -> CheckRead:
                self.check_reads += 1
                return CheckRead(
                    "recorded",
                    (pending_check("ci"),)
                    if self.check_reads == 1
                    else (Check("passed", "ci", "SUCCESS"),),
                )

        reader = ChangingReader()
        result = run_queued(
            reader,
            [context],
            options=PollOptions(interval=10, timeout=1),
            clock=DeterministicClock(),
        )
        self.assertEqual(result["kind"], "TIMEOUT")
        self.assertEqual(reader.check_reads, 1)

    def test_queued_rejects_duplicate_identity_and_preserves_full_keys(self) -> None:
        duplicate = PRContext("owner", "repo", 24)
        with self.assertRaises(ValueError):
            run_queued(FakeReader(), [duplicate, duplicate], clock=DeterministicClock())
        first = PRContext("first", "repo", 24)
        second = PRContext("second", "repo", 24)
        reader = FakeReader(
            facts={
                24: PRFacts(first, state="MERGED", merged_at="2026-01-01T00:00:00Z"),
            }
        )
        result = run_queued(reader, [first, second], clock=DeterministicClock())
        self.assertEqual(result["kind"], "COMPLETE")
        self.assertEqual(
            [(row["owner"], row["repo"], row["number"]) for row in result["queue"]],
            [("first", "repo", 24), ("second", "repo", 24)],
        )
        self.assertEqual(len(result["merged"]), 2)

    def test_reader_identity_collision_is_refused(self) -> None:
        requested = PRContext("owner", "repo", 24)
        other = PRContext("other", "repo", 24)

        class CollisionReader(FakeReader):
            def pull_request(self, context: PRContext) -> PRFacts:
                del context
                return PRFacts(other)

        with self.assertRaises(UnknownProviderData):
            run_simple(CollisionReader(), [requested], clock=DeterministicClock())

    def test_max_query_errors_requires_a_finite_positive_integer(self) -> None:
        for value in (0, -1, True, 1.0, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PollOptions(max_query_errors=value)

    def test_poll_durations_reject_booleans(self) -> None:
        for field in ("interval", "sweep_interval", "timeout"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                PollOptions(**{field: True})

    def test_provider_rollups_and_check_states_fail_closed(self) -> None:
        payload = json.loads((FIXTURES / "provider-ready.json").read_text())
        payload["pull_request"]["merge_state_status"] = "BLOCKED"
        payload["checks"]["checks"][0]["reported_state"] = "FAILURE"
        with self.assertRaises(UnknownProviderData):
            read_snapshot(JsonProviderAdapter(payload), PRContext("owner", "repo", 24))
        payload = json.loads((FIXTURES / "provider-ready.json").read_text())
        payload["commit_rollups"] = [
            {"oid": "other", "state": "SUCCESS"},
        ]
        with self.assertRaises(UnknownProviderData):
            read_snapshot(JsonProviderAdapter(payload), PRContext("owner", "repo", 24))
        payload["commit_rollups"].append({"oid": "head", "state": "SUCCESS"})
        payload["commit_rollups"].append({"oid": "head", "state": "FAILURE"})
        with self.assertRaises(UnknownProviderData):
            read_snapshot(JsonProviderAdapter(payload), PRContext("owner", "repo", 24))

    def test_non_head_rollup_without_state_is_rejected(self) -> None:
        payload = json.loads((FIXTURES / "provider-ready.json").read_text())
        payload["commit_rollups"] = [
            {"oid": "head", "state": "SUCCESS"},
            {"oid": "old"},
        ]
        with self.assertRaises(UnknownProviderData):
            read_snapshot(JsonProviderAdapter(payload), PRContext("owner", "repo", 24))

    def test_check_state_invariant_is_shared_by_reader_seams(self) -> None:
        with self.assertRaises(UnknownProviderData):
            Check("passed", "ci", "FAILURE")
        check = Check("passed", "ci", "SUCCESS")
        object.__setattr__(check, "reported_state", "FAILURE")
        reader = FakeReader(checks=CheckRead("recorded", (check,)))
        with self.assertRaises(UnknownProviderData):
            read_snapshot(reader, PRContext("owner", "repo", 24))

    def test_reader_seams_revalidate_all_typed_records(self) -> None:
        context = PRContext("owner", "repo", 24)

        class StaticReader:
            def __init__(self, facts: PRFacts, thread: ReviewThread, check_read: CheckRead, commit: CommitRollup) -> None:
                self.facts = facts
                self.thread = thread
                self.check_read = check_read
                self.commit = commit

            def pull_request(self, context: PRContext) -> PRFacts:
                del context
                return self.facts

            def review_threads(self, context: PRContext) -> tuple[ReviewThread, ...]:
                del context
                return (self.thread,)

            def checks(self, context: PRContext) -> CheckRead:
                del context
                return self.check_read

            def commit_rollups(self, context: PRContext) -> tuple[CommitRollup, ...]:
                del context
                return (self.commit,)

        mutations = {
            "PR state": lambda facts, thread, check_read, commit: object.__setattr__(facts, "state", "ALIEN"),
            "merged state": lambda facts, thread, check_read, commit: object.__setattr__(facts, "state", "MERGED"),
            "facts context owner": lambda facts, thread, check_read, commit: (
                object.__setattr__(facts, "context", PRContext("owner", "repo", 24)),
                object.__setattr__(facts.context, "owner", []),
            ),
            "review decision": lambda facts, thread, check_read, commit: object.__setattr__(facts, "review_decision", "ALIEN"),
            "draft flag": lambda facts, thread, check_read, commit: object.__setattr__(facts, "is_draft", []),
            "head ref oid": lambda facts, thread, check_read, commit: object.__setattr__(facts, "head_ref_oid", []),
            "head ref name": lambda facts, thread, check_read, commit: object.__setattr__(facts, "head_ref_name", ""),
            "base ref name": lambda facts, thread, check_read, commit: object.__setattr__(facts, "base_ref_name", []),
            "merged timestamp": lambda facts, thread, check_read, commit: object.__setattr__(facts, "merged_at", "not-a-timestamp"),
            "review thread": lambda facts, thread, check_read, commit: object.__setattr__(thread, "id", ""),
            "check source": lambda facts, thread, check_read, commit: object.__setattr__(check_read, "source", []),
            "current head oid": lambda facts, thread, check_read, commit: object.__setattr__(commit, "oid", []),
            "current head state": lambda facts, thread, check_read, commit: object.__setattr__(commit, "state", "ALIEN"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                facts = PRFacts(context)
                thread = ReviewThread("thread-1")
                check_read = CheckRead("recorded", (Check("passed", "ci", "SUCCESS"),))
                commit = CommitRollup("head", "SUCCESS")
                mutate(facts, thread, check_read, commit)
                with self.assertRaises(UnknownProviderData):
                    read_snapshot(StaticReader(facts, thread, check_read, commit), context)

    def test_classification_revalidates_mutated_snapshot_records(self) -> None:
        context = PRContext("owner", "repo", 24)
        snapshot = read_snapshot(
            FakeReader(commits=(CommitRollup("head", "SUCCESS"),)),
            context,
        )
        object.__setattr__(snapshot.facts, "state", "MERGED")
        with self.assertRaises(UnknownProviderData):
            classify_pr(snapshot)

    def test_queue_rejects_unhashable_mutated_identity_fields(self) -> None:
        for field in ("owner", "repo", "number"):
            with self.subTest(field=field):
                context = PRContext("owner", "repo", 24)
                object.__setattr__(context, field, [])
                with self.assertRaises(UnknownProviderData):
                    run_queued(FakeReader(), [context], clock=DeterministicClock())

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

    def test_units_and_inbox_writes_reject_unsafe_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(Path(temporary))
            try:
                for value in ("bad\x00brief", "bad\u2028brief"):
                    with self.subTest(value=repr(value)), self.assertRaises(StoreError):
                        store.units_add("unit-1", "track", value)
                store.units_add("unit-1", "track", "safe")
                with self.assertRaises(StoreError):
                    store.units_set("unit-1", "verified", branch="bad\x00branch")
                with self.assertRaises(StoreError):
                    store.inbox_push("agent", "unit", "done", "bad\x00report")
                with self.assertRaises(StoreError):
                    store.ledger_record(1, "sha", "unit-test-verified", "evidence", "bad\x00verifier")
                with self.assertRaises(StoreError):
                    store.gate_park("gate", "bad\x00question", "options", "default")
                with self.assertRaises(StoreError):
                    store.standing_add("bad\x00standing")
                with self.assertRaises(StoreError):
                    store.gate_park("gate-newline", "bad\nquestion", "options", "default")
                with self.assertRaises(StoreError):
                    store.standing_add("bad\tstanding")
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

    def test_frontier_repo_symlink_parent_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (target / "repo").mkdir()
            (root / "link").symlink_to(target, target_is_directory=True)
            reader = StaticStackReader((FrontierEntry(23, "feature/23", "a" * 40, "MERGED"),))
            store = self.make_store(root / "store", stack_reader=reader)
            try:
                with self.assertRaises(StoreError):
                    store.frontier_set(root / "link" / "repo")
            finally:
                store.close()

    def test_frontier_reader_revalidates_entries_before_identity_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            store = self.make_store(root / "store")
            try:
                for field in ("pr", "branches", "sha", "state"):
                    entry = FrontierEntry(23, "feature/23", "a" * 40, "MERGED")
                    object.__setattr__(entry, field, [])
                    store.stack_reader = StaticStackReader((entry,))
                    with self.subTest(field=field), self.assertRaises(StoreError):
                        store.frontier_set(repo)
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

    def test_store_init_rejects_every_preexisting_file_symlink_and_releases_lock(self) -> None:
        for filename in ("units.tsv", "ledger.tsv", "gates.md", "preferences.md", "frontier.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "store"
                root.mkdir()
                outside = Path(temporary) / "outside"
                outside.write_text("outside\n")
                (root / filename).symlink_to(outside)
                store = OrchestratorStore(root, pid=4100, pid_alive=lambda pid: False)
                with self.assertRaises(StoreError):
                    store.init()
                self.assertFalse(store._locked)
                self.assertFalse((root / ".orch.lock").exists())
                store.close()

    def test_store_rejects_duplicate_unit_and_ledger_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self.make_store(root)
            try:
                (root / "units.tsv").write_text(
                    f"{UNIT_HEADER}\nunit\ttrack\tpending\t\t\t\tbrief\n"
                    "unit\tother\tverified\t\t\t\tother\n",
                    encoding="utf-8",
                )
                with self.assertRaises(StoreError):
                    store.units_counts()
                (root / "ledger.tsv").write_text(
                    f"{LEDGER_HEADER}\n24\tsha\tunit-test-verified\tevidence\toffline\t2026-01-01T00:00:00Z\n"
                    "24\tsha\tunit-test-verified\tevidence-2\toffline\t2026-01-02T00:00:00Z\n",
                    encoding="utf-8",
                )
                with self.assertRaises(StoreError):
                    store.ledger_summary()
            finally:
                store.close()

    def test_store_rejects_semantically_malformed_unit_rows(self) -> None:
        invalid_rows = (
            "\ttrack\tpending\t\t\t\tbrief",
            "unit\t\tpending\t\t\t\tbrief",
            "unit\ttrack\tBOGUS\t\t\t\tbrief",
            "unit\ttrack\tpending\tbranch\t0\tsha\tbrief",
            "unit\ttrack\tpending\tbranch\t24\t \tbrief",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self.make_store(root)
            try:
                for row in invalid_rows:
                    with self.subTest(row=row):
                        (root / "units.tsv").write_text(f"{UNIT_HEADER}\n{row}\n", encoding="utf-8")
                        with self.assertRaises(StoreError):
                            store.units_counts()
            finally:
                store.close()

    def test_store_rejects_blank_units_and_ledger_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self.make_store(root)
            try:
                (root / "units.tsv").write_text(f"{UNIT_HEADER}\n\n", encoding="utf-8")
                with self.assertRaises(StoreError):
                    store.units_counts()
                (root / "ledger.tsv").write_text(f"{LEDGER_HEADER}\n\n", encoding="utf-8")
                with self.assertRaises(StoreError):
                    store.ledger_summary()
            finally:
                store.close()

    def test_store_rejects_semantically_malformed_ledger_rows(self) -> None:
        invalid_rows = (
            "0\tsha\tunit-test-verified\tevidence\tverifier\t2026-01-01T00:00:00Z",
            "24\t\tunit-test-verified\tevidence\tverifier\t2026-01-01T00:00:00Z",
            "24\tsha\tBOGUS\tevidence\tverifier\t2026-01-01T00:00:00Z",
            "24\tsha\tunit-test-verified\t\tverifier\t2026-01-01T00:00:00Z",
            "24\t=sha\tunit-test-verified\tevidence\tverifier\t2026-01-01T00:00:00Z",
            "24\tsha\tunit-test-verified\tevidence\tverifier\tbad-ts",
            "24\tsha\tunit-test-verified\tevidence\tverifier\t2026-01-01T01:00:00+01:00",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self.make_store(root)
            try:
                for row in invalid_rows:
                    with self.subTest(row=row):
                        (root / "ledger.tsv").write_text(f"{LEDGER_HEADER}\n{row}\n", encoding="utf-8")
                        with self.assertRaises(StoreError):
                            store.ledger_summary()
            finally:
                store.close()

    def test_store_rejects_malformed_inbox_rows_and_preserves_pending_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self.make_store(root)
            try:
                pending, filename = store.inbox_push("agent-a", "unit-a", "pending", "report.txt")
                self.assertEqual(store.inbox_peek(), [pending])
                valid_timestamp = "2026-01-01T00:00:00Z"
                invalid_rows = (
                    "",
                    "\n",
                    f"{valid_timestamp}\tagent\tunit\tstatus",
                    f"{valid_timestamp}\tagent\tunit\tstatus\textra\textra\n",
                    f"{valid_timestamp}\tagent\x00\tunit\tstatus\treport\n",
                    f"\tagent\tunit\tstatus\treport\n",
                    f"{valid_timestamp}\t\tunit\tstatus\treport\n",
                    f"{valid_timestamp}\tagent\t\tstatus\treport\n",
                    f"{valid_timestamp}\tagent\tunit\t\treport\n",
                    f"not-a-timestamp\tagent\tunit\tstatus\treport\n",
                    f"2026-01-01T01:00:00+01:00\tagent\tunit\tstatus\treport\n",
                )
                bad_path = root / "inbox" / "bad.tsv"
                for row in invalid_rows:
                    with self.subTest(row=repr(row)):
                        bad_path.write_text(row, encoding="utf-8")
                        with self.assertRaises(StoreError):
                            store.inbox_peek()
                        with self.assertRaises(StoreError):
                            store.inbox_drain()
                        self.assertTrue(bad_path.exists())
                bad_path.unlink()
                self.assertEqual(store.inbox_peek(), [pending])
                self.assertEqual(store.inbox_drain(), [pending])
                self.assertFalse((root / "inbox" / filename).exists())
            finally:
                store.close()

    def test_store_normalizes_malformed_utf8_before_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self.make_store(root)
            try:
                (root / "units.tsv").write_bytes(UNIT_HEADER.encode("utf-8") + b"\nunit\ttrack\tpending\t\t\t\t\xff\n")
                with self.assertRaises(StoreError):
                    store.units_counts()
            finally:
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

    def test_profile_symlink_parent_is_refused(self) -> None:
        profile = {"name": "fixture", "required_files": [], "rules": []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (target / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
            (root / "link").symlink_to(target, target_is_directory=True)
            with self.assertRaises(PlanInputError):
                load_profile(root / "link" / "profile.json")

    def test_profile_normalizes_malformed_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            path.write_bytes(b"{\xff")
            with self.assertRaises(PlanInputError):
                load_profile(path)


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

    def test_metadata_rejects_duplicate_paths_and_invalid_main_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.json"
            duplicate = {
                "main_worktree": "/repo",
                "worktrees": [{"path": "/repo"}, {"path": "/repo"}],
            }
            path.write_text(json.dumps(duplicate), encoding="utf-8")
            with self.assertRaises(AuditInputError):
                load_metadata(path)
            for value in ([], {"path": "/repo"}, 24):
                malformed = {"main_worktree": value, "worktrees": []}
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(AuditInputError):
                    load_metadata(path)

    def test_metadata_normalizes_malformed_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.json"
            path.write_bytes(b"{\xff")
            with self.assertRaises(AuditInputError):
                load_metadata(path)

    def test_metadata_hostile_enum_shapes_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.json"
            payload = {"worktrees": [{"path": "/repo", "dirty": []}]}
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(AuditInputError):
                load_metadata(path)
            payload["worktrees"][0]["dirty"] = "clean"
            payload["worktrees"][0]["activity"] = {"status": []}
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(AuditInputError):
                load_metadata(path)

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
            path.write_text(
                "ts\tphase\tdecision\twhy\tevidence\tresult\n"
                "2026-01-01T00:00:00Z\tp\tok\twhy\tproof\tbad\x00\n",
                encoding="utf-8",
            )
            with self.assertRaises(AuditLogError):
                append_row(path, "phase", "ok", "why", "proof", "PASS")
            invalid = root = Path(temporary) / "invalid.tsv"
            with self.assertRaises(AuditLogError):
                append_row(invalid, None, "ok", "why", "proof", "PASS")
            self.assertFalse(invalid.exists())
            with self.assertRaises(AuditLogError):
                append_row(invalid, "phase", "ok", "why", None, "PASS")
            self.assertFalse(invalid.exists())

    def test_log_rejects_existing_row_without_terminal_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.tsv"
            original = (
                "ts\tphase\tdecision\twhy\tevidence\tresult\n"
                "2026-01-01T00:00:00Z\tp\tok\twhy\tproof\tPASS"
            )
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(AuditLogError):
                append_row(path, "phase-2", "ok", "why", "proof-2", "PASS")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_log_rejects_raw_splitlines_controls_and_invalid_timestamps(self) -> None:
        controls = ("\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for control in controls:
                path = root / f"control-{ord(control):x}.tsv"
                path.write_text(
                    "ts\tphase\tdecision\twhy\tevidence\tresult\n"
                    f"2026-01-01T00:00:00Z\tp\tok\twhy\tproof\tPASS{control}\n",
                    encoding="utf-8",
                )
                with self.subTest(control=repr(control)), self.assertRaises(AuditLogError):
                    append_row(path, "phase", "ok", "why", "proof", "PASS")
            for timestamp in ("not-a-timestamp", "2026-01-01T00:00:00", "2026-01-01T01:00:00+01:00"):
                path = root / f"timestamp-{len(timestamp)}.tsv"
                path.write_text(
                    "ts\tphase\tdecision\twhy\tevidence\tresult\n"
                    f"{timestamp}\tp\tok\twhy\tproof\tPASS\n",
                    encoding="utf-8",
                )
                with self.subTest(timestamp=timestamp), self.assertRaises(AuditLogError):
                    append_row(path, "phase", "ok", "why", "proof", "PASS")

    def test_log_normalizes_hostile_evidence_and_clock_values(self) -> None:
        class BadPath:
            def __fspath__(self) -> str:
                raise ValueError("path conversion failed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid = root / "invalid.tsv"
            with self.assertRaises(AuditLogError):
                append_row(invalid, "phase", "ok", "why", BadPath(), "PASS")
            self.assertFalse(invalid.exists())
            for clock in (lambda: "not-a-datetime", lambda: (_ for _ in ()).throw(ValueError("clock failed"))):
                path = root / f"clock-{len(list(root.iterdir()))}.tsv"
                with self.subTest(clock=repr(clock)), self.assertRaises(AuditLogError):
                    append_row(path, "phase", "ok", "why", "proof", "PASS", clock=clock)


if __name__ == "__main__":
    unittest.main()
