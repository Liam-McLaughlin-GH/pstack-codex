### Worktree cleanup

You own the disk report and the safety gate. Cleanup defaults to dry-run.
Deletion is a separate, explicitly authorized operation.

1. Record disk state and obtain worktree paths from git worktree list. Do not
   construct paths from memory. Classify each by size, age, branch, merge
   state, dirty files, active use, and local audit pointers.
2. Treat the audit as advice. Cross-check every candidate against the
   user-supplied active set and current worktree status.
3. For uncertain or recently used paths, read the supplied local activity note
   and report the owner or missing evidence. Dirty worktree state pauses the
   candidate.
4. Dry-run proposes only clean, merged, unused paths. Name every proposed
   path and the reason. Do not remove a path in a dry-run.
5. If the user authorizes a particular deletion, re-check the exact path,
   active use, branch, and dirty state immediately before the fixture action.
   Record the operation and post-action accounting. Do not infer that a broad
   authorization covers other paths.
6. Treat simulator, cache, runtime, and repository deletion as separate
   operations. Keep simulator deletion NOT RUN, cache deletion NOT RUN,
   runtime deletion NOT RUN, and repository deletion NOT RUN unless each exact
   target is authorized.
   Runtime and repository deletion are separate operations with separate
   targets and authorization records.
7. Confirm disk state and worktree list after any controlled fixture action.

UNSUPPORTED-SEAM: no automatic transcript, simulator, cache, worktree-removal,
or deletion adapter is assumed. The safe fallback is a local inventory and
dry-run fixture report. Each exact deletion remains explicitly authorized.

Reply with before and after disk evidence, proposed or deleted paths, held
paths with reasons, dry-run state, and the next authorization gate.
