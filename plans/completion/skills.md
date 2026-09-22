# Skill assignments

Read only relevant skills, using their available installed paths. A skill listed as planned or under development cannot be assumed installed. Resolve pstack skills directly from this checkout and inspect before trusting them; the current abbreviated ports have not been behaviorally validated.

| Skill | Required use |
| --- | --- |
| focused-agent-goal | Coordinator goal envelope and persisted result packets |
| task-model-routing | Dispatch model, effort, escalation and usage accounting |
| github-tools | Coordinator issue/PR reads and authorized publication |
| skill-creator | Every lane's SKILL.md structure, supporting resources and validation |
| avoid-ai-writing | Final composition pass on instructions, docs and PR bodies |
| openai-docs | #8 runtime/install/agent contracts; verify current official sources |
| skill-installer | #8 isolated installation design and collision checks |
| research-codebase | #10 source-backed reconnaissance; reuse for #9 |
| web-app-safe-tests | Only if #9/#10 need to run backend checks; do not assume a production-safe command |
| playwright-cli | #5 browser proof if that adapter is selected and available |
| code-review | Independent batch review |
| herdr | Only if dispatch uses Herdr; read before operating sessions |
| plugin-creator | Supplemental agent/plugin packaging only if packaging chosen |

Optional pstack references: how for source tracing, architect for the shared contract, swarm for disjoint scopes, show-me-your-work for evidence, tdd for executable helpers. Arena is not selected because multiple candidate generations would spend quota without a demonstrated need. Imported principle skills inform choices but never override user scope.

Issue-specific sets: #1 skill-creator; #2/#4 skill-creator plus existing feature-qa guidance if applicable; #3 focused-agent-goal/task-model-routing; #5 skill-creator/playwright-cli; #6/#7 skill-creator; #8 openai-docs/skill-installer; #9 skill-creator/research-codebase; #10 research-codebase/skill-creator. All use the composition pass. Coordinator retains external mutations.
