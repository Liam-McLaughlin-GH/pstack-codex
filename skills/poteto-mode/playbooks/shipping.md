### Shipping

For Showpass backend/frontend work, also read
[Showpass PR lifecycle](showpass-pr-lifecycle.md). It owns repository-specific
PR standards, authorized reviewer/Jira updates, QA, and post-merge verification.
Its lifecycle-specific authorization applies over the generic defaults below;
merge and deployment mutations retain their explicit permission gates.


You own what lands. Verify each item independently, land only the contiguous
verified run, and stop after the next gap.

1. Resolve the forge adapter before reading remote state. Use a fake forge for
   tests. Verify each item against its parent and head. A verifier that wrote
   the code is not independent.
2. Walk from the lowest unmerged item. Stop at the first missing or failed
   verdict. A verified item above an unverified parent is not eligible.
3. Record verdict head SHA, base SHA, and patch-id. Recompute the patch-id
   after a rebase or base retarget. A changed patch needs a fresh verdict.
   An unchanged patch still needs current mergeability and check state.
4. Prepare only the bottom item against the exact current base. A
   change-producing request authorizes its scoped push and review PR creation
   after local checks. Keep other remote writes in a local fixture until
   authorized.
5. Land one item at a time only after explicit ship or merge authorization.
   A fake forge can record the proposed operation. Without authorization the
   merge log must remain empty.
6. Recompute the frontier after each fixture merge or supplied remote update.
   Confirm the merged head, next base, checks, and patch-id before continuing.
7. Poll only within a bounded interval and stop at the first failure,
   unavailable capability, or ceiling. Do not arm auto-merge or deployment.

The report names verified run, ceiling, verdict producer, current state, and
what the next gap needs.

UNSUPPORTED-SEAM: no remote forge, merge queue, watcher, auto-merge, or
deployment adapter is implicit. Use fixture state and explicit authorization,
or report BLOCKED or NOT RUN.

Reply with the verified run and ceiling, each verdict and evidence, authorized
actions taken, and the next unverified item.
