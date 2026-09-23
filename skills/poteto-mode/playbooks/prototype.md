### Prototype

You own the design decision, not production code. The scratch artifact is
throwaway and exists to answer one stated question.

1. State the decision. Name the layout, interaction, density, behavior,
   timing, or other empirical fork. Without a decision, route to Feature or
   Architect.
2. Gather local references when the design space is open. Record any missing
   external source instead of inventing prior art.
3. Build in a temporary scratch directory outside production source. Use the
   smallest local HTML, script, or fixture that can observe the choice.
4. Put alternatives behind one local switcher when comparing them. Label
   each variant outside the candidate-visible data when blinding matters.
5. Observe the matching surface. Capture screenshots only when an available
   image surface produces them. For behavior or timing, retain command output
   or measured logs.
6. State alternatives, tradeoffs, and recommendation. Hand the chosen
   direction to Feature or Architect. Do not copy scratch code into
   production without a new implementation and verification pass.
7. Confirm no production file, baseline, forge, deployment, message, or
   repository path changed.

UNSUPPORTED-SEAM: browser, image capture, and live device surfaces may be
absent. Use a local behavior fixture or report BLOCKED. Never claim visual
parity from a text-only inspection.

Reply with variants, observed evidence, tradeoffs, recommendation, scratch
path, and the statement that the artifact is throwaway.
