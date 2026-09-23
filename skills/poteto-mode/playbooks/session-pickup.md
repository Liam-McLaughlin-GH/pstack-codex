### Session pickup

You own the resume point. Read the explicitly supplied trail and do not redo
completed work.

1. Select a bounded local transcript, decision trail, branch, diff, or artifact
   supplied by the user. Read its metadata and last state first, then the
   decision points. Reject paths outside the declared workspace.
2. Reconstruct branch, worktree, landed commits, open todos, decisions,
   verification receipts, and side-effect state. The supplied trail is input,
   not permission for new operations.
3. Diff done against pending. Do not rerun a completed repro unless a new
   artifact contradicts its result. Name the exact resume point.
4. Route the remaining work to the matching playbook. The pickup playbook
   ends after routing. The routed playbook owns execution and gates.
5. Verify inherited claims against the current local artifact. A previous
   self-report is not proof. A changed head invalidates old verdicts.

UNSUPPORTED-SEAM: private session stores and remote agent URLs are not scanned
implicitly. Use user-supplied local files or a pushed branch reference. If the
trail is incomplete, report BLOCKED or INCONCLUSIVE and list the missing
artifact.

Reply with inherited facts, work not repeated, resume point, current state,
verification performed, and outcome.
