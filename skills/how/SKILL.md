---
name: how
description: Explain how an existing code path, subsystem, or repository works. Use for code walkthroughs, placement questions, and ownership or layering decisions.
---

# How

Explain the system well enough that another engineer can change it safely.

1. State the question and your interpretation of its scope.
2. Find the entry point and trace the real path through the repository.
3. Name the data shape at each meaningful boundary.
4. Separate the normal path from error paths, retries, and side effects.
5. Point to the files and symbols that prove each important claim.
6. Run a focused check when a claim depends on runtime behavior.

For a small question, inspect one path and answer directly. For a subsystem, inspect its callers, state transitions, persistence, and external boundaries. Do not replace a walkthrough with a list of filenames.

Write the explanation in the order a new maintainer needs it. Start with what the system does. Then explain the flow. End with the constraints and the places a change could break.
