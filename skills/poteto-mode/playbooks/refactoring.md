### Refactoring

You own the behavior contract. The structure changes and the observed result
does not.

1. Run $how over the affected subsystem. Capture a characterization test,
   snapshot, or equivalence harness before moving structure. Type checking is
   not the pin.
2. Name the target shape with $principle-model-the-domain and
   $principle-foundational-thinking. The change should remove branches,
   invalid states, or reader load. It should not add an abstraction without a
   reason.
3. Run $architect for a cross-function boundary. Record the module layout,
   data types, callers, and failure states before the move.
4. Subtract first. Delete dead code, one-caller wrappers, redundant
   validators, and orphan references before adding the target shape.
5. Move in small green steps. Migrate every caller before deleting an old
   internal API. Do not keep a compatibility path unless the contract
   requires it. Check strings, tests, documentation, and back-references.
6. Prove equivalence on the real fixture. Diff old and new outputs or replay
   a recorded input. A compile-only pass is insufficient.
7. Measure reader load. If the change does not make the path easier to trace,
   revert it. Sequence subtraction, reshape, and cleanup into checked units.
8. Route the ordered local handoff through [Opening a PR](./opening-a-pr.md).
   Remote operations remain independently gated.

UNSUPPORTED-SEAM: a missing runtime surface limits the equivalence claim. Use
the strongest local fixture available and mark the result INCONCLUSIVE when it
does not exercise the actual user path.

Reply with old and target shapes, characterization pin, equivalence result,
reader-load change, kept and reverted units, and evidence paths.
