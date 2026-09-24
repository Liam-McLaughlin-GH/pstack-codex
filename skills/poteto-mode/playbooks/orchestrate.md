### Orchestrate

You own the program, never the code. Use this playbook only for work that
outlives one bounded session. A single task with one clear predicate uses
Autonomous run.

## Roles

- The coordinator frames the program, writes briefs, drains the queue, owns
  the human report, and makes decisions. It does not write a worker's code.
- A track owner groups related units only when one coordinator cannot drain
  them. Its boundary and unit list are explicit.
- A worker owns one disjoint worktree or branch and returns facts, evidence,
  and a terminal state.
- A verifier reads the worker result and checks the named artifact. Use a
  different available capability when the check is judgment-heavy.

Every writer has one worktree. Sibling communication goes upward through the
coordinator. A worker result never authorizes a merge.

## Local store

Create a project-local store with one writer per file:

- preferences.md contains numbered standing orders and forbidden actions.
- overview.md is the append-only project record.
- units.tsv has unit, track, state, branch, PR, head SHA, and brief path.
- frontier.json is computed from current parent and head records.
- ledger.tsv has one verdict per unit and head SHA.
- inbox/ holds completion pointers.
- gates.md holds human decisions and options.
- decisions.tsv is the $show-me-your-work trail.
- status.md is derived at drain time.

## Brief

Every brief stands alone and contains:

    GOAL
    SCOPE
    CONTEXT
    ACCEPTANCE
    VERIFY
    TIMEBOX
    FORBIDDEN
    REPORT
    STANDING

The scope names writable paths and paths that are forbidden. The verify field
names the exact command or fixture. The report names status, branch, head,
evidence, deviations, and follow-up. Missing fields are a refuse-to-start
condition.

## Steps

1. Require an explicit go before initializing the local store, starting a
   worker, or fanning out. A plan, status request, or state-then-wait response
   is NOT RUN and leaves the coordinator and every owner at zero-write hold.
2. Frame a countable done predicate, unit count, tracks, stack shape, and
   time budget. If one owner fits, route to Autonomous run.
3. Initialize the local store, write standing orders, and open the decision
   trail before starting a worker.
4. Pilot one unit through brief, work, verification, ledger, and local
   integration. Fix the contract before fan-out.
5. Scale with a bounded rolling window. Recompute ready work after every
   drain. Stop spawning when the time budget reaches its landing threshold.
6. Drain completion pointers in batches. Classify each as landed,
   needs-verification, failed, zombie, or noise. Update units and ledger
   records before refilling.
7. Integrate the first verified unit while later units run. Keep the local
   frontier green. Topology changes have one owner.
8. Close by reconciling every spawned unit to done, abandoned, or
   zombie-reconciled. Verify the terminal predicate on the artifact and audit
   the trail.

## Retained changes

If a worker change is retained, route the local handoff through [Opening a PR](./opening-a-pr.md).
Record the diff, exact head SHA, checks, and proposed body before any forge
operation and set `local_handoff_ready` when that package is complete. Push,
PR create, ready, edit, comment, merge, auto-merge, and deployment are separate
operations; each remains externally `NOT RUN` until its exact operation is
explicitly authorized.

## Verification and failure

A unit needs evidence stronger than a green generic check. Use
live-surface-verified, unit-test-verified, type-check-only, verifier-blocked,
or verifier-failed. A new head voids the row. A blocked verifier is not a
pass.

Probe state read-only. A silent worker gets a postmortem row and a bounded
retry. Retry a cap or memory failure with smaller scope, a network failure as
is, a tool failure with another available capability, and an unknown failure
once. After two retries, abandon the unit and replan.

UNSUPPORTED-SEAM: no implicit coordinator store, agent-spawn, external message,
worker, cloud status, forge, watcher, merge, deployment, or deletion adapter
exists. Use a local fixture store and fake worker records. Report BLOCKED where
the missing capability matters.

Reply with predicate and counts from units.tsv and ledger.tsv, tracks, frontier
records, verdicts, abandoned units, gates awaiting the user, store path, and
trail path. Do not list a remote PR unless one was explicitly supplied.
