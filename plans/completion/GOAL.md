/goal Complete pstack-codex issues #1–#10 with one verified implementation PR per issue, then resolve the remaining upstream parity work in plans/completion/parity.md. Persist progress so the program resumes after interruption. Deliver PR links, checks, merge order, and an honest parity report.

Workspace: /Users/liammclaughlin/PycharmProjects/pstack-codex
Mode: implementation.
Problem: Finish the partial Codex port and its Python and Showpass repository skills.
Inputs: Read plans/completion/{README,orchestration,models,skills,parity,status}.md and every issues/*.md before dispatch. Refresh the live issues and PRs; reuse existing work.
Context: Upstream cursor/plugins at 53e579f1481697931fc44f5445171397cfa2b24b. Ten issue briefs define scope; upstream behavior must be inspected before implementation. Short generic rewrites do not establish parity.

Permission envelope:
- File edits, local tests, formatters, commits and staging: allowed in isolated pstack worktrees and this plan's result directories.
- Pushes: allowed only to task branches in Liam-McLaughlin-GH/pstack-codex.
- PR creation and updates: allowed for this program; reference the issue and verification. Coordinator owns publication.
- Supplemental parity issues and associated PRs: allowed for the concrete gaps enumerated in parity.md; check for duplicates.
- Read-only inspection of web-app, showpass-frontend and web-k8s: allowed for issues #9/#10 from this user's named repository scope. Read their AGENTS.md first.
- Changes to those three repositories, deployment commands, production queries/mutations, merges, force pushes, paid API fallback and global Codex setting changes: forbidden.
- PR comments: only program evidence or blocker reports when needed; no requests to other people.
- Jira, Slack, Coda writes: forbidden.
- Real user runtime installation: leave for review; test installation in isolated temporary directories.
- Beads tracking: none required. Use the file ledger; do not introduce a new tracking service.

Internal work: Use two concurrent Luna workers at most, in separate worktrees. Follow models.md, with Luna coordination and bounded Terra/Sol escalation. Native subagents may be used only when their model can be selected and verified. Otherwise use supervised codex exec workers with explicit model flags. Do not silently inherit an expensive parent model.
Tooling delegation plan: delegate issue implementation, scoped repository research and batched independent reviews. Parent reads required instructions, owns the ledger, shared integration, staging/commits/pushes, PR publication and final checks.
Result persistence: plans/completion/status.md and results/issue-NN.md; sanitized final evidence is committed to the relevant task branch. Raw process logs, auth and transcripts stay outside tracked paths.
Constraints: default service tier, Fast off, ChatGPT login, max two concurrent workers. No arena/model voting. Read the exact issue brief and necessary source paths per worker, not the whole session transcript.
Safe continuation while blocked: allowed for independent issues. Record an issue-specific blocker and continue ready work.
Hard stop when: quota/auth blocks all progress, a required model is unavailable with no permitted lower-cost path, or completion requires an action outside the envelope. Persist resumable state before stopping; never loop on quota errors.
Deliverable: a PR for every issue, independent review and behavior evidence, coherent combined-tree validation, explicit missing parity, and final report.
Result packet format: issue, source/base/head SHAs, worker/model/effort, attempts, files, checks with outcomes, scenario evidence, reviewer findings and resolution, PR URL/base/dependencies, limitations, next action.
Completion: all ten issue PRs meet their live acceptance criteria; supplemental parity items are implemented or explicitly classified unsupported with evidence and follow-ups. A draft or blocked PR satisfies visibility only, not development completion. Do not merge or close issues manually. Continue until the completion predicate or a genuine recorded blocker.
