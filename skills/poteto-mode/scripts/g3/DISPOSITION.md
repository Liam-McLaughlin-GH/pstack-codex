# G3 helper inventory and disposition

This reference inventories every file below the pinned upstream script
directories.  The useful offline behavior is adapted into the Python modules
in this directory and the audit log helper under
`skills/show-me-your-work/scripts/`.  The repository `LICENSE` retains the
MIT attribution for adapted material.

Source snapshot: `cursor/plugins` pstack at commit
`53e579f1481697931fc44f5445171397cfa2b24b`.

## Code and support files

| Upstream file | Disposition | Local result |
| --- | --- | --- |
| `poteto-mode/scripts/bootstrap.ts` | Unsupported runtime bootstrap | No import-time install or restart; current helpers use Python stdlib. |
| `poteto-mode/scripts/check-plan.mjs` | Adapted policy | `g3/plan_checker.py` uses a supplied profile and the current completion profile. |
| `poteto-mode/scripts/orch/orch.ts` | Adapted CLI surface | `g3/orch.py` exposes local store operations; it never launches agents or mutates a repository. |
| `poteto-mode/scripts/orch/store.ts` | Adapted local state core | `g3/orch.py` provides atomic files, PID locks, stale-lock recovery, inbox, gates, frontier, status, units, and ledger. |
| `poteto-mode/scripts/watch-pr/cli.ts` | Adapted offline entry point | `g3/watch.py` accepts an explicit recorded fixture; live provider resolution stays outside the policy. |
| `poteto-mode/scripts/watch-pr/github.ts` | Adapter boundary only | `JsonProviderAdapter` validates recorded provider data; no `gh`, network, or provider-specific review markers are assumed. |
| `poteto-mode/scripts/watch-pr/policy.ts` | Adapted pure policy | `g3/watch.py` contains blocker precedence, pending waits, queue sweeps, retry backoff, timeout, and injected clock/sleep. |
| `poteto-mode/scripts/watch-pr/render.ts` | Minimal JSON result shape | Watcher results are dictionaries suitable for JSON rendering; no provider output is trusted without validation. |
| `poteto-mode/scripts/watch-pr/types.ts` | Adapted typed contract | Dataclasses and protocols define contexts, facts, checks, snapshots, reader, clock, and query errors. |
| `poteto-mode/scripts/watch-pr/watch-pr` | Unsupported Bun wrapper | No wrapper bootstraps dependencies; the Python module is directly executable. |
| `poteto-mode/scripts/worktree-audit.sh` | Read-only rewrite | `g3/worktree_audit.py` consumes explicit metadata, never fetches, scans transcripts, merges, or deletes. |
| `show-me-your-work/scripts/log.sh` | Adapted helper | `show-me-your-work/scripts/log.py` implements the six-column UTC TSV contract; `log.sh` is a dependency-free compatibility wrapper. |

## Manifests and configuration

| Upstream file | Disposition | Local result |
| --- | --- | --- |
| `poteto-mode/scripts/package.json` | Not copied | There is no new JavaScript package or install boundary. |
| `poteto-mode/scripts/watch-pr/tsconfig.json` | Not copied | Python annotations and `py_compile` provide the local static check. |

## Lockfile

| Upstream file | Disposition | Local result |
| --- | --- | --- |
| `poteto-mode/scripts/bun.lock` | Not copied | No runtime dependency or network install was added. |

## Tests and fixtures

| Upstream file | Disposition | Local result |
| --- | --- | --- |
| `poteto-mode/scripts/orch/orch.test.ts` | Scenarios adapted | `tests/g3/test_g3_tooling.py` covers store state, malformed files, stale locks, inbox drain, frontier injection, and refused side effects. |
| `poteto-mode/scripts/watch-pr/cli.test.ts` | Scenarios adapted | Tests exercise explicit fixture input, deterministic output, and status/polling behavior. |
| `poteto-mode/scripts/watch-pr/fakes.test-helper.ts` | Contract adapted | `FakeReader`, check factories, call history, and `DeterministicClock` are in `g3/watch.py`. |
| `poteto-mode/scripts/watch-pr/github.test.ts` | Offline shape scenarios adapted | Recorded JSON fixtures cover malformed and unknown provider values; no provider command is executed. |
| `poteto-mode/scripts/watch-pr/policy.test.ts` | Policy scenarios adapted | Tests cover success, blocker precedence, pending timeout, and queued completion without merge calls. |
| `poteto-mode/scripts/watch-pr/types.compile.ts` | Runtime schema scenarios adapted | Dataclass construction and adapter validation are exercised by the focused tests. |
