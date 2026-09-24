### Perf issue

You own the measurement story. Tie each fix to a captured baseline and a
matching post-change artifact.

1. Confirm that the matching profiler, browser, trace, or process-control
   surface is available. If the required surface is missing, stop before
   editing and return BLOCKED. If a supplied artifact is wrong-surface or
   unmappable, return INCONCLUSIVE. If capture is unapproved or its
   precondition is unmet, return NOT RUN. Otherwise
   capture a baseline on the matching surface. Name metric, unit,
   workload, sample count, noise rule, and artifact path.
2. Run $how over the affected path. Use the following strategy families as
   hypothesis prompts, not a checklist: elimination, divide and conquer,
   caching, indirection, batching, redundancy, lazy evaluation, and
   scheduling. Attempt a family only when source or trace evidence supports
   it.
3. Run $architect before a cross-function change. Delegate a bounded fix only
   through an available local worker, then inspect the diff.
4. Capture a post-fix artifact with the same workload. Compare artifacts in a
   queryable form. Report before, after, delta, unit, source mapping, and
   paths. An artifact with no mapping or a wrong surface is INCONCLUSIVE.
5. Apply $principle-sequence-verifiable-units. Verify one attempt before starting the
   next. Cite the measurement in the handoff.
6. Route accepted work through [Opening a PR](./opening-a-pr.md) for local
   package preparation. Forge and deployment actions remain independently
   gated.

UNSUPPORTED-SEAM: no live profiler, browser, trace parser, or process-control
surface is assumed. A local fixture or supplied artifact can support Trace
forensics, but it cannot prove a live performance change. Missing matching
surface status is BLOCKED before any code change; a wrong-surface artifact is
INCONCLUSIVE and an unapproved capture is NOT RUN.

Reply with baseline, post-fix number, delta, workload, artifacts, source
mapping, and any INCONCLUSIVE boundary.
