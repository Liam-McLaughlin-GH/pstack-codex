# Showpass PR lifecycle

Use for `showpass/web-app` backend and `showpass/showpass-frontend` frontend
work. Resume at the evidenced stage. Keep a todo for PR standards, Jira,
review, local or preview QA, fixes, human review when needed, merge permission,
merge, cloud build, deployment confirmation, and Beta QA. Mark inapplicable
steps with reasons. Do not claim completion while a required step is pending.

## Scope and authority

Read the repository instructions and applicable installed skills before acting.
Load `$ponytail` full for implementation and review, `$failure-pattern-memory`,
`$web-app-pr-writing`, and the relevant fix, review, and QA skills. Reuse their
rules rather than copying templates or guessing commands.

A request to execute this PR lifecycle authorizes its scoped PR preparation,
reviewer requests and re-review triggers, QA evidence publication, and linked
Jira lifecycle updates. A title/body-only task retains that narrow scope.
Merely loading this skill grants no external messaging authority. Preserve
explicit draft-only, no-message, no-push, testing, and environment limits.
Merge requires explicit user permission for the PR and destination branch;
bot approval does not supply it. Deployment mutations and Production writes
require separate authorization. Read-only build and deployment verification
may proceed within the task. Do not inspect `web-k8s` without its own permission.

## Prepare the PR and Jira

1. Confirm the checkout, remote, source and target branches, current head,
   linked tickets, and acceptance criteria. Preserve unrelated work.
2. Make the smallest root-cause change under Ponytail. Run permitted focused
   checks before pushing and opening the review PR. Follow Opening a PR, with
   Showpass repository and `$web-app-pr-writing` rules taking precedence over
   generic title and body examples. Read back the saved title, body, branches,
   head, draft state, and intended reviewer fields.
3. Follow the shared `$codex-prompt-intake` implementation-goal contract for
   Jira lifecycle. Verify available transitions and the ticket's actual state.
   Move to In Progress when implementation starts and Code Review when the PR
   is ready and the reviewer is assigned. Verify each transition. Move later
   statuses only when the team's mapped criteria are met; a merge or build
   alone does not prove QA or Done. Track every attached card separately.

## Backend review and QA

Use `$review-unblock` and its `references/galileo-review-loop.md`. Resolve the
live Galileo account, make the PR ready unless explicitly held draft, assign
that reviewer, and read back the assignment.

After a fix is pushed, the entire re-review comment must be the provider's
resolved Galileo mention followed by ` re-review`, with no additional text.
Bitbucket raw syntax is `@{<verified-account-id>} re-review`. Resolve and verify
the account at execution time. Plain text `@galileo-agent re-review` is not a
valid substitute for a resolved Bitbucket mention. Read back raw and rendered
content. Refresh Galileo's original review comment by ID as well as inline
findings and actual provider approval.

Address verified actionable findings and repeat permitted checks, push, and
re-review until Galileo approves the current head with no unresolved blocking
findings. Then perform local acceptance and regression QA using the applicable
backend QA and safe-test skills. If QA finds an issue, fix it, rerun affected
QA, and obtain review coverage for the new head before merging.

## Frontend review and QA

Read the repository's current automation and `$pr-babysitter`. Verify the
active reviewer and trigger before posting. Use its supported label or command
for the frontend bot; use Galileo's verified mention protocol only when Galileo
is the configured reviewer. Do not copy Bitbucket mention syntax into GitHub.
Require current-head automated approval. When Galileo is required as well as
the frontend bot, satisfy both gates; one approval cannot stand in for another.

Run customer-facing acceptance and regression QA locally or on the Vercel
preview for this exact head, whichever provides the required behavior. Record
runtime identity and preview URL when applicable. Capture real screenshots
and publish them to the PR through its supported attachment mechanism, then
verify the evidence is accessible. A local filesystem path is not a shared PR
attachment. If publishing is unavailable, keep the captures and report that
step BLOCKED. Use recordings when interaction or timing needs them.

Fix QA findings and repeat affected checks, screenshots, and automated review
until the current head passes. Assess risk using the changed behavior and blast
radius. For medium or high risk, request April and another relevant developer
after verifying their live accounts and repository ownership. Repository rules
may require human review at any risk level. Record actual approvals and open
requests; a tag is not approval. Do not merge through outstanding required
human review or changes requested.

## Merge, build, deployment, and Beta QA

1. At merge-ready, refresh exact head and base, review coverage, required human
   approvals, unresolved threads, mergeability, CI, QA receipts, and Jira state.
   Preserve existing merge permission when it covers this PR and branch. If
   missing, finish the reviewable package and request only the merge decision.
2. Merge only with that permission and all required gates met. Verify the
   provider's merged state, destination branch, and resulting commit.
3. Use the permitted Google Cloud read tools to find the build associated with
   that merge commit and environment. Record build ID, source commit, final
   status, and relevant failure evidence. A green pre-merge PR check does not
   prove this build passed. For frontend work, verify the actual pipeline; if
   deployment uses Vercel instead of Google Cloud, document the evidenced
   mapping and check it. Do not invent a Google Cloud build.
4. Read deployment announcements in
   [the deployment channel](https://showpass.slack.com/archives/C0615C5QCF6)
   or verify the rollout through authorized Google Cloud reads. Match service,
   commit or image, environment, and time. Distinguish Beta from live. A build
   success or announcement alone does not prove that all serving instances
   run the change; verify runtime identity and rollout completion where the
   available access permits. Report uncertain runtime coverage explicitly.
5. After this change reaches Beta, run the acceptance and affected regression
   scenarios on Beta. Capture results and real screenshots for visible changes.
   Verify the relevant flags and runtime version. Beta writes must stay within
   authorized test accounts and data. Fix failures through the same PR/review
   cycle; do not patch an environment directly. If only live is deployed, Beta
   QA remains NOT RUN until a matching Beta deployment exists.
6. Reconcile linked Jira cards against verified review, deployment, and QA
   outcomes. Report each stage independently and link the PR and QA evidence.

## Waiting and stopping

Use bounded polling with backoff, never duplicate reviewer triggers while a
review is pending. Follow the reviewer skill's iteration limit; otherwise stop
after six fix/re-review cycles, repeated unresolved feedback, a product decision,
an unrelated CI failure, missing capability, or the task's timebox. Report the
pending stage and smallest next action. Do not treat a bounded stop as approval.

Use a supported recurring automation only when the user authorized ongoing
watching; keep it quiet until a meaningful change or required action. Do not
promise continued monitoring without an actual registered mechanism. Separate
PASS, FAIL, BLOCKED, NOT RUN, and INCONCLUSIVE for review, QA, merge, build,
deployment, and Beta QA. Finish with evidence and remaining authorization.
