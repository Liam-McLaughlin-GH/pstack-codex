### Authoring or modifying a skill

You own the skill's voice and its contract. The deliverable is a discoverable
Codex skill that another session can load without guessing.

1. Route authoring through the installed skill-creator skill when it is
   available. If it is unavailable, use the repository's skill layout and
   frontmatter rules directly, then report the missing capability.
2. Define the skill name, description, trigger, input scope, output contract,
   and the smallest set of files it may read or write before drafting.
3. Validate the frontmatter. It must contain name and description, the name
   must match the directory, and every local Markdown reference must resolve.
4. Add structural tests when a rule can be checked by a fixture. Skip a test
   only when the behavior is subjective and state why.
5. Read every referenced skill and reference file. Do not restate a rule that
   belongs to another skill. Use a current Codex skill call when delegation
   is useful.
6. Run the narrow structural check, the repository verifier, and
   git diff --check. Inspect the resulting tree, not only command output.
7. After local checks pass, hand off to Opening a PR to prepare the local
   review package. Forge authorization is not required for that preparation.
   Keep external status `NOT RUN` and `local_handoff_ready` separate when the
   exact create, push, or review operation is not authorized.

Keep prose that changes a decision. Delete duplicate explanation. When a
workflow repeats, encode it in a check or fixture instead of adding another
paragraph.

UNSUPPORTED-SEAM: the installed skill-creator surface may not be available in
this session. Use the local frontmatter and link checks as a safe fallback and
mark the authoring lane BLOCKED if that skill is required.

Reply with the skill summary, the trigger and output decisions, validation
commands and results, unresolved references, and the next authorized action.
