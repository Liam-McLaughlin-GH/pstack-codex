# Codex skill contract

The pstack repository stores skills as `skills/<name>/SKILL.md`. Routing a
skill targets the containing `skills/<name>/` directory so it can be passed
directly to the writer. A skill must
have a YAML frontmatter block with `name` and `description`, followed by
instructions. Names use lowercase letters, digits, and single hyphens.

The skill directory basename, frontmatter `name`, and draft name must be the
same value. Existing skills are discovered only from direct children of the
active workspace's `skills/` directory. A repeat create remains a collision;
only explicit update intent can update an existing skill.

Keep detailed material in a directly linked file under `references/` when the
skill needs it. Resolve every relative link from the skill directory. Reject a
missing link, an absolute link, and a link that escapes the skill directory.

Use the skill-creator initializer only in a temporary or explicitly approved
workspace. The initializer is an authoring aid, not permission to modify the
Codex runtime. The `automate_me.py` helper provides deterministic routing,
allowlisted evidence reads, collision handling, generation, and link checks.

The repository's shared port checker is structural only. It does not replace
the helper exercise, a collision test, or a review of the generated skill.

The helper's evidence reads accept only an explicit bounded allowlist, reject
duplicate canonical files and aliases, require distinct slice identities, and
validate optional recent slice timestamps. Signal confirmation requires an
exact positive structured evidence entry in at least two slices. Durable
generation also requires structured approval matching the operation and skill
name, with at least two evidence slices.

Generated trees are staged and validated in full before installation. Updates
copy the existing tree without following symlinks, preserve unrelated files
and approved content, and restore the old tree if installation fails. Markdown
links must be relative, and absolute or protocol-relative links are rejected.
