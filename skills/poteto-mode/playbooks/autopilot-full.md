### Autopilot full

You own the verdicts, never the remote queue. One local owner may carry an
independent work item from build to merge-ready. Nothing merges without a
fresh verified verdict and explicit landing authorization.

1. Mark operator-named items and honor state-then-wait. A request to state the
   protocol is not permission to execute it. Start only after an explicit go.
   Record the queue, owner, branch, head SHA, base SHA, and current state.
2. Assign one owner to each independent item when Codex delegation is
   available. Give each owner a disjoint worktree or branch and an exact
   scope. If delegation is unavailable, keep the work in this session or
   report BLOCKED. Each owner keeps a local decisions trail and returns its
   evidence.
3. Resolve one forge adapter for read operations and record the choice. Use a
   fake forge for tests. Check the exact head SHA before every verdict. A
   changed head voids the old verdict unless a stable patch-id check proves
   that the code patch is unchanged, followed by fresh mergeability checks.
4. Verify each merge-ready head with independent lanes. Re-run gates at that
   SHA, exercise the changed local surface, compare the receipts with the
   diff, and run a regression check against the declared base. CI status is
   input, not the verdict. A missing live surface is BLOCKED.
5. Keep operator-named items at merge-ready. For an autonomous landing
   request, the user must name the landing authorization. A fake forge may
   record an attempted merge only after that authorization. Otherwise no
   merge, push, PR edit, external comment, or deployment is run.
6. Audit the queue at a bounded cadence. Read the playbook and the current
   state record at each tick. Use a 30-minute audit tick when the user asks
   for a standing run, with a bounded local timer and a recorded wake event.
   Count side effects only as progress. A lane that exceeds its time budget
   without a state change is stuck. Stop it in the fixture state and record a
   replacement plan rather than waiting forever.
7. On an operator hold, put every owner into a zero-write state. Preserve
   their briefs and local evidence. Do not delete a worktree or discard
   uncommitted work during the hold.

8. For an accepted code change, route the merge-ready handoff through
   [Opening a PR](./opening-a-pr.md). Keep PR creation, push, ready, comment,
   merge, auto-merge, and deployment NOT RUN until each exact action is
   authorized.

The queue record uses one row per item. The verification ledger keys verdicts
by item and head SHA. The frontier is computed from the ledger, not inferred
from a reply.

UNSUPPORTED-SEAM: remote forge, worker, watcher, and merge adapters are not
implicit. Use a fake adapter in tests, or stop at the named capability with
BLOCKED or NOT RUN.

Reply with the queue, owners, states, exact heads, independent verdicts,
operator gates, local evidence paths, and the next authorized operation.
