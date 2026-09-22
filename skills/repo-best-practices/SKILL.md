---
name: repo-best-practices
description: Route Showpass work to web-app, showpass-frontend, or web-k8s using current repository ownership, request-flow, and safe verification evidence. Use when a task crosses repositories or its owner is unclear.
---

# Repo best practices

Use this skill to choose the owning repository, trace a change across its boundaries, and report verification without overstating evidence. It is guidance, not authorization to edit, deploy, access production data, or contact a live service.

## Route the request

Start with the current root `AGENTS.md` in each repository that may be involved. For a quick, deterministic hint, run:

```sh
python3 skills/repo-best-practices/scripts/route_task.py "<request>"
```

The helper is a keyword hint. Exact source paths, symbols, and current repository guidance take precedence. An `unknown` or `mixed` result requires a source trace before editing.

| Request shape | Primary owner | Boundary |
| --- | --- | --- |
| Django, DRF, models, ViewSets, serializers, services, Celery, durable records, permissions, API contracts, ledger, or backend tests | `web-app` | Check the domain owner, not only the URL prefix. Postgres and backend contracts are authoritative for durable behavior. |
| React, Next.js, mobile, desktop, shared client core, repositories, service hooks, or client tests | `showpass-frontend` | Keep routes thin. Follow `API -> Repository -> Service Hook -> use-case/composition hook -> UI`; inspect the backend contract first. |
| AngularJS, Django templates, `apps/main/static`, Gulp, redirects, embedded pages, or legacy bridge code | `web-app` | This is a legacy frontend surface in the backend repository, not a reason to move the change to the modern frontend monorepo. |
| Fabric, Kubernetes, Jinja, `gcloud`, `kubectl`, ingress, rollout, monitoring, HPA, KEDA, or environment configuration | `web-k8s` | Select the environment first. Treat deployment commands as operational actions requiring explicit authorization. |
| A backend contract plus a modern client plus deployment or ingress changes | Split by owner | Trace backend contract first, then the client adapter, then deployment configuration. Include only repositories whose source actually changes. |

Do not infer ownership from a ticket label, API prefix, package name, or a neighboring repository. Read the current root guidance and the smallest relevant architecture/domain document when the helper is insufficient.

## Trace the request flow

Use this boundary map for a cross-repository change:

```text
request -> entrypoint -> web-app contract -> client adapter -> deployment/ingress
```

At the high level, the path is `request -> backend -> frontend/client -> deployment`. The longer map names the boundary artifacts to inspect.

- `request` identifies the actor, action, result, and durable effect to verify.
- `entrypoint` is the actual route, command, task, page, or deployment task. Find it by symbol and route, not by a broad keyword alone.
- `web-app` owns Django/DRF behavior, durable backend state, legacy frontend bridges, and worker/provider contracts. Its API surface does not by itself prove client behavior.
- `showpass-frontend` consumes backend contracts through repositories and service hooks, then composes use cases and UI. A client type or mock does not prove the backend serializer or permission behavior.
- `web-k8s` renders and applies infrastructure for the selected environment. A manifest or task definition does not prove that a deployment ran or that a workload became ready.

For a mixed change, record the contract and source path at every boundary. Keep backend, client, and infrastructure edits separate in the plan and in the final evidence.

## Verify without overclaiming

Run commands only in the matching checkout, record the exact revision, and report each command as `PASS`, `FAIL`, or `NOT RUN` with a reason. Narrow checks are preferred.

`web-app` local checks, selected from the changed surface:

```sh
./quick_install.sh --preflight
$PY -m conf.docs.check
python -m unittest conf.docs.test_check
bash conf/ruff/changed_files.sh
$PY manage.py test <focused.labels> --keepdb
$PY -m tools.api_surface --check
```

Use the repository's resolved Python environment for `$PY`. Backend integration behavior such as basket, checkout, permission, serializer, or database effects needs a routed integration test where applicable. Do not substitute a bare full suite for a focused proof.

`showpass-frontend` local checks, selected from the changed package:

```sh
nvm use 22.21.1
corepack enable
pnpm install
pnpm run check:changed:ci
pnpm run arch:check
pnpm run typecheck
pnpm run test --filter <focused-package>
pnpm run next:build
```

Use the least expensive command that proves the changed boundary. The build is relevant when Next configuration, routes, generated output, or production wiring changes.

`web-k8s` offline checks:

```sh
python3.9 --version
ruff check .
python3.9 -m compileall -q .
python3.9 -m tests
fab --list
git diff --check
```

Do not run deploy, cluster, `gcloud`, or `kubectl` operations from this skill without explicit target and authorization. Even a dry-run may contact a cluster. Deployed read-only tests are a separate evidence class and require the repository's stated guard.

Hosted CI is evidence only when the exact revision has passed the relevant pipeline. The web-app documents CircleCI `build_and_test`; the frontend documents GitHub build, lint, and test workflows. Documentation, local static checks, and a neighboring revision are not CI proof. The infrastructure repository's offline tests are not deployment proof. Never claim live services, a deployment, production data, provider behavior, workload readiness, or hosted CI unless that action actually ran and its result was captured.

If a command was not run, write `NOT RUN: <reason>` rather than implying success.

## Evidence register

Use the [repository evidence register](references/repository-evidence.md) for the source paths, line anchors, exact SHAs, upstream provenance, command status, and limitations captured for this skill. The classifier is at [scripts/route_task.py](scripts/route_task.py).

## Limits

The evidence register is a dated, read-only source snapshot. Refresh the repository SHA and line anchors before relying on it for a later task. Protected environment values and credentials are not evidence for routing and must not be copied into notes or output. Static source inspection does not prove runtime behavior, provider responses, deployment success, production state, or CI. The helper intentionally gives only a routing hint and cannot replace source inspection.
