### Feature

You own the design and the implementation review. Build from a named data
shape, not a pile of conditionals.

1. Run $how over the affected subsystem.
2. Run $architect for the design shape. If the boundary is obvious and the
   skill is skipped, write architect skipped with the evidence. Do not hide a
   design choice inside the implementation.
3. Write the throughput checkpoint as four items. Keep an item as n/a with a
   reason when it does not apply:
   - blocking gates before fan-out;
   - independent workstreams and their file boundaries;
   - shared mutable state and the split-before-serialize choice;
   - the smallest safe decomposition and why one owner is enough when it is.
4. Name the data shape and its organizing structure before implementation.
   Choose a state machine, table, registry, typed model, or boundary object
   when that shape keeps callers from carrying internal rules.
5. Delegate code writing through available Codex-native workers by default.
   For one to five supplied independent items, keep this as one lead task and
   assign one Luna-at-max code worker per item. Use fewer workers for shared
   files or dependencies, and keep trivial serial work local. Give writers
   separate worktrees or branches, or disjoint paths. If the worker control or
   a verified Luna lane is unavailable, use a bounded local fallback only when
   it fits; otherwise report BLOCKED. The parent integrates the changes,
   checks each exact branch and head, and reviews the diff and receipts. Queue
   ready items in waves up to the runtime's worker limit. Keep one parent task
   and launch the next queued item as capacity opens instead of serializing
   independent implementation work in the parent.
6. Verify the matching user-observable surface. Tests can cover branches but
   do not replace the behavior check. A missing surface is BLOCKED.
7. Sequence small green units, then run $interrogate when the design is
   contested. Route the local handoff through [Opening a PR](./opening-a-pr.md);
   remote operations remain independently gated.

Keep shared primitive changes synchronized across all consumers. Do not use a
worker summary as evidence. Open the files and receipts.

UNSUPPORTED-SEAM: no worker or model lane is guaranteed by the skill. Check
the Codex-native worker controls and active model catalog; do not treat Herdr,
browser, forge, or deployment access as implied. Use a bounded local fallback
and state the boundary when a live proof cannot run.

Reply with the built behavior, chosen shape and reason, throughput checkpoint,
verification result, open decisions, and authorized next action.
