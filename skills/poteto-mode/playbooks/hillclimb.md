### Hillclimb

You own the metric and the experiment's integrity. Each iteration changes one
thing, measures it, then keeps or reverts it.

1. Run $how over the target and choose a workload that reproduces the user's
   complaint. Name the metric, unit, direction, noise rule, target, and
   minimum attempt count. If the case does not reproduce, repair the
   measurement setup before changing code.
2. Build a small measurement lever. Run contrasting workloads, prove that the
   target case separates, then freeze the command and its sampling rule.
   Capture the baseline and a green regression gate before editing.
3. Start a local decision.tsv through $show-me-your-work. Each row contains
   id, hypothesis, change, before, after, delta, checks, verdict, and note.
4. Ground one hypothesis per iteration in the architecture model. A
   hypothesis names a mechanism, not a vague optimization.
5. Implement in a bounded worktree or local slice. Measure before and after,
   run the regression gate, and keep a change only when the metric clears
   noise and the gate remains green. Revert the full change otherwise.
6. Record both kept and reverted attempts. One accepted change gets one
   ordered commit when commits are in scope. Do not stage unrelated files.
7. Push past the first plateau. Re-read the source, change the category, or
   combine a supported near-miss. Stop when the predicate passes or the
   bounded set of cheap hypotheses is exhausted. Do not relax the predicate.
8. Route accepted work to [Opening a PR](./opening-a-pr.md). Push and forge
   actions stay behind the side-effect gate.

UNSUPPORTED-SEAM: live timing, profiler control, and delegated workers may be
unavailable. A local harness can produce a fixture result, but a missing
matching runtime surface is BLOCKED rather than a claimed performance win.

Reply with metric and target, baseline to final, kept and reverted iterations,
decision log, evidence paths, and the next bounded hypothesis.
