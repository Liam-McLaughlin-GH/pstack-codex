# Dispatch and integration

## Start and foundation

1. Read workspace instructions and target AGENTS.md. Refresh issues, PRs, branches, worktrees and quota availability. Preserve dirty changes and resume matching branches.
2. Pin upstream source and inspect all relevant upstream files in each lane. Inventory references, scripts, agent roles, playbooks, tool assumptions and instruction differences.
3. Coordinator declares shared contracts: skill directories, sibling reference resolution, safe artifact paths, status/evidence schema, Codex capability detection, and permission precedence. Scope requests in a skill never grant permission for external actions.
4. Issue #8 owns validation/install tooling, including improvement of scripts/verify_port.py. Its current regex-only frontmatter check and SKILL.md-only seam scan are insufficient proof of behavior. Validate references too; don't reject legitimate documentation merely because it mentions an unsupported capability.
5. Begin two workers with #8 and #10. Record worker identity, exact worktree, process/session ID, model and SHA before calling either started.

## Queue

| Wave | Lane A | Lane B | Unlock |
| --- | --- | --- | --- |
| A | #8 setup and validation | #10 repository guide and shared reconnaissance | capability contract, repository evidence |
| B | #1 automation authoring | #6 recall | authoring choices, scoped artifact contract |
| C | #2 verification creation | #9 Python practices | verification schema, source-backed Python rules |
| D | #4 verification maintenance | #7 reflection | consumes #2 and #6 contracts respectively |
| E | #3 task orchestration | #5 bot UI | combines stable workflows; confirms actual UI capability |
| F | Independent review and repairs | Combined-tree validation | ten reviewable PRs |
| G | Supplemental parity queue | Supplemental parity queue | remaining upstream gaps, quota permitting |

This is a ready queue, not a barrier: refill a free slot with an unblocked issue; keep at most two workers total. #9 needs #10 reconnaissance but need not wait for its entire PR. #4 needs #2's verified artifact schema. #7 needs #6's input/privacy contract. #3 consumes the stable contracts; it must not create another recursive coordinator. #5 must inspect upstream before selecting a UI approach; the issue's earlier Cursor assumption may be incomplete.

## Worktree and PR ownership

Create separate sibling worktrees under /Users/liammclaughlin/PycharmProjects/pstack-codex-worktrees/issue-NN, branches codex/issue-NN-SLUG. Use git worktree add with a verified base; never reset an existing worker directory. Workers own their skill folder, scoped references/tests and results/issue-NN.md only. #8 owns shared scripts/CI; coordinator owns README and ledger integration.

Prefer independent main-based PRs when dependency is only a documented contract. If code imports require an unmerged prerequisite, stack #4 on #2 and #7 on #6, naming the actual PR base and merge order. Do not copy prerequisite commits into every independent PR. Shared foundation changes in #8 must either be explicit stacked prerequisites or remain optional until integration.

Workers return files and proof; coordinator inspects, stages exact paths, commits, pushes task branches and opens PRs referencing Closes #N. On retries find the existing branch/PR first. README changes are coordinator-authored per branch with cumulative conflicts resolved in combined validation. Do not label skills active on main before merge; PR text describes the resulting branch.

## Verification and review

Each lane passes full skill frontmatter validation, link/reference checks, whitespace checks, and an issue-specific exercise in a scratch repo. Evidence records actual outputs and failed-before cases when meaningful. YAML validity alone is insufficient.

Review batches: (1) #8/#1/#6 for installation/authoring/privacy; (2) #10/#9 for traceable repository facts and Python rules; (3) #2/#4/#7/#3/#5 for workflow interoperability and UI evidence. A reviewer cannot be the author. Reviews are read-only. Repair concrete findings once, rerun affected checks, escalate only per models.md. Record NOT RUN accurately.

Create a temporary integration branch/worktree from current main and combine candidate heads in dependency order. This is local integration only. Run all shared checks and representative create→maintain and recall→reflect scenarios against the combined tree. Resolve shared README/registry conflicts into the appropriate task branches without force pushing. Recheck all PR head SHAs after fixes. Do not use combined-tree proof as a substitute for branch-specific checks.

A ready PR has the issue link, concrete before/after behavior, limitations, test commands/results, head SHA, independent review, dependency/base information, and no known blocking defects. Missing remote CI is recorded as absent, never green. Fix owned CI failures; classify unavailable infrastructure honestly.

## Resume and finish

After every lane transition persist status, SHA, PR URL, attempts, model, evidence path and next action. Store raw worker output outside Git; persist sanitized receipts only. On resume reconcile GitHub and worktrees before relaunching anything.

Publish partial/draft PRs only for useful reviewable work with explicit blockers. They do not count as completed issues. Finish with results/final.md containing all ten PRs, checks, review verdicts, merge order, supplemental parity disposition and quota usage if exposed. User review/merge remains outstanding.
