### Opening a PR

This is the handoff at the end of another playbook. It prepares a review
package. It does not silently grant remote write access.

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
2. A PR is ready only when the user has explicitly authorized its creation and
   the local diff, head SHA, checks, and body are recorded.
3. Push, PR create, ready, edit, comment, merge, auto-merge, and deployment
   are separate operations. Ask for or consume authorization for the exact
   operation. One authorization does not imply the others.
4. If authorization is absent, return a complete local handoff with status
   NOT RUN. Do not leave a watcher or retry loop active.

UNSUPPORTED-SEAM: no implicit forge, PR, video, or deployment adapter exists.
The local handoff is the safe fallback.

Reply with worktree state, ordered commits, title and body path, checks,
authorization state, and the next operation.
