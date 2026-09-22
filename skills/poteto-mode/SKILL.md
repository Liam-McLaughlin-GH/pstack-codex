---
name: poteto-mode
description: Apply pstack's concise, evidence-led workflow discipline to substantial Codex work. Use for multi-step changes, design choices, reviews, or requests for rigorous verification.
---

# Poteto mode

Use this skill when the work needs more structure than a quick answer. Keep the process proportional to the risk.

## Before acting

1. State the outcome the user needs and the files or systems in scope.
2. Identify the data shape or artifact that will change before writing code.
3. Choose the smallest useful workflow.
   - Use `$how` to trace an existing system.
   - Use `$why` to separate evidence from design rationale.
   - Use `$architect` before a cross-boundary or stateful change.
   - Use `$swarm` when work can be split into independent, non-overlapping units.
   - Use `$arena` when competing designs are worth comparing.
   - Use `$interrogate` before shipping a contested design.
   - Use `$tdd` when a focused regression test is practical.
4. Name the verification that will prove the result.

## Working rules

- Prefer the smallest change that satisfies the outcome.
- Model the domain before scattering conditionals across files.
- Keep independent work in separate files, worktrees, or branches until it is ready to combine.
- Treat observed output, source evidence, and inference as different things.
- Do not call a change complete because it compiles. Run the check that exercises the real behavior.
- Use `$unslop` for user-facing prose, docs, commit messages, and skill instructions.
- Use `$show-me-your-work` when the task is long, autonomous, or likely to be reviewed later.

## Reply

Lead with the result. State what changed, what you verified, and what remains open. Keep uncertainty attached to the claim it qualifies. Do not claim a check you did not run.
