---
name: no-comments
description: Review comments and suppressions in a diff. Delete stale narration and encode real constraints in code, types, tests, or checks when that is in scope.
---

# No comments

Review comments as part of the code, not as decoration.

1. Read the diff and the surrounding code.
2. Delete comments that restate the code or describe a temporary step that no longer exists.
3. Keep a comment only when the code cannot express the reason and the reason is still true.
4. For a kept constraint, offer the cheapest executable encoding such as a type, runtime check, test, or lint rule.
5. Do not widen the task to fix unrelated comments.
6. Run the narrowest relevant check after deletions or encodings.

Do not remove comments that document an external contract, a safety requirement, or a non-obvious reason that cannot be represented in the code. Verify that judgment against the real call path.
