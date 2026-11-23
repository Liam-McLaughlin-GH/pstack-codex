---
name: weekly-project-update
description: Draft a Weekly Project and Operational Update from recent work and the user's plans. Use for weekly engineering manager updates that cover project status, deadline health, next outcomes, and non-project work.
---

# Weekly Project and Operational Update

<!-- writing-pass: avoid-ai-writing -->

Use the reporting period and the user's latest corrections. Review their previous updates for voice and continuity. Check relevant messages, tickets, or pull requests for last week's outcomes, dependencies, and this week's plans when available. Use the original template if the user provides another version.

Repeat the project section for every active project where the user has a meaningful contribution. A developer lead reports the project's Dev Complete deadline. A contributor reports their own deliverable and labels wider risks as observations. Keep Dev Complete and deployment dates distinct. Use Unknown for an unconfirmed deadline or health assessment. Describe review, validation, and deployment at the stage the evidence supports.

Deadline health:

- Green: The date is achievable and there are no material unmanaged risks.
- Yellow: A risk, dependency, or decision could cause the date to slip.
- Red: The current plan is expected to miss the date and needs replanning.
- Unknown: There is insufficient information to assess the date.

Retain all reporting sections and field labels. Leave the instructional prompts out of the finished copy. Use None for confirmed absences and a short qualifier when information is missing. Give one to three next outcomes per project with owner and target date. In Non-project work, include meaningful operational work and any impact on project capacity. Use Final signal to state the main priority and any action needed from the recipient.

Apply avoid-ai-writing in composition mode as the final pass when available. Preserve the template labels, facts, uncertainty, and the user's voice. Return the finished message for the user to send.

```text
Weekly Project and Operational Update
Week starting: [date]

Availability or capacity changes: [None / describe briefly]

At a glance
* Projects with a deadline at risk: [None / list]
* Decisions or support needed this week: [None / list]
* Non-project work affecting project capacity: [None / describe]

Project updates

Project: [Project name]
* My role: [Developer lead / Contributor]
* Dev Complete deadline: [YYYY-MM-DD / Unknown]
* Deadline health: [Green / Yellow / Red / Unknown]

Work moved
* [Outcome, decision, or validation since the last update]

Risks to deadlines
* [Risk and what it could delay]

Alignment and assistance needed
* [Decision, clarification, dependency, review, or manager action]

Plan until the next update
* [Outcome] - [owner] - [target date]

Non-project work
* [Work, next step, impact, and support needed]

Final signal
* [Most important point and any action needed from the recipient]
```
