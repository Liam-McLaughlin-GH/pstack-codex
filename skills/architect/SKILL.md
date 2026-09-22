---
name: architect
description: Design a small, reviewable shape before implementing a non-trivial change. Use for new boundaries, stateful logic, public interfaces, or changes that cross modules.
---

# Architect

Design the shape before writing the implementation.

1. Run `$how` over the existing paths the change will touch.
2. Name the core data types, ownership boundaries, inputs, outputs, and failure states.
3. Write a small sketch with interfaces or signatures and no implementation details.
4. Compare at least one credible alternative when the boundary or state model is not obvious.
5. Choose the smallest design that keeps callers from knowing internal rules.
6. Implement against the sketch.
7. If implementation repeatedly needs exceptions, stop and redesign the sketch instead of adding guards around a wrong shape.

The deliverable is a design that another engineer can review before code exists. Record important choices and the evidence behind them when the work is long enough to need an audit trail.
