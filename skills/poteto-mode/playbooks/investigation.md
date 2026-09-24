### Investigation

You own the answer. Investigation is read-only and ends with a cited
explanation or a recommendation.

1. Route through $how. Add $why for motivation or historical questions.
2. Keep the throughput checkpoint to one line:
   throughput checkpoint: n/a, read-only investigation.
3. Produce the how-shaped answer with Overview, Key Concepts, How It Works,
   Where Things Live, and Gotchas. For a choice, use a tradeoff table.
4. Separate observed facts, inferences, and unknowns. Name the file, symbol,
   command, or fixture that supports each material claim.
5. Check git status and the fake side-effect log before and after. Do not
   create a branch, edit code, create a PR, send a message, or mutate data.
6. Apply $unslop to the final explanation. If the investigation is the
   prelude to a change, hand the evidence back and route to Bug fix or
   Feature. Do not begin that change in this playbook. Use this state matrix:
   a missing required capability is `BLOCKED`, an unapproved or unmet
   precondition is `NOT RUN`, and a supplied artifact that is wrong-surface or
   cannot be mapped is `INCONCLUSIVE`.

UNSUPPORTED-SEAM: external issue, chat, observability, or analytics sources are
not assumed. Record the unavailable category and use local source evidence,
explicitly supplied artifacts, or a bounded read-only connector.

Reply with an evidence report containing the investigation output, source
pointers, confidence labels, and unresolved evidence gaps. The side-effect
log must remain empty.
