### Showpass backend/frontend loop

Use when one behavior crosses `web-app` and `showpass-frontend`. Keep one
customer outcome and one shared API contract across both repos. Treat this as
a serialized integration loop, not two unrelated fixes.

1. Rehydrate the full Jira/Slack/PR context and acceptance criteria. Confirm
   the user-visible outcome, affected roles, error behavior, and the exact
   backend and frontend surfaces. Read current repo guidance for both repos.
2. Inventory each checkout independently: path, remote, branch, dirty state,
   candidate branches, PRs, and current base. Preserve active work and use
   separate worktrees when a branch is already in use. Never enter `web-k8s`
   without current-turn permission.
3. Trace the flow from frontend action through API request, backend validation
   and service, persisted or returned data, then back through frontend state
   and rendering. Write down the contract fields, types, nullability,
   permissions, status codes, and error shape before changing either side.
4. Resolve product or contract ambiguity before implementation. Build the
   backend contract first when it defines the data shape; update frontend
   callers against that verified shape. If the existing backend contract is
   sufficient, record it and begin at the frontend. Do not invent a new API
   or silently drop existing fields.
5. Work in one repo at a time with disjoint ownership. Apply
   [Backend fix](./backend-fix.md) and [Frontend fix](./frontend-fix.md) for
   their local conventions. Reconcile both diffs against the same acceptance
   criteria and check that the frontend handles success, empty, loading, and
   error responses from the backend.
6. Verify the backend contract with the narrowest authorized backend checks.
   Verify the frontend with its targeted checks, then capture or exercise the
   integrated flow when the required services and browser are available.
   Label local tests, API capture/replay, browser QA, CI, and Production proof
   separately. A backend test cannot prove the UI and a UI mock cannot prove
   the API.
7. Prepare linked PR handoffs with the dependency order and exact base/head
   SHAs. Check both repos' current CI/review state before each push; budget
   pipeline-triggering pushes across the pair. Keep Jira state, PR creation,
   comments, push, merge, and deployment within the authorization actually
   given.
8. Repeat the paired loop after review: read current-head feedback in both
   repos, fix the smallest linked slice, refresh both heads and checks, then
   re-evaluate the integrated contract. Stop when one side changes the
   contract without a matching update on the other.

For a support-driven issue, use [Showpass hotfix](./hotfix.md) as the outer
workflow. For a long-running multi-card effort, use
[Orchestrate](./orchestrate.md) to maintain the queue and context; this loop
owns one coherent end-to-end slice at a time.

Reply with the user outcome, both repo/branch pairs, the shared contract,
dependency order, per-repo checks, integrated evidence, open blockers, and
next action.
