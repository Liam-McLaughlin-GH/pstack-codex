---
name: architect
description: Design a small, reviewable shape before implementing a non-trivial change. Use for new boundaries, stateful logic, public interfaces, or changes that cross modules.
---

# Architect

Design the shape before implementation. Keep the sketch small enough to
review and specific enough to reject a wrong implementation.

## Start

Open a todo list with Ground, Sketch, Agree, Implement, and Scrap. Keep a
skipped phase visible with its reason.

## Ground

Run $how over every existing path the change touches. Run $why when the
design changes ownership or layering and history may be a constraint.

## Sketch

Name data types, ownership boundaries, inputs, outputs, invariants, failure
states, and callers. Write signatures or pseudocode with no implementation
details. Use $arena when two shapes could lock in different public surfaces.
Compare at least two structurally different candidates when the boundary is
not obvious. Use [design-red-flags.md](references/design-red-flags.md) and
[rationale-template.md](references/rationale-template.md) to review each one.

Prefer the smallest interface that keeps callers from knowing internal rules.
The rationale records the usage sketch, selected shape, rejected alternative,
and the evidence behind the choice. See
[runner-prompt.md](references/runner-prompt.md) when delegation is available.

## Agree

Proceed directly unless the user asks for a checkpoint. A checkpoint presents
the selected sketch and stops before implementation. A missing delegation
surface does not block local design work.

## Implement

Implement against the sketch. Review every deviation. A new parameter,
escape hatch, repeated branch, or shared lock is evidence that the sketch may
be wrong.

## Scrap

If two deviations share a shape, stop adding guards. Re-run $how, subtract the
wrong structure, redesign from first principles, and verify again.

The deliverable is a design another engineer can review before code exists.
Record the choice, alternatives, evidence, and unresolved boundary.

UNSUPPORTED-SEAM: no named model pool or remote design runner is assumed. Use
local candidate files and a sequential fallback, or mark that lane BLOCKED.
