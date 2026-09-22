---
name: why
description: Explain why a system or change has its current shape. Use when design rationale, history, tradeoffs, or intent are unclear.
---

# Why

Recover the reason for a design without presenting guesses as facts.

1. State the decision or behavior whose reason is unclear.
2. Gather evidence from the current code, tests, documentation, git history, and issue or review context when available.
3. Label each conclusion as observed, strongly supported, inferred, or unknown.
4. Compare the current choice with the smallest credible alternatives.
5. State which constraints still matter and which may be historical residue.
6. Say what new evidence would change the conclusion.

Do not invent a historical explanation because the current code looks intentional. If the repository does not contain the reason, say that the reason is unknown and describe the evidence you did find.
