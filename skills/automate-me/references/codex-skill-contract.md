# Codex skill contract

The pstack repository stores skills as `skills/<name>/SKILL.md`. A skill must
have a YAML frontmatter block with `name` and `description`, followed by
instructions. Names use lowercase letters, digits, and single hyphens.

Keep detailed material in a directly linked file under `references/` when the
skill needs it. Resolve every relative link from the skill directory. Reject a
missing link, an absolute link, and a link that escapes the skill directory.

Use the skill-creator initializer only in a temporary or explicitly approved
workspace. The initializer is an authoring aid, not permission to modify the
Codex runtime. The `automate_me.py` helper provides deterministic routing,
allowlisted evidence reads, collision handling, generation, and link checks.

The repository's shared port checker is structural only. It does not replace
the helper exercise, a collision test, or a review of the generated skill.
