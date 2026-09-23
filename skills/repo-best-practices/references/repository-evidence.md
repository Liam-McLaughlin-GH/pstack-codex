# Repository evidence register

Snapshot date: 2026-09-22.

This is a concise, source-backed register for the Codex-native `repo-best-practices` skill. It records read-only observations from the named repositories at exact revisions. Line ranges are anchors for the inspected checkout; refresh them when the revision changes.

## Provenance

The pinned upstream pstack checkout was inspected at `53e579f1481697931fc44f5445171397cfa2b24b`. Its `README.md` and `LICENSE` identify the pstack project and MIT licensing. The pinned tree has no `repo-best-practices` skill path, so this is a new Codex-native skill rather than a direct port. The implementation keeps upstream provenance in this note and does not copy upstream runtime-specific instructions into the active skill.

- Upstream checkout: pinned local source checkout; its machine-local path is omitted from this published register
- Upstream revision: `53e579f1481697931fc44f5445171397cfa2b24b`
- Upstream reference: [pstack at the pinned commit](https://github.com/cursor/plugins/tree/53e579f1481697931fc44f5445171397cfa2b24b/pstack)
- License reference: [pstack LICENSE at the pinned commit](https://github.com/cursor/plugins/blob/53e579f1481697931fc44f5445171397cfa2b24b/pstack/LICENSE)

## Source identity

| Repository | Relative source root | Branch | Exact inspected SHA | Remote |
| --- | --- | --- | --- | --- |
| `web-app` | `web-app/` | `SPW-20615-stripe-terminal-backfill-audit-report` | `f7884b9e876acc4c5dc476259b241fe8bf677664` | `showpass/web-app` on Bitbucket |
| `showpass-frontend` | `showpass-frontend/` | `develop` | `0aae56009bb1aabe09942adb04f6aa4782f98450` | `showpass/showpass-frontend` on GitHub |
| `web-k8s` | `web-k8s/` | `master` | `daa154cab26375c721251c0d6e74717c79c563ae` | `showpass/web-k8s` on Bitbucket |

The initial web-app source capture was at `508b22a3fa7a25ef5cdac465a29195117f3f97cc` on `develop`. A later read-only identity check observed that checkout at `f7884b9e876acc4c5dc476259b241fe8bf677664` on `SPW-20615-stripe-terminal-backfill-audit-report`; the cited AGENTS, architecture, domain, testing, and CircleCI files were unchanged between those SHAs. The table uses the final observed checkout SHA. The current checkout state and root `AGENTS.md` must be rechecked before any future edit. This worker did not edit, test, deploy, or push any of the three repositories.

## Ownership and boundaries

| Repository rule | Exact source evidence | SHA |
| --- | --- | --- |
| Backend ownership is Django/DRF, Celery, legacy UI/bridges, and backend contracts. API prefixes are not ownership boundaries. | `web-app/AGENTS.md:3-5,9-15,31-36`; `web-app/ARCHITECTURE.md:26-53,92-102,116-120` | `f7884b9e876acc4c5dc476259b241fe8bf677664` |
| The web-app request/runtime path separates browser, DNS, Nginx, Next.js, static/media, and Django. Backend-first tracing is required. | `web-app/ARCHITECTURE.md:62-76,116-120`; `web-app/docs/domains/api_router_topology.md:5-10,23-24,48-69` | `f7884b9e876acc4c5dc476259b241fe8bf677664` |
| Legacy public/dashboard/SDK surfaces and bridges stay in web-app; modern UI is in the separate frontend repository. | `web-app/docs/domains/frontend_public_dashboard_and_sdk.md:3-37,39-49`; `web-app/ARCHITECTURE.md:9-17` | `f7884b9e876acc4c5dc476259b241fe8bf677664` |
| Modern frontend ownership is shared core plus Next, mobile, and desktop packages. New feature logic belongs in core, not a route or page component. | `showpass-frontend/AGENTS.md:26-35,171-205`; `showpass-frontend/ARCHITECTURE.md:20-45,146-184` | `0aae56009bb1aabe09942adb04f6aa4782f98450` |
| Frontend data flow is backend API to Repository to Service Hook to use-case/composition hook to UI; routes are thin and the backend ViewSet is the contract source. | `showpass-frontend/ARCHITECTURE.md:104-142,195-205,272-279`; `showpass-frontend/packages/next-app/ARCHITECTURE.md:3-24` | `0aae56009bb1aabe09942adb04f6aa4782f98450` |
| Infrastructure owns Fabric3 task registration, `demo`/`beta`/`live` environment selection, Jinja rendering, Kubernetes operations, Django rollout, Next.js rollout, ingress, and monitoring boundaries. | `web-k8s/AGENTS.md:5-15,30-40`; `web-k8s/README.md:58-106`; `web-k8s/deploy/__init__.py:11-28,37-60` | `daa154cab26375c721251c0d6e74717c79c563ae` |
| Django and Next rollouts are separate operational paths. `kubectl apply` and rollout helpers can mutate infrastructure. | `web-k8s/AGENTS.md:58-69`; `web-k8s/README.md:72-106,153-169`; `web-k8s/deploy/web/__init__.py:341-353,386-417,438-505`; `web-k8s/deploy/web/frontend/__init__.py:32-79,135-187`; `web-k8s/deploy/kubectl.py:40-90` | `daa154cab26375c721251c0d6e74717c79c563ae` |

## Request to deployment flow

| Boundary | What to inspect | What it does not prove |
| --- | --- | --- |
| Request and entrypoint | Actor/action/result, route or command, model/ViewSet/task/page/deploy task, and the root guidance in the owning repository | A ticket title or API prefix does not identify ownership. |
| Backend contract | web-app view/router, serializer, permission, service/task, durable record, and focused routed test | A backend test does not prove that a client renders or that deployment succeeded. |
| Client adapter | showpass-frontend Repository, Service Hook, use-case/composition hook, route/page, and focused test | Client types, mocks, or a page build do not prove the backend contract or provider behavior. |
| Deployment and ingress | web-k8s environment task, Jinja/template, workload/ingress, rollout path, and offline/deployed test guard | A rendered manifest or `fab --list` does not prove a cluster mutation, readiness, live traffic, or production state. |

## Verification command register

These are source-documented commands, not a claim that this worker ran them in the named repositories. The named repositories were read-only for this issue, so repository setup, repository tests, deployment checks, and hosted CI are `NOT RUN` here.

| Repository | Command family | Source evidence | Worker status |
| --- | --- | --- | --- |
| `web-app` | `./quick_install.sh --preflight`; `$PY -m conf.docs.check`; `$PY -m unittest conf.docs.test_check`; focused Django test; changed-file Ruff; API surface check | `web-app/README.md:3-16,25-68`; `web-app/AGENTS.md:40-85`; `web-app/docs/standards/backend_testing.md:23-49,92-108` | `NOT RUN: read-only source checkout and no backend test authorization in this worker` |
| `web-app` | Legacy frontend tests and asset rebuild/watch: `$PY manage.py gulp_test`; `$PY manage.py gulp_watch`; inspect `apps/main/management/commands/` before collection | `web-app/docs/domains/frontend_public_dashboard_and_sdk.md:45-49` | `NOT RUN: read-only source checkout and no legacy frontend command authorization in this worker` |
| `web-app` | CircleCI `build_and_test`, including test, contracts, docs, and Ruff stages | `web-app/.circleci/config.yml:329-363,536-565,699-730` | `NOT RUN: hosted CI was not invoked; no CI proof` |
| `showpass-frontend` | `nvm use 22.21.1`; Corepack/pnpm install; changed checks; architecture checks; typecheck; focused tests; Next build | `showpass-frontend/AGENTS.md:39-88,123-142`; `showpass-frontend/package.json:11-15,51-93`; `.nvmrc:1` | `NOT RUN: read-only source checkout and no frontend test authorization in this worker` |
| `showpass-frontend` | GitHub build, lint, and test workflows | `.github/workflows/build.yml:1-11,30-69`; `.github/workflows/lint.yml:30-90`; `.github/workflows/test.yml:30-71` | `NOT RUN: hosted CI was not invoked; no CI proof` |
| `web-k8s` | Activated-environment `python --version`; Ruff; `python -m compileall -q deploy tests fabfile.py settings.py`; offline tests; `fab --list`; diff check | `web-k8s/AGENTS.md:71-84`; `web-k8s/README.md:108-134`; `web-k8s/tests/README.md:3-39` | `NOT RUN: read-only source checkout and no infrastructure command authorization in this worker` |
| `web-k8s` | Deployed read-only tests or any deploy/cluster command | `web-k8s/AGENTS.md:58-69,71-100`; `web-k8s/tests/README.md:41-74` | `NOT RUN: no target, credentials, or explicit operational authorization` |

## Concrete references

- `web-app`: `ARCHITECTURE.md:62-76` defines the browser-to-Django/Next request boundary, while `docs/standards/backend_testing.md:92-108` defines routed integration proof.
- `showpass-frontend`: `ARCHITECTURE.md:195-205` defines the API-to-UI data path, and `packages/next-app/ARCHITECTURE.md:3-24` keeps route wiring thin.
- `web-k8s`: `deploy/web/__init__.py:341-353` shows the Django build/static/migrate path, while `deploy/web/frontend/__init__.py:135-168` shows the separate Next static-sync/workload path.

## Limitations

This register is read-only evidence from the exact SHAs above, not a live contract. Source paths, line anchors, commands, and ownership may drift. No live service, cluster, production database, provider, deployment, readiness, or hosted CI was accessed or proven. Protected environment values are deliberately excluded from this register. The helper and this register cannot resolve an ambiguous request without current source inspection.
