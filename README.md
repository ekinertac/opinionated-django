# op-django

> A collection of [Agent Skills](https://vercel.com/kb/guide/agent-skills-creating-installing-and-sharing-reusable-agent-context) that give a coding agent architectural skills to build scalable, maintainable Django projects with clean separation of concerns, testability, and a full suite of best practices.

Django's ORM is powerful but hard to type — querysets, model instances, related managers, and `F()`/`Q()` expressions don't play well with static type checkers. These skills solve that by keeping all ORM work behind a repository layer that returns Pydantic DTOs. Business logic lives in services that never import a model. Views become thin dispatchers, and each layer is easy to mock and test independently.

## A Layered Approach Using Encapsulation

Each layer encapsulates the one beneath it. The API layer never touches the ORM. Services never import a model. Repositories never leak a queryset or a model instance. Every boundary between layers is crossed as a typed Pydantic DTO, so changes stay local and tests stay fast.

```
  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
  │   API    │──▶│ Service  │──▶│   DTO    │──▶│   Repo   │──▶│  Model   │
  └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
   thin views    business       typed data      ORM lives       
                 logic + DI     at boundaries   here only       
```

| Layer           | Role                                              | Library                                                                                          |
|-----------------|---------------------------------------------------|--------------------------------------------------------------------------------------------------|
| **API**         | Routing, input validation, OpenAPI                | [DRF](https://www.django-rest-framework.org/) · [drf-spectacular](https://drf-spectacular.readthedocs.io/) · [drf-nested-routers](https://github.com/alanjds/drf-nested-routers) |
| **Service**     | Business logic & orchestration with true DI       | [svcs](https://svcs.hynek.me/)                                                                   |
| **DTO**         | Typed data at every layer boundary                | [Pydantic v2](https://docs.pydantic.dev/)                                                        |
| **Repository**  | All ORM access, transactions, prefetches          | [Django](https://www.djangoproject.com/)                                                         |
| **Model**       | Persistence with Django's default BigAutoField     | [Django](https://www.djangoproject.com/)                                                         |
| **Async**       | Reliable signals & background tasks               | [Celery](https://docs.celeryq.dev/)                                                              |

## DX

| Concern                  | Tool                                                                                                                                                                                                               |
|--------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Local environment**    | [Docker Compose](https://docs.docker.com/compose/) — postgres, redis, celery, web                                                                                                                                  |
| **Packaging**            | [uv](https://docs.astral.sh/uv/)                                                                                                                                                                                   |
| **Settings**             | [python-decouple](https://github.com/HBNetwork/python-decouple)                                                                                                                                                    |
| **Linting & formatting** | [ruff](https://docs.astral.sh/ruff/)                                                                                                                                                                               |
| **Type checking**        | [pyrefly](https://pyrefly.org/) · [django-stubs](https://github.com/typeddjango/django-stubs)                                                                                                                      |
| **Testing**              | [pytest](https://docs.pytest.org/) · [pytest-django](https://pytest-django.readthedocs.io/) · [pytest-celery](https://docs.celeryq.dev/projects/pytest-celery/) · [freezegun](https://github.com/spulec/freezegun) |

## Project Structure

```
src/
  manage.py
  config/
    settings/
      base.py
      local.py
      production.py
    urls.py
    wsgi.py
    asgi.py
    celery.py
    services.py
    types.py
    signals.py
    exception_handler.py
  apps/
    <app>/
      models.py
      admin.py
      views.py
      serializers.py
      urls.py
      services.py
      repositories.py
      dtos.py
      tests/
```

## Install

Skills install with a single command:

```bash
# The whole collection
npx skills add ekinertac/opinionated-django

# Or just one
npx skills add ekinertac/opinionated-django/django-scaffold|django-docker|django-architecture|django-models|django-services|django-signals|django-settings|django-pytest|django-lint
```

Your agent will pick them up automatically on its next run. You can also clone the repo and point your agent at `skills/` directly.

## The Skills

Each skill is a directory under `skills/` with its own `SKILL.md`. They stand alone but compose nicely — `django-scaffold` lays the foundation, `django-architecture` builds features on top, and the rest fill in the details.

### `django-scaffold`
Sets up a new (or existing) Django project into the opinionated layout. Creates the `src/config/` shell — split settings, services registry, exception handler, reliable signals, Celery wiring — installs dependencies with `uv`, and lays down ruff / pyrefly / pytest config. **Run this first.**

### `django-docker`
Adds Docker Compose for local development — `Dockerfile`, `docker-compose.yml` (web + postgres + redis + celery), `.dockerignore`, and `.env.example`. All dev commands run inside the `web` container. **Run after `django-scaffold`.**

### `django-services`
Plain service classes with constructor-injected repositories, wired through an [svcs](https://svcs.hynek.me) registry. Business logic lives here, zero ORM imports allowed. Resolve anywhere — views, Celery tasks, management commands, tests — with a single generic `get[T]()` call.

### `django-models`
Structures Django models with a strict internal layout: `Meta` first (verbose names, indexes, constraints), then fields grouped as identifiers → time → status → domain → relations. Uses Django's default `BigAutoField` for primary keys. Uniqueness lives in `Meta.constraints` (never `unique=True`), indexes in `Meta.indexes` (never `db_index=True`). Every model is registered in the admin with a clean, fast-loading config.

### `django-architecture`
The full feature blueprint. Given a description, the agent scaffolds models, Pydantic DTOs, repositories, services, DRF ViewSets, admin registration, and three layers of tests (repo against a real DB, service against mocked repos, API through HTTP).

### `django-signals`
Reliable signals for async side-effects — notifications, cache invalidation, analytics, cross-service coordination. Receivers are enqueued **inside** the database transaction via Celery, so rollbacks are respected and delivery is at-least-once.

### `django-settings`
Keeps settings organized with banner-style section headers across a base/local/production split. Use whenever settings are added, removed, or restructured.

### `django-pytest`
Three-layer pytest setup — repo against a real DB, service against mocked repos, API through HTTP — with `pytest-django`, `pytest-celery` for reliable-signal receivers, `freezegun` for time-sensitive logic, and shared conftest fixtures.

### `django-lint`
Runs `ruff check`, `ruff format --check`, and `pyrefly check`, then fixes whatever it finds. Use before committing, or any time you want a clean bill of health.

## The Patterns at a Glance

- **Models** — `Meta` first (verbose names, indexes, constraints). Fields ordered: identifiers → time → status → domain → relations. Django's `BigAutoField` for primary keys. No business logic, no custom `save()`, no computed properties.
- **DTOs** — Pydantic v2 with `from_attributes=True`. ORM objects never leave the repository.
- **Repositories** — The only layer that touches the ORM. Returns DTOs. `@transaction.atomic` for multi-writes. One repo per aggregate root.
- **Services** — Receives dependencies via `__init__`. Pure business logic. Zero ORM imports. Testable without a database.
- **API** — DRF ViewSets in each app with their own router. Input validation via DRF Serializers, output reuses DTOs via `.model_dump()`. Nested resources use `drf-nested-routers`. OpenAPI via `drf-spectacular`.
- **Reliable Signals** — Side-effects enqueued inside the DB transaction via Celery. At-least-once delivery. Idempotent receivers.
- **Settings** — Split into base/local/production. Sectioned with banner headers. `python-decouple` for env vars.

## Example Project

See [`example_project`](./example_project) for a working Django project built with these patterns — two apps (`products`, `orders`), full repository + service + API layering, and tests at all three levels.

## License

[MIT](./LICENSE).
