### Opening a PR

For Showpass backend/frontend work, also read
[Showpass PR lifecycle](showpass-pr-lifecycle.md). It owns repository-specific
PR standards, authorized reviewer/Jira updates, QA, and post-merge verification.
Its lifecycle-specific authorization applies over the generic defaults below;
merge and deployment mutations retain their explicit permission gates.


This is the handoff at the end of another playbook. It prepares a review
package and, for an authorized change-producing task, publishes it for review.

Worktree:

- Work in the isolated checkout named by the user. Keep unrelated dirty files
  out of the patch.
- Use a fresh local branch or worktree when branch ownership is unclear.
- Do not reset, remove, or apply a patch over another user's checkout.

Commits:

- Keep commits small and ordered. Each commit should tell one part of the
  story and pass its focused check.
- Stage named files only. Keep local audit trails out of the tree unless the
  reviewer needs them.
- Before a commit, run the prose and comment checks requested by the project,
  including `$technical-writing`, `$unslop`, and `$no-comments` when those
  skills are available. Record `NOT RUN` with the missing capability when one
  is unavailable.

Review package:

- Use a conventional imperative title such as type(scope): subject.
- The body contains Why, Scope, Tradeoffs when needed, Blast Radius, and
  Verification. List real commands and outcomes.
- Attach screenshots or videos only when the matching surface produced them.
  A missing surface is BLOCKED, not a fabricated artifact.

Forge gate:

1. Resolve the selected forge adapter before any remote operation. A local
   fake adapter is the default in tests.
2. A local review package is ready when the local diff, exact head SHA, checks,
   and proposed body are recorded. Preparing it does not require forge
   authorization.
3. A request to implement a change authorizes pushing its scoped commits and
   creating a review PR once local checks pass. Verify the remote, branch,
   target base, exact head, and proposed body before each operation. Do not
   push unrelated work or force-push without separate authorization.
4. Ready, edit, comment, merge, auto-merge, and deployment remain separate
   operations requiring their own authorization. Push and PR creation do not
   imply approval for those actions.
5. If a remote operation lacks authorization, return its canonical external
   status as `NOT RUN`. Carry `local_handoff_ready: true` when the local
   package is complete. Do not leave a watcher or retry loop active.

UNSUPPORTED-SEAM: no implicit forge, PR, video, or deployment adapter exists.
The local handoff is the safe fallback.

Reply with worktree state, ordered commits, title and body path, checks,
authorization state, and the next operation.
