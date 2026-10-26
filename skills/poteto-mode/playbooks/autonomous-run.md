### Autonomous run

You own the exit condition. Define done, then drive to it without relaxing the
predicate.

1. State the exit condition as a checkable predicate before the first
   iteration. Examples include a passing repro, an exact file inventory, a
   zero image diff, or a verified local queue.
2. Choose a bounded wake mechanism. Prefer a local status poll or an available
   monitor with an explicit interval and maximum number of iterations. If no
   event surface exists, use a finite local loop. Do not leave a recurring
   watcher running after the predicate is met.
3. Each iteration makes the smallest change the evidence supports, checks the
   predicate, and records the result through show-me-your-work. Keep an
   advancing change. Revert a change that does not help or that weakens the
   regression check.
4. Treat side discoveries as part of the run only when they block the
   predicate. Put unrelated reversible work in a separate local note. Stop
   for a product choice, an irreversible action, or a missing capability.
5. Checkpoint each iteration with the state, changed paths, evidence, and
   predicate result. A plateau triggers a changed hypothesis, not an
   unbounded loop.
6. Stop when the predicate passes. Stop with BLOCKED when the required
   capability is absent. Stop with a dead-end record after the bounded retry
   budget is exhausted. Never call a plateau a success.
7. If the run reaches a forge, deployment, external message, or deletion
   boundary, keep the operation in the fixture adapter until the user names
   the exact action.
8. A code-changing run is not complete at a local predicate. Return the diff,
   exact head SHA, checks, proposed body, reviewer result, and PR handoff
   state. After local checks pass, push scoped commits and create a review PR
   under the change-producing request. Updating an existing PR remains a
   separate operation. If the forge is unavailable, keep external status
   `NOT RUN` and record `local_handoff_ready` separately.
9. For a retained code change, route the handoff through [Opening a PR](./opening-a-pr.md).
   Push, PR, review, merge, and deployment operations remain independently
   gated.

Use the state shape in [the capability contract](../references/capability-boundaries.md)
and the local audit trail from $show-me-your-work.

UNSUPPORTED-SEAM: a host-provided recurring loop or watcher is not assumed.
Use a bounded local poll, or report BLOCKED with the missing event surface.

Reply with the exit predicate, iteration records, kept and reverted changes,
final state, evidence paths, and the first action after any BLOCKED result.
