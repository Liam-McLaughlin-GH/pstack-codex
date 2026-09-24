### Trace forensics

You own the diagnosis from a fixed artifact. Load it, make it queryable,
narrow it, and attribute it to source. Do not rerun the capture.

1. Identify the format and load it with the local parser available for that
   format. If no parser exists, report BLOCKED.
2. Transform the raw artifact into a queryable local representation. Use one
   row per sample, frame, node, or event. Keep the raw capture unchanged.
3. Query the dominant frames, call tree, retainer chain, or wait reason.
4. Map the finding to file, symbol, and line using the artifact's own symbols.
   A frame with no source mapping is not a confirmed diagnosis.
5. Compare a paired capture when supplied. Without one, call the result the
   strongest hypothesis supported by the artifact.
6. Hand back a cited diagnosis with no fix. Route to Bug fix or Perf issue
   only after the cause is known. Use this state matrix: a missing parser is
   `BLOCKED`, a supplied artifact with a wrong format, wrong surface, or no
   source map is `INCONCLUSIVE`, and an unapproved or unmet capture
   precondition is `NOT RUN`. Stop with the trace report. Keep the throughput checkpoint:
   throughput checkpoint: n/a, read-only forensics.

UNSUPPORTED-SEAM: a missing parser or source-map adapter limits the claim.
Do not invent a hot frame, retainer, or line number.

Reply with artifact and format, reduced finding, source mapping, raw and
queryable paths, paired-capture result, and confidence state.
