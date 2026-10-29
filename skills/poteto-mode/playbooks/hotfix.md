### Showpass hotfix

Use this for a support-reported customer problem when Liam asks to apply a
code hotfix. A Slack/Jira report alone requests investigation, not a code
change. Start from the support evidence and keep the repair tied to the exact
customer-visible failure.

1. Read the full supplied Slack/Jira context and linked evidence. Identify the
   affected venue, event, user, order, or other exact record, expected behavior,
   observed behavior, severity, and whether this is Production or Beta. Keep
   customer data out of committed notes.
2. Trace the report to the owning surface. Use the backend or frontend fix
   playbook for implementation. If the owner is unknown, investigate both
   code paths before choosing; do not infer a frontend defect from a backend
   symptom or the reverse.
3. Confirm repo path, remote, branch, and dirty state. Preserve the active
   checkout; use the user's isolated review/fix checkout or a new worktree
   when the branch is owned by another task. Read the repo's `AGENTS.md` and
   relevant domain guidance before editing.
4. Reproduce or validate the mechanism with the supplied evidence and the
   narrowest safe local fixture. Separate source evidence, local data, provider
   state, browser behavior, and Production evidence. Do not use real customer
   data for a local fixture.
5. Make the smallest root-cause change. For a Production-sensitive or
   behaviorally risky workaround, prefer a narrowly scoped feature flag or
   guard. Keep a one-off data repair separate from a persistent code fix.
6. Run only the verification authorized for the task, starting with the
   narrowest meaningful check. Record manual QA, local checks, CI, review, and
   deployment as separate evidence. Do not claim a hotfix is live because the
   code is ready or pushed.
7. Prepare the repo-specific PR handoff. Include the exact customer-visible
   failure, cause, files, verification, residual risk, and next owner action.
   Push, PR edits, merge, deployment, and Production data changes each require
   their own authorization.

Stop and report BLOCKED when the customer scope or expected behavior is
ambiguous, the safe reproduction needs unavailable Production data, or the
repair would change unrelated customers. Do not widen the target set to make
the fix easier.

Reply with the issue identifiers, affected surface, observed failure, root
cause, local change, evidence by layer, and exact next action.
