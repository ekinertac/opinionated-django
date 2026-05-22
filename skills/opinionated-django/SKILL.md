---
name: opinionated-django
description: Index + quick reference for the opinionated-django stack. 16 skills, routing table, inline rules. Read first. Answers most "what's the convention?" questions. Invoke deep skill only when this doesn't cover it. Use at start of any task in an opinionated-django project.
allowed-tools: Read, Grep, Glob, Bash
---

# Opinionated Django — Index

Entry point. Most questions answer from this page. Invoke deep skill only when this doesn't cover.

## Stack

```
API     → DRF ViewSet                      (django-api)
Service → svcs DI, _item(s) naming         (django-services)
DTO     → Pydantic v2, from_attributes
Repo    → ORM lives here, returns DTOs     (django-repositories)
Model   → BaseModel + BigAutoField         (django-models)
Signals → Celery, on_commit                (django-signals)
```

Dev: Docker Compose (django-docker). Prod: Ansible + self-hosted (django-deploy).

## Project Layout

```
src/
  manage.py
  config/
    settings/{base,local,production}.py
    services.py      # svcs registry + get[T]()
    api.py           # ServiceMixin + validate + dto_response
    models.py        # BaseModel
    signals.py       # ReliableSignal
    types.py         # AuthedRequest
    exception_handler.py
    urls.py wsgi.py asgi.py celery.py
  apps/<app>/
    models.py admin.py
    dtos.py repositories.py
    services.py signals.py receivers.py
    serializers.py views.py urls.py
    tests/{test_repo,test_service,test_api}.py
```

## Routing

| Task | Skill |
|---|---|
| New project | django-scaffold → django-docker |
| Whole feature end-to-end | django-architecture |
| Just model | django-models |
| Just repo | django-repositories |
| Just service | django-services |
| API surface (JSON, DRF) | django-api |
| HTML pages (templates + htmx + Alpine) | django-templates |
| Async side effect | django-signals |
| Settings edit | django-settings |
| Tests | django-pytest |
| Caching | django-cache |
| Email | django-email |
| Pre-commit | django-lint |
| Deploy / Ansible | django-deploy |
| GitHub Actions | django-ci |
| Destructive migration | django-migrations-scale |

## Rules — Models

- Inherit `config.models.BaseModel` (timestamps only — never bloat).
- Member order: choices → fields → manager (rare) → Meta → methods.
- Field order: identifiers → time → status → domain → relations.
- BigAutoField PK (Django default). No explicit PK.
- No business logic. No `save()` override. No custom manager. No computed `@property`. `__str__` only.
- Uniqueness → `Meta.constraints`. Never `unique=True` on field.
- Indexes → `Meta.indexes`. Never `db_index=True` on field.
- Multi-word field → explicit `verbose_name`.
- INSTALLED_APPS short path: `"apps.products"`, not `".apps.ProductsConfig"`.

## Rules — Repositories

- Only layer with `.objects` / model imports.
- Primitives in (`pk: int`), DTOs out.
- `LookupError` on missing row.
- `@transaction.atomic` on multi-writes.
- `select_related` forward FK / `prefetch_related` reverse + M2M, for nested DTOs.
- Naming: `get_by_*` raises, `list_*` list, `count_*` int, `exists_*` bool, `create` / `update` / `delete`, `bulk_*`.
- Pagination shapes: `Page[T]` offset, `CursorPage[T]` cursor.

## Rules — Services

- Zero ORM. Zero model imports.
- Repos via `__init__`. Never instantiate inside.
- Resource services: ALL methods use `_item(s)` suffix. `list_items`, `get_item`, `create_item`, `update_item`, `delete_item`, `archive_item`, `restock_item`, `bulk_create_items`. No `archive_product`. Class already names resource.
- Non-resource services (notification, payment, search): action verbs (`send`, `charge`, `query`). No `_item`.
- Take `user_id: int`, never `request.user`.
- Raise plain Python: `ValueError` → 400, `LookupError` → 404, `PermissionError` → 403.
- Wired via svcs registry. `get(MyService)` to resolve.

## Rules — API

- Serializers INPUT only. Never `ModelSerializer`. Never shape output.
- Output: `dto.model_dump()`. Via `dto_response()` helper.
- Resource ViewSet: `class FooViewSet(ServiceMixin, viewsets.ViewSet)` + `service_class` + `create_serializer` + `update_serializer`. CRUD actions free.
- Override action method to customize. Never config knobs on ServiceMixin. Never new mixins.
- Never `ModelViewSet` / `GenericViewSet` with queryset.
- Two-tier perms: DRF `permission_classes` (request-level: auth, role) + service `PermissionError` (data-level: ownership).
- URL versioning: `/api/v1/`.
- Throttling: `AnonRateThrottle` + `UserRateThrottle` global; `ScopedRateThrottle` on login/signup/password-reset.
- OpenAPI: `@extend_schema(responses={...: DTO.drf_serializer})`. drf-pydantic bridges DTO → schema.
- File upload < 10MiB: `MultiPartParser` + serializer validation. Large: S3 signed URLs.

## Rules — Templates (HTML via htmx + Alpine)

Peer to django-api. Both layers can coexist (DRF for `/api/v1/`, templates for `/products/`).

- Views function-based. `@login_required` per view (not via global default).
- No ORM in views. `get(SomeService)` for every call.
- Forms: `forms.Form`. Never `ModelForm`. Distinct Create + Update.
- Templates in `src/templates/<app>/`. Partials prefixed `_`.
- `base.html` carries `hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'` for global CSRF on htmx.
- htmx: detect via `request.htmx` (from `django-htmx` middleware). Partial template for htmx, full page + redirect for non-htmx POST.
- `HX-Trigger` / `HX-Redirect` response headers for client events / redirects.
- Alpine.js ONLY for client-side UI state (dropdowns, modals, tabs). NEVER data fetching — that's htmx.
- `ServiceExceptionMiddleware` (peer to DRF exception handler) maps service exceptions to HTML responses. Lives after auth + Htmx middleware.
- Two-tier perms: `@login_required` / `@permission_required` (request-level) + service `PermissionError` (data-level).
- Pagination: server-rendered, htmx "load more" via `hx-trigger="revealed"` + `CursorPage[T].next_cursor`.
- Tests: `Client` with `HTTP_HX_REQUEST="true"` to exercise htmx path. `assertTemplateUsed` via `response.templates`.

## Rules — Tests

- Real test DB at every layer. NO mocks of own repos/services.
- Mocks only at external boundaries (Stripe/SES/HTTP/Celery `.delay()`).
- `@pytest.mark.django_db` everywhere.
- `--reuse-db` in `addopts`. Transactional rollback per test.
- Three files: `test_repo.py`, `test_service.py`, `test_api.py`.
- Builder fixtures (`make_product`) create real DB rows.
- `freezegun` for time, NOT `MagicMock(datetime)`.
- `transaction=True` only when needed (`on_commit` tests).
- Every reliable-signal receiver: "called twice, ran once" idempotency test.

## Rules — Signals

- `ReliableSignal` from `config.signals`. Never standard Django signals for cross-service work.
- `signal.send_reliable(sender=None, foo_id=1)` inside `transaction.atomic()`.
- Args JSON-serializable (IDs, not models).
- Receivers MUST be idempotent (at-least-once).
- Receivers run as Celery tasks on commit.

## Rules — Settings

- Split: `base.py` shared + `local.py` dev + `production.py` prod.
- Banner section headers (77 chars `=`).
- `python-decouple`: `config("KEY", default="...")`.
- `DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"`.
- Env-specific values (`DEBUG`, `ALLOWED_HOSTS`) → `local.py` / `production.py`, never `base.py`.

## Rules — Cache

- Lives in repository layer. Not service. Not view.
- Use `redis_cache` instance (NOT broker — see django-deploy).
- Cache-aside. Explicit invalidate on writes.
- Keys: `<resource>:<id>`, `<resource>:list:<filter>`, `user:<id>:<resource>:<id>`.
- TTL default 5min. Jitter for hot keys.
- NEVER cache: money, inventory counts, auth state, list queries (until profiled).
- Per-user data → key MUST include user_id. Cross-user leak otherwise.

## Rules — Email

- AWS SES via SMTP. Config in `production.py`.
- `EmailService.send(...)` idempotent via sha256 of `(template, to, context)` or explicit `idempotency_key`.
- Templates: `src/templates/emails/<name>/{subject.txt,body.txt,body.html}`.
- Triggered via reliable signal → receiver → service. Never direct `send_mail` inside transaction.
- Celery retry on `SMTPException` / `ConnectionError`. Exponential backoff.
- Suppression list from SNS bounce/complaint webhook. Verify SNS signature.

## Rules — Docker (dev)

- All commands inside `web` via Makefile.
- `make up-d` first; then `make test`, `make migrate`, etc.
- `docker compose exec` (stack up). Never `run --rm` for normal dev.
- `run --rm` only for pre-stack-up: `uv add`, `manage.py startapp`.
- `Dockerfile` has `dev` + `prod` targets. CI builds `--target prod`.
- Entrypoint waits postgres. `RUN_MIGRATIONS=true` on `web` only (env var gates migrate).

## Rules — Deploy

- Multi-stage Dockerfile, `--target prod`.
- `compose.prod.yml`: gunicorn command, no bind mounts, `stop_grace_period: 35s`.
- Topology self-hosted: HAProxy (`lb`) + Postgres (`db`) + Redis × 2 (`redis_broker`, `redis_cache`) + GlitchTip + pgbouncer (on `db`).
- External only: S3 (static/media), AWS SES, Let's Encrypt.
- Inventory groups: `lb`, `web` (N), `worker_beat` (1), `worker` (0+), `db`, `redis_broker`, `redis_cache`, `glitchtip`.
- Beat is a singleton. `worker_beat` = ONE host. Scale `worker:` for capacity.
- Ansible Vault for secrets. `~/.config/django-deploy/vault_pass`, never in repo.
- `make provision` (one-time + on infra changes), `make deploy IMAGE_TAG=<sha>`, `make rollback TAG=<sha>`.
- Zero-downtime rolling: HAProxy admin-socket drain → wait 30s → swap → `/readyz` → resume. `rescue` block resumes on failure.
- gunicorn `graceful_timeout = 30`. Match with compose `stop_grace_period: 35s`.
- pgbouncer transaction mode → `CONN_MAX_AGE = 0`. No `LISTEN/NOTIFY`. `DISABLE_SERVER_SIDE_CURSORS = True`.
- Migrations run once on `web[0]` before any restart.
- Backups: `pg_dump` cron + rclone off-host. Restore drill quarterly.

## Rules — CI

- GitHub Actions, two jobs.
- `check`: lint + format-check + typecheck + test against real Postgres/Redis services. Every push/PR.
- `build`: `--target prod`, push to GHCR with SHA tag. Master only.
- No `:latest`. No auto-deploy.
- `--reuse-db` matches local.
- `make deploy IMAGE_TAG=<sha>` after CI publishes.

## Rules — Migrations at scale

- Safe in rolling deploy: `AddField` (nullable), `CreateModel`, `AddIndex` (small), `RemoveIndex`, metadata-only `AlterField`.
- Unsafe → expand-contract: `RemoveField`, `RenameField`, `AlterField` (type change), `AddField` (NOT NULL no default).
- Large table index: `CREATE INDEX CONCURRENTLY` + `atomic = False`.
- `NOT NULL`: `CHECK ... NOT VALID` → backfill → `VALIDATE CONSTRAINT` → `SET NOT NULL`.
- Backfills = management commands. NOT `RunPython` for >1000 rows.
- `SET statement_timeout = '5s'` on DDL so stuck locks fail fast.

## Cross-cutting Rules

- **Official tools first.** `django-admin startproject`, `startapp`, `uv init`. Edit the result. Never transcribe generated boilerplate (`INSTALLED_APPS`, `MIDDLEWARE`, `TEMPLATES`, `manage.py`, `wsgi.py`).
- **Real DB testing.** Never mock own repos/services. Pytest-django + reuse-db handles speed.
- **`BaseModel` minimal.** Two timestamps. No `is_active`, no soft delete, no `created_by`, no UUID, no JSON metadata. Add to specific models if needed.
- **`_item(s)` on resource services.** Strict consistency. No `archive_product` mixed with `create_item`.
- **One thin ServiceMixin.** No new mixins. No config knobs. Override the method.
- **Reliable signals** for post-commit side effects. Never standard Django signals for cross-service.
- **Models lose, the others gain.** Custom manager → repo. `save()` override → service. `pre/post_save` signal → reliable receiver. `@property` (computed) → DTO field or service method.

## When to invoke a deep skill

Invoke if:
- Generating code (model, repo, service, viewset, migration, ansible role)
- Full setup walkthrough (scaffold a new project, set up CI)
- Detail not on this page
- User explicitly asks "show me the X skill"

Skip invoke if:
- Question answers from rules above
- Quick lookup ("where does business logic live?", "what's the field order?")
- Reviewing existing code against conventions

## Verify

If editing this index: every rule here MUST be in sync with its source skill. Drift = confused agent.

```bash
make check
make test
```
