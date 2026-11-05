### Showpass frontend fix

Use for scoped UI behavior in `showpass-frontend`, including web, mobile,
desktop, component, hook, API integration, and frontend CI work.

1. Confirm the checkout path, GitHub remote, branch, and dirty state. Preserve
   active work. Use an isolated checkout for PR fixes, branch conflicts, or
   concurrent work.
2. Read the checkout's `AGENTS.md`, architecture docs, and the relevant
   package guidance. Locate the route and feature owner before editing. Prefer
   the repository's Repository -> Service Hook -> Component data flow and
   approved design-system patterns.
3. Trace the user action to the rendered state and its data source. Identify
   the API contract, loading/error/empty states, accessibility impact, and the
   visible behavior that proves the defect is fixed.
4. Make the smallest root-cause change and keep unrelated layout or package
   refactors out.
5. Run only the narrowest useful lint, type, unit, or build checks authorized
   for the task. For a visible behavior change, prepare or perform browser QA
   only when requested and available; automated checks do not prove browser
   behavior.
6. Review the final diff and prepare a frontend PR handoff using the current
   GitHub template and frontend title rules. Keep local checks, hosted CI,
   review, browser QA, and deployment as separate states.

Do not push, merge, resolve review threads, change PR state, or claim browser
QA unless that action was authorized and actually performed. For a support
hotfix, follow [Showpass hotfix](./hotfix.md) as the outer workflow.

Reply with repo/branch, user flow traced, visible behavior, files changed,
checks and browser evidence, QA gaps, and next action.
