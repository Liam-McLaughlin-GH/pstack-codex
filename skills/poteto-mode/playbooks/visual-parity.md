### Visual parity

You own pixel comparison. The baseline is the spec and must not be changed.

1. Establish or receive the baseline before migration. It must cover the
   component's states and the matching target surface. No baseline means
   BLOCKED, not parity.
2. Reject harness edits, baseline writes, and component changes made only to
   hide a diff. If the baseline is wrong, stop and report the conflict.
3. Migrate one component at a time. Shared primitives come first. Separate
   worktrees prevent two writers from changing the same baseline or component.
4. Capture the same states on the same available surface. Use image diff, not
   visual confidence. A nonzero diff fails and gets a delta report.
5. A missing browser or image adapter is BLOCKED. A text-only source review
   cannot prove pixel equivalence.
6. Route a passing component to [Opening a PR](./opening-a-pr.md). Prepare the
   review package, push scoped commits, and create a review PR after local
   checks. The baseline is unchanged.

UNSUPPORTED-SEAM: no browser, simulator, screenshot, or image-diff capability
is implicit. Use a supplied pair of images for offline comparison or report
BLOCKED. Never generate a screenshot path without an artifact.

Reply with components, baseline path, diff result for each state, blocked
capabilities, and the remaining work.
