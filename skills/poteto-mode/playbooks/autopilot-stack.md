### Autopilot stack

You own the verified linear stack, never the landing. Build and verify the
queue, then hand the operator one base-branch chain.

1. A plan, status request, or state-then-wait response is NOT RUN and leaves
   every owner at zero-write hold. Run the local owner loop when the user asks
   to execute Autopilot. This authorizes scoped push and review PR creation
   after local checks, but not other remote mutations or external workers.
2. Resolve the work shape before dispatch. Independent items branch from the
   same base and use Luna-at-max code workers when available. Dependent items
   name their intended parent and remain a single linear chain. One writer
   owns each worktree or branch. Check model availability in the active
   catalog and dispatch controls; if no allowed lane is available, use a
   bounded local fallback or report BLOCKED.
3. Run the owner loop for build, local verification, decision trail, review
   preparation, and merge-ready state. Keep exact base and head SHAs. A
   changed head requires a new verdict. For a standing run, use a 30-minute
   audit tick with a bounded local timer and a recorded wake event.
4. Hold the operator gates. An explicit stop
   moves every owner to zero-write hold. A request to land or merge routes to
   Shipping and still needs explicit authorization.
5. Verify at STACK-READY with independent lanes. Check the gates, matching
   local surface, receipts, diff, and base regression. Record PASS,
   PASS+NOTES, FAIL, BLOCKED, or NOT RUN. A clean stack contains no blocked
   required lane.
6. Append only a cleanly verified item. The root is the only topology writer.
   Recompute the parent tip after each append. A patch-id comparison may keep
   a code verdict across a base rewrite, but mergeability and checks still
   need a fresh read.
7. Absorb drift bottom-up. If a rebase changes a patch, send that item back
   through verification. If a conflict touches an owner's files, report the
   owning branch and stop the topology action.
8. Deliver the chain with a report. Route accepted code changes through
   [Opening a PR](./opening-a-pr.md). Push scoped commits and create review
   PRs after local checks. Retarget, force-push, merge, auto-merge, deployment,
   and external comment still require exact authorization. A fake forge may
   record proposed operations in tests.

The stack record contains item, parent, base SHA, head SHA, patch-id, verdict,
and evidence. A child is not eligible while its parent is unverified.

UNSUPPORTED-SEAM: no Graphite-like stack mutator, remote forge, or live
deployment adapter is assumed. Use ordinary local branch data and a fixture
adapter, or report BLOCKED when a remote surface is required.

Reply with the root and tip records, each link's verdict and evidence, items
parked with reasons, and the next operator gate.
