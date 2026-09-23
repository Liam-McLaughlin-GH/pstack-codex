### Multi-phase or multi-PR plan

You own the plan, not the code. The plan is a checklist another owner can run
from evidence. Do not implement the planned change.

1. Skip the plan for one or two files with an obvious approach. State the
   reason and stop.
2. Settle open questions with [Prototype](./prototype.md) before writing the
   plan. Keep local branch, artifact, and observed result for each decision.
   Ask the user only for a product or preference choice no fixture can settle.
3. Explore the repository with $how and, when needed, bounded local workers.
   Each finding returns file pointers, conventions, entry points, and checks.
   Use a local fallback when delegation is unavailable.
4. Write one section per PR. Include dependencies, file boundaries, data
   shape, build steps, user-observable result, unit checks, live checks, perf
   checks, review gates, and merge or handoff rules.
5. State the execution playbook in the plan. Use Autopilot full for independent
   items with landing authorization, Autopilot stack for a reviewed chain, or
   Orchestrate for a standing program.
6. Every verification block opens with the rule that tests alone are not
   enough. A live check must name its matching local surface or say BLOCKED.
   A perf block names a metric, unit, baseline, probe, and failure number.
7. Run the available local plan checker. If it is not installed, use the
   structural contract in
   `tests/poteto_mode/fixtures/playbook_contracts.json`: every phase must name
   an owner, inputs, acceptance evidence, dependencies, and a final gate.
   Record the fixture result and fix every printed issue before handoff.
8. Stop after returning the plan path and checks. Execution starts only on an
   explicit go and still follows the side-effect gate.

Plan skeleton:

    # Program plan
    ## How to read this
    ## Program checklist
    ### Arm the program
    ### Spawn owners
    ### PR mechanics
    ### Verdict and merge
    ## Change one
    ## Change two
    ## Close the program
    ## Appendix A. Prototype evidence
    ## Appendix B. Alternatives rejected
    ## Appendix C. Risks and unsupported surfaces
    ## Appendix D. Reading list

Each change section names its own evidence. A shared check does not prove a
different phase. Review-gated interaction changes stop at merge-ready and
wait for the operator's review.

UNSUPPORTED-SEAM: remote plan checkers, cloud workers, live browser lanes,
forge operations, and video capture are not implicit. Use local fixtures or
mark the affected box BLOCKED. Do not fill a missing screenshot or verdict
with a prose claim.

Reply with the plan path, dependency graph, review-gated items, prototype
findings, unresolved risks, and checker output.
