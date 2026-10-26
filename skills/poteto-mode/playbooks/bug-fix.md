### Bug fix

You own the task. Reproduce first, trace the mechanism, make the smallest
evidence-backed change, and verify it on the same surface.

1. Reproduce the failure on the matching available surface. Capture input,
   state, output, and the failing command. If the surface is unavailable,
   report BLOCKED after a local fixture attempt. Do not replace a missing
   runtime observation with source inspection.
2. Build binary hypotheses over the affected path. Use $how to trace the
   entry point and $why for history when it can change the split. Instrument
   the local fixture or supplied process to eliminate hypotheses one at a
   time. Record the surviving mechanism.
3. Run $architect before a cross-function or cross-boundary fix. Name the
   data shape, owners, failure states, and alternative. Delegate only when
   the current Codex session exposes a worker capability. Give it a bounded
   scope and review its diff yourself.
4. Verify the original repro on the same surface. A different test or a
   source-only claim is INCONCLUSIVE. Keep the failing output and the passing
   output together.
5. Sequence the evidence. Land or record the failing check before the fix
   when the repository workflow supports ordered commits. Use $tdd for a
   cheap local path. Skip it for expensive or unclear integration work and
   explain why.
6. Run the structural checks and route the local handoff through
   [Opening a PR](./opening-a-pr.md). Push scoped commits and create a review
   PR after local checks pass.

UNSUPPORTED-SEAM: live browser, mobile, or process-control surfaces may be
absent. A local reproduction is useful evidence but does not prove a
wrong-surface bug fix; return BLOCKED or INCONCLUSIVE with the fallback.

Reply with the broken behavior, root mechanism, smallest fix, matching-surface
verification, and failing-then-passing output paths.
