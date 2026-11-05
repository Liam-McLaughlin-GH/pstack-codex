### Showpass backend fix

Use for scoped Django or backend work in `web-app`, including API behavior,
calculations, reports, tasks, data repair scripts, and backend CI failures.

1. Confirm the checkout path, Bitbucket remote, branch, and dirty state. Keep
   the user's active checkout intact. Use an isolated checkout for PR fixes,
   branch conflicts, or concurrent work.
2. Read the checkout's `AGENTS.md` and relevant architecture, standards, and
   domain docs. Follow workspace rules and do not inspect `web-k8s` unless the
   user grants permission for it in this turn.
3. Trace the real request/task/data flow through callers, models, services,
   serializers, and side effects. For financial, permission, or data repair
   paths, identify the trust boundary and exact record scope before editing.
4. Keep the change within the reported behavior and preserve existing API
   contracts, money/data safeguards, and error handling.
5. Verify with the narrowest relevant backend check that the user authorized.
   Check the original failing path and nearby callers when the blast radius
   requires it. State database, provider, and Production limitations plainly.
6. Review the final diff for unrelated edits and prepare a backend PR handoff
   using the current web-app template and title format. Keep local checks,
   Bitbucket CI, review, and deployment as separate states.

Do not stage, commit, push, merge, run Production scripts, or claim manual QA
unless the user authorized that exact action. For a support hotfix, follow
[Showpass hotfix](./hotfix.md) as the outer workflow.

Reply with repo/branch, backend flow traced, files changed, checks actually
run, residual risk, and next action.
