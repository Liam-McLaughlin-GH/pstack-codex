### Perf issue

You own the measurement story. Tie each fix to a captured baseline and a
matching post-change artifact.

1. Confirm that the matching profiler, browser, trace, or process-control
   surface is available. If representative measurement is unavailable, mark
   performance verification BLOCKED. Local candidate implementation and review
   may proceed with that limit stated; do not claim a verified performance
   improvement or performance readiness. If a supplied artifact is wrong-surface or
   unmappable, return INCONCLUSIVE. If capture is unapproved or its
   precondition is unmet, return NOT RUN. Otherwise
   capture a baseline on the matching surface. Name metric, unit,
   workload, sample count, noise rule, and artifact path. Explain why the
   workload represents affected callers, data volume, relationship fan-out,
   and composed queries. Include empty scopes and repeated background work
   when they could change the decision. Measure the complete operation; query
   count is supporting evidence. Use an authorized representative environment;
   Production is not automatically required.
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

### SQL verification layer

For a significant query rewrite, compare baseline and candidate SQL on
representative production data before recommending performance readiness.
Start with the environment's approved read-only broker and plain
`EXPLAIN (FORMAT JSON)`. When actual execution plans are needed and the broker
rejects `ANALYZE`, use an explicitly approved SQL Explorer or isolated
production-data environment. Do not disguise SQL or weaken the broker filter.

Confirm the selected connection, read-only protection, statement timeout,
target records, overall time budget, and stop conditions before executing
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on read-only SELECTs. Syntax support
does not establish resource isolation or safe limits. Do not analyze writes,
locking SELECTs, or side-effecting functions through this route. Keep existing
stricter limits and obtain separate authorization when the scope must expand.

Use application-generated SQL and bound parameters, including downstream query
composition. Compare equivalent inputs and results across relevant empty,
typical, and high-fanout scopes. Record tested revisions, connection, limits,
sample/cache conditions, plans, actual rows and loops, buffer activity, and
temporary work. Retain sensitive SQL and plans in private evidence and share
sanitized summaries. Keep correctness and concurrency checks as well.

Measure whole-operation timing separately where authorized. Estimated plans
alone do not verify runtime; replica execution does not prove primary
concurrency or fleet performance. If the required comparison cannot be
completed within the allowed scope, leave performance readiness unverified and
name the missing check.

### Showpass query route

For Showpass query changes, start with `$showpass-db-broker` for bounded
read-only plan checks. When actual execution plans are needed and the broker
rejects `ANALYZE`, use Showpass Explorer or an isolated production-data
environment only after its read-only protections, connection, statement timeout,
target records, time budget, and stop conditions are explicitly confirmed and
authorized. Use application-generated SQL and bound parameters, compare
equivalent empty, typical, and high-fanout scopes, and include downstream
composition. Replica execution does not establish primary concurrency or fleet
performance.

UNSUPPORTED-SEAM: no live profiler, browser, trace parser, or process-control
surface is assumed. A local fixture or supplied artifact supports conclusions
only for its tested workload and candidate revision; it cannot prove a live
performance change. Missing representative measurement leaves performance
verification BLOCKED, while bounded local candidate work may proceed. A
wrong-surface artifact is INCONCLUSIVE and an unapproved capture is NOT RUN.

Reply with baseline, post-fix number, delta, workload, artifacts, source
mapping, and any INCONCLUSIVE boundary. State whether the evidence supports
code review, a bounded experiment, or performance readiness, and name the
missing check when representative performance remains unverified.
