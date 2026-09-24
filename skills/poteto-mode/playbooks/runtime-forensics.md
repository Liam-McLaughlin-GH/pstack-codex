### Runtime forensics

You own the diagnosis. Instrument an available live process and return a
cited finding, not a fix.

1. Capture the live signal on the matching surface. Name the CPU, heap, event,
   or render artifact and the exact capture command.
2. Reduce the artifact to the smoking gun. Keep only the hot function,
   retainer chain, or repeated event in the main record. Store raw data in a
   local artifact path.
3. Prove the mechanism with the least invasive local instrumentation. Do not
   modify production source for a diagnostic unless the user explicitly asks
   for a temporary fixture change and the rollback is recorded.
4. Map the finding to file, symbol, and line. If the artifact has no source
   mapping, say so.
5. Keep the throughput checkpoint to one line:
   throughput checkpoint: n/a, read-only forensics.
6. Hand the diagnosis to Bug fix or Perf issue. Do not apply a fix in this
   playbook. Use this state matrix: an absent required live surface is
   `BLOCKED`, a supplied artifact from the wrong surface or without source
   mapping is `INCONCLUSIVE`, and an unapproved or unmet capture precondition
   is `NOT RUN`. Stop with the diagnostic report.

UNSUPPORTED-SEAM: no live process, browser protocol, device, or profiler
adapter is assumed. A supplied capture can be handled by Trace forensics, but
it is not live evidence. Report BLOCKED when no matching artifact exists.

Reply with signal, reduced finding, mechanism proof, source location, artifact
paths, and whether the evidence is confirmed or only a supported hypothesis.
