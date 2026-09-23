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
Validate links in every bundled/reference Markdown file, using descriptor-
relative no-follow reads for the linked file and its parent components.

Use the skill-creator initializer only in a temporary or explicitly approved
workspace. The initializer is an authoring aid, not permission to modify the
Codex runtime. The `automate_me.py` helper provides deterministic routing,
allowlisted evidence reads, collision handling, generation, and link checks.

The repository's shared port checker is structural only. It does not replace
the helper exercise, a collision test, or a review of the generated skill.

The helper's evidence reads accept only an explicit bounded allowlist, use one
canonical lexical path representation, descriptor-relative `O_NOFOLLOW`
opens, and descriptor/path revalidation. They reject duplicate canonical files
and aliases, unsafe parent components, absolute escapes, require distinct
slice identities, and validate optional recent slice timestamps. Signal
confirmation requires an exact positive structured evidence entry in at least
two slices.

Before durable generation, the preference interaction must contain one or two
questions with four to six options each, support multi-select where
appropriate, identify a focused follow-up when present, and include a
free-form response. Incomplete and declined responses are refused; only an
accepted response matching the operation and skill name can authorize
generation.

Generated trees are staged and validated in full before installation. Output
must be a direct `skills/<name>/` directory. Create publication reserves the
destination with no-replace semantics, and an exclusive workspace lock
serializes create/update read-merge-publication cycles. Updates copy the
existing tree without following symlinks, preserve unrelated files and
approved content, and restore the old tree if installation fails. Markdown
links must be relative, absolute or protocol-relative links are rejected, and
dangling discovery aliases are refused.

Source commit: `53e579f1481697931fc44f5445171397cfa2b24b`

MIT License

Copyright (c) 2026 Lauren Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
