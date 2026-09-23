---
name: python-best-practices
description: Apply practical Python guidance and source-backed Showpass Django, ORM, testing, and deployment conventions. Use when reading or editing Python, choosing a Python test or lint command, or routing work across web-app, showpass-frontend, and web-k8s.
---

<!-- Codex-native pstack skill. No python-best-practices skill exists at upstream pstack SHA 53e579f1481697931fc44f5445171397cfa2b24b. -->

# Python best practices

Use this skill to choose a small, explicit Python design and a proportionate local check. Keep general Python advice separate from repository rules. Load `references/source-ledger.json` when adding or refreshing a Showpass citation. Run the checker with `python3 skills/python-best-practices/scripts/check_skill.py` after changing the ledger or routing examples.

## Route the task

| Task | Apply | Boundary |
|---|---|---|
| General Python or an unowned Python project | General guidance below | Inspect that project's own version, lint, test, and security configuration before assuming a convention. |
| Showpass backend, Django, DRF, ORM, Celery, or `web-app/` | Django and ORM guidance below | `web-app` owns the Django backend, Celery work, and legacy UI/bridges. Source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L1-L6`. |
| Showpass Fabric, Jinja, Kubernetes, `kubectl`, or `gcloud` tooling | Deployment tooling guidance below | `web-k8s` is an operational control plane. Treat its commands as real infrastructure operations. Source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L3-L7`. |
| Showpass web, mobile, or desktop UI | Use the frontend repository guidance | **[SP-FRONTEND-BOUNDARY]** Start with the `showpass-frontend` repository guidance for its web, mobile, and desktop apps built with React, Next.js, and React Native. Framework-only React or Next.js component requests without a Showpass or repository cue stay general until the project is identified. **[SP-FRONTEND-PACKAGES]** Explicit `packages/core`, `packages/next-app`, `packages/mobile`, and `packages/desktop` cues route to `showpass-frontend` even without a framework cue. Sources: `showpass-frontend@0aae56009bb1aabe09942adb04f6aa4782f98450:showpass-frontend/AGENTS.md#L3-L7`; `showpass-frontend@0aae56009bb1aabe09942adb04f6aa4782f98450:showpass-frontend/AGENTS.md#L28-L35`. |

Requests that combine backend cues such as `Django` or `web-app/` with frontend cues such as `Next.js` or `client` are `ambiguous`. Trace the source before choosing an owner. **[SP-WEB-LEGACY-ROUTING]** Give bridge, redirect, and bridge-route cues precedence for `web-app`: a bridge-only Showpass Next.js request stays with `web-app`, while a distinct modern UI cue makes the request ambiguous and keeps both owners. Sources: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L3-L5`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/domains/frontend_public_dashboard_and_sdk.md#L16-L30`. **[SP-K8S-SAFETY]** `Fabric3`, `Fabric 3`, and natural-language Fabric environment operations, as well as generic `production` or `live` operations, are `unsafe/manual-review`, even when another repository cue is present; the documented read-only exception is `fab --list`. Sources: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L5-L7`; `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L58-L67`; `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L96-L100`.

## General Python guidance

- Make invalid states hard to construct. Use explicit parameters, useful return annotations at public boundaries, small functions, and names that describe the domain operation.
- Keep side effects at the edge. Pass dependencies into functions or small classes instead of hiding global state. Use context managers for files, locks, transactions, and other resources.
- Validate untrusted input once at the boundary, then pass a typed value inward. Do not use `eval`, `exec`, shell-string concatenation, or interpolated SQL for input handling.
- Raise errors that describe the failed operation. Preserve the cause with `raise NewError(...) from exc`; translate to HTTP, task, or CLI responses only at that boundary.
- Avoid mutable defaults, broad catches without a reason, import cycles, duplicate configuration, and abstractions that do not own a real boundary.
- Test observable behavior and failure paths. Isolate time, randomness, network, filesystem, and external services. Use the repository's real test primitives when they are cheap and safe; mock only the boundary that cannot run locally.

```python
# Safe: validate a value without executing it.
def parse_page(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError("page must be a positive integer")
    return raw


# Unsafe: input becomes executable Python.
def parse_page(raw: str) -> int:
    return eval(raw)  # Never use for request, file, or environment input.
```

## Verified Showpass conventions

These rules are repository facts, not general Python defaults. Re-check the cited source before changing a convention.

### Django and ORM

- **[SP-WEB-ENV]** Use the `web-app` environment's selected Python executable. Its agent guide requires Python 3.12 and Django 5.2.15; the setup guide pins the bootstrap interpreter to Python 3.12.10. Sources: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L38-L44`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/README.md#L49-L58`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/requirements.txt#L75-L75`.
- **[SP-WEB-OWNERS]** Keep models, managers/querysets, services, cross-model queries, task adapters, serializers, and signal handlers in their documented owners. Follow the existing app capability map before adding a helper. Source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/code_style.md#L364-L378`.
- **[SP-WEB-LIFECYCLE]** Put universal instance invariants in `save()` only after accounting for callers that bypass it, including `QuerySet.update()`, bulk writes, raw SQL, and cascades. Use `clean()` for reusable model validation, but remember that DRF `ModelSerializer` does not call `clean()` or `full_clean()` automatically. Source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/code_style.md#L22-L39`.
- **[SP-WEB-TRANSACTION]** Put transaction boundaries and row locks on the application owner. Use `atomic_if_needed` when save-time related writes need one rollback unit, `select_for_update()` when a locked re-fetch is part of the invariant, and `transaction.on_commit()` for an accepted post-commit handoff. Use an outbox when the obligation must survive process failure. Source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/code_style.md#L41-L58`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/code_style.md#L184-L199`.
- **[SP-WEB-QUERY]** Put composable model-relative query methods on a queryset or manager, reusable cross-model/raw-SQL/OLAP plans under the app's `queries/` package, and action-only eager graphs in `ViewSet.get_queryset()`. Keep querysets lazy, deterministically ordered, and stable for pagination. Parameterize raw SQL through `retrieve_from_query` or `retrieve_one_from_query`. Source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/code_style.md#L132-L162`.
- **[SP-WEB-SERVICE]** Use stateless, class-based services with one public command identity by default. Services receive domain inputs, own application orchestration, and return transport-free results. Models do not import services. Source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/code_style.md#L166-L199`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/service_design.md#L92-L105`.
- **[SP-WEB-CELERY]** Dispatch ordinary Celery work through `call_task` from `core.tasks.base`, not direct `.delay()` or `.apply_async()`. Retain direct canvas dispatch only when an established adapter has focused proof for its protocol. Source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/code_style.md#L225-L237`.
- **[SP-WEB-IMPORTS]** Use the project-owned translation and logging wrappers in application code rather than importing Django translation or `logging.getLogger` directly. Source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/code_style.md#L338-L360`.

Safe and unsafe ORM shapes:

```python
# Safe shape for a locked, multi-write application operation.
with transaction.atomic():
    basket = TicketBasket.objects.select_for_update().get(pk=basket_id)
    invoice = basket.record_payment()
    transaction.on_commit(lambda: call_task(send_receipt, invoice.pk))


# Unsafe assumption: update() bypasses model save(), validation, and signals.
Ticket.objects.filter(pk=ticket_id).update(status=new_status)
```

### Tests and lint

- **[SP-WEB-TEST]** Run the narrowest labeled Django test. The repository forbids an unlabeled local `$PY manage.py test`; use a method, class, focused module, or app package as the scope requires, and inspect fixtures before using `--keepdb`. Sources: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L63-L71`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/backend_testing.md#L23-L43`.
- **[SP-WEB-RUFF]** Use the repository Ruff configuration, which targets Python 3.12 and pins Ruff 0.15.19. Use the changed-file wrapper while iterating and the CI wrapper before a PR or when fixing the Python lint gate. Sources: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/ruff.toml#L1-L12`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L73-L85`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/conf/ruff/changed_files.sh#L1-L16`; `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/conf/ruff/ci.sh#L1-L16`.

## Deployment tooling and safety

- **[SP-K8S-VERSION]** Keep `web-k8s` compatible with Python 3.9.x, known-good at 3.9.23, and `Fabric3==1.14.post1`. Sources: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L52-L56`; `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/README.md#L12-L28`; `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/requirements.txt#L1-L6`.
- **[SP-K8S-OFFLINE]** Prefer the offline test runner for local proof. It blocks sockets and process execution, and task-body tests must mock operational dependencies. Use `FabricTaskTestCase` for task bodies and `TemplateTestCase` for in-memory template rendering. Sources: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L71-L90`; `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/tests/README.md#L10-L39`.
- **[SP-K8S-CONTEXT]** Environment selection is part of the operation. `fab beta <task>` selects an environment, Fabric captures the cluster context, and every kubectl call is pinned to it. A server-side dry run still contacts the selected cluster. Sources: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L19-L28`; `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L58-L69`.
- **[SP-K8S-DEPLOYED]** Deployed tests are read-only HTTPS checks against a selected environment, not offline tests. Production checks require explicit live-safe metadata and a targeted test. Direct network clients, Fabric, `gcloud`, `kubectl`, and mutating infrastructure operations are forbidden in that framework. Sources: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L88-L100`; `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/tests/README.md#L41-L74`.

### Command status

`VERIFIED / NOT RUN` means the command is documented at the cited source, but was not executed while authoring this skill. `UNSAFE: ... / NOT RUN` means the command is shown only to mark a hard boundary. Do not turn an unsafe example into a test.

| Status | Command | Route and source |
|---|---|---|
| `VERIFIED / NOT RUN` | `$PY --version` | `web-app`; source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L38-L40` |
| `VERIFIED / NOT RUN` | `$PY -c 'import django; print(django.get_version())'` | `web-app`; source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L38-L40` |
| `VERIFIED / NOT RUN` | `bash conf/ruff/changed_files.sh` | `web-app`; source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L73-L85` |
| `VERIFIED / NOT RUN` | `bash conf/ruff/ci.sh` | `web-app`; source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L73-L85` |
| `VERIFIED / NOT RUN` | `$PY -m conf.docs.check` | `web-app`; source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L73-L79` |
| `VERIFIED / NOT RUN` | `$PY manage.py test financials.tests.api.venue_based.invoices.test_api_venue_based_invoices.VenueBasedInvoiceViewSetBarcodeFilterTestCase --keepdb` | `web-app`; source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/AGENTS.md#L63-L71` |
| `UNSAFE: unlabelled broad Django suite / NOT RUN` | `$PY manage.py test` | `web-app`; source: `web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:web-app/docs/standards/backend_testing.md#L23-L34` |
| `VERIFIED / NOT RUN` | `python --version` | `web-k8s`; source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/README.md#L17-L28` |
| `VERIFIED / NOT RUN` | `ruff check .` | `web-k8s`; source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L71-L86` |
| `VERIFIED / NOT RUN` | `python -m compileall -q deploy tests fabfile.py settings.py` | `web-k8s`; source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L71-L86` |
| `VERIFIED / NOT RUN` | `python -m tests` | `web-k8s`; source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/tests/README.md#L1-L23` |
| `VERIFIED / NOT RUN` | `fab --list` | `web-k8s`; source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/README.md#L45-L51` |
| `VERIFIED / NOT RUN` | `git diff --check` | `web-k8s`; source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/AGENTS.md#L77-L84` |
| `UNSAFE: contacts a real environment / NOT RUN` | `python -m tests deployed --env live` | `web-k8s`; source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/tests/README.md#L64-L74` |
| `UNSAFE: cluster mutation / NOT RUN` | `fab beta web.rolling_update` | `web-k8s`; source: `web-k8s@daa154cab26375c721251c0d6e74717c79c563ae:web-k8s/README.md#L72-L88` |

Safe local proof should stop at static inspection, `py_compile`, source-documented offline tests, or a narrowly scoped test whose fixtures and dependencies are understood. Do not run Django services, deployment tasks, production queries, or data mutations from this skill without an explicit, separately scoped authorization.
