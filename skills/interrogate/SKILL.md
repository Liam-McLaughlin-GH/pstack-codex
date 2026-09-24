---
name: interrogate
description: Apply adversarial pressure to a design, plan, diff, or claim before shipping. Use when the decision is contested or the cost of a missed edge case is high.
---

# Interrogate

Run independent adversarial reviews against one prompt and rubric. The
deliverable is a synthesized verdict. Do not auto-apply changes.

## Scope and intent

Use the user-supplied files or the current local diff. State the intended
behavior, the changed paths, and the assumptions a pass depends on. Package
the diff with nearby callers, tests, types, and boundary docs.

## Review protocol

1. Build one prompt and rubric for every reviewer. Include invalid inputs,
   retries, concurrency, permissions, rollback, data shape, and matching
   surface when they apply.
2. Use one reviewer per available model or worker capability. If the model
   pool is unavailable, run a bounded sequential fallback. A missing reviewer
   is recorded as BLOCKED.
3. Give every reviewer the same scope and no write permission. Use
   [reviewer-prompt.md](references/reviewer-prompt.md),
   [rubric.md](references/rubric.md), and
   [code-quality-review.md](references/code-quality-review.md).
4. Parse and deduplicate findings. Mark consensus, lone findings, explicit
   disagreements, and missing evidence.
5. Apply the lead judgment from
   [lead-judgment.md](references/lead-judgment.md). Classify every finding as
   Act on, Consider, Noted, or Dismissed with the reviewer labels and reason.
6. Recommend the smallest executable check for every open concern. The parent
   owns implementation. No reviewer changes files.

## Verdict

Return intent, reviewer table, Act on, Consider, Noted, Dismissed, agreement
map, open checks, and no-auto-apply confirmation. Keep the final verdict tied
to the repository and user's outcome. Do not manufacture generic risks.

UNSUPPORTED-SEAM: no named reviewer model pool is assumed. A local sequential
review is valid evidence for its scope, but an absent independent lane remains
BLOCKED.
