---
name: interrogate
description: Apply adversarial pressure to a design, plan, diff, or claim before shipping. Use when the decision is contested or the cost of a missed edge case is high.
---

# Interrogate

Try to disprove the proposed shape before it becomes expensive to change.

1. State the intended behavior and the assumptions it depends on.
2. Test the boundary cases, invalid inputs, retries, concurrency, permissions, and rollback path that apply.
3. Search for callers and consumers that would observe the change.
4. Separate confirmed defects from questions and low-impact concerns.
5. For each important concern, name the smallest executable check that could confirm or clear it.
6. Recommend action, acceptance with a reason, or a follow-up with an owner.

An adversarial review is useful only when it stays tied to the actual repository and the user's outcome. Do not manufacture risks from generic checklists.
