---
name: django-architecture
description: Implement a Django feature following the opinionated architecture — repository pattern, Pydantic DTOs, svcs service locator, DRF ViewSet API, Celery reliable signals, and layered tests. Use when the user asks to add a new entity, endpoint, app, or business logic in a Django project that follows these conventions.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Implement a Django Feature

You are implementing a feature in an opinionated Django project managed with `uv`. Every convention below is mandatory. Do not deviate.

**Why this architecture exists:** Django's ORM is powerful but hard to type — querysets, model instances, related managers, and `F()`/`Q()` expressions don't play well with static type checkers. This project solves that by pushing all ORM usage into repositories that return Pydantic DTOs. Services receive repos via constructor injection and contain pure business logic with zero ORM imports. Views are thin dispatchers. The result: everything from the repository boundary outward is fully typed, IDE-friendly, and testable in isolation.

**Tooling:** `uv` is the package manager. Local development runs entirely in Docker Compose, with a Makefile wrapping the common commands (`make test`, `make migrate`, `make check`, etc.). Never use `pip`, `poetry`, or raw `python`. To add a dependency: `docker compose run --rm web uv add <package>`. See the `django-docker` skill for the Compose stack and the full Makefile target list.

## BEFORE WRITING CODE

Gather current project state by reading:

- `src/config/services.py` — registered repos/services
- `src/config/settings/base.py` — `INSTALLED_APPS` and `REST_FRAMEWORK` config
- `src/config/urls.py` — included app URLs
- `src/config/types.py` — `AuthedRequest` and other shared request types
- Any existing app the feature touches

Then state your implementation plan: models, DTOs, repos, services, views, tests.

---

## LAYER-BY-LAYER IMPLEMENTATION

Follow this exact order. Do not skip layers. Each layer has rules that are non-negotiable.

### Layer 1: Model

File: `src/apps/<app>/models.py`

If the app doesn't exist yet, let Django generate it — don't hand-roll the skeleton. Run:

```bash
mkdir -p src/apps/<app>
docker compose exec web uv run python manage.py startapp <app> src/apps/<app>
```

Then edit the generated `src/apps/<app>/apps.py` so the `name` attribute uses the dotted path: `name = "apps.<app>"`. Everything else Django generated (`models.py`, `admin.py`, `migrations/`, `views.py`, `tests.py`) gets customized in the layers below — `views.py` becomes a DRF ViewSet, `tests.py` is replaced with the `tests/` package, etc.

Follow the **django-models** skill for full conventions. The key rules:

- Inherit from `config.models.BaseModel` — not `models.Model` directly. Provides `created_at` / `updated_at`.
- Member order: **choices → fields → manager (rare) → Meta → methods**
- Use Django's default `BigAutoField` for primary keys — do NOT define explicit PK fields
- All indexes in `Meta.indexes` — never `db_index=True` on fields
- ZERO business logic — no custom managers, no `save()` overrides, no signals, no properties that compute
- `__str__` is the only method allowed
- Each banned thing has a specific home elsewhere: query helpers → **repository**, write-time invariants/normalization → **service**, post-write side-effects → **reliable signals + receivers**, computed values → **DTO** (Pydantic computed field) or service method. See `django-models` → "Where it lives instead" for the full mapping.

```python
from django.db import models

from config.models import BaseModel


class MyEntity(BaseModel):
    name = models.CharField(max_length=255)

    class Meta:
        verbose_name = "my entity"
        verbose_name_plural = "my entities"
        indexes = [
            models.Index(fields=["-created_at"], name="idx_%(class)s_recent"),
        ]

    def __str__(self):
        return self.name
```

If this is a new app, add it to `INSTALLED_APPS` in `src/config/settings/base.py` using the short dotted path (e.g., `"apps.myapp"`) — this is Django's convention. Django auto-discovers the `AppConfig` from the app's `apps.py`. The explicit `"apps.myapp.apps.MyAppConfig"` form is only needed if an app defines multiple AppConfigs.

Then run:
```bash
make makemigrations && make migrate
```

### Layer 2: DTO

File: `src/apps/<app>/dtos.py`

RULES:
- `model_config = ConfigDict(from_attributes=True)` — always
- For Django `RelatedManager` fields (reverse FKs, M2M), add the `coerce_related_manager` `mode="before"` validator. See **django-repositories** → "Reverse Relations" for the full pattern (the validator is tightly coupled to repo prefetching, so both halves are documented there together).

### Layer 3: Repository

File: `src/apps/<app>/repositories.py`

Follow the **django-repositories** skill for full conventions, examples, and the checklist. The key rules:

- ORM objects NEVER leave this layer — every public method returns a `DTO` / `list[DTO]` / `Page[DTO]`
- Convert with `MyEntityDTO.model_validate(orm_obj)`
- Inputs are primitives (IDs, strings, dates) — never model instances
- `select_related` / `prefetch_related` when the DTO has nested relations
- `@transaction.atomic` on any method with multiple writes
- One repo per aggregate root — child entities are managed by the parent's repo
- `LookupError` on missing rows
- No business logic, no permission checks, no signal sending

### Layer 4: Service

File: `src/apps/<app>/services.py`

RULES:
- Receives repos via `__init__` — NEVER instantiates them, NEVER imports models
- Contains all business logic: validation, orchestration, cross-repo coordination
- Touches ZERO ORM — no `.objects`, no `F()`, no `Q()`, no model imports
- Returns DTOs

### Layer 5: Register in svcs

Add to `src/config/services.py`:

```python
from apps.myapp.repositories import MyEntityRepository
from apps.myapp.services import MyEntityService

registry.register_factory(MyEntityRepository, MyEntityRepository)

def _my_entity_service_factory(container: svcs.Container) -> MyEntityService:
    repo = container.get(MyEntityRepository)
    return MyEntityService(repo)

registry.register_factory(MyEntityService, _my_entity_service_factory)
```

### Layer 6: API Views

Views live inside the app — `src/apps/<app>/views.py`, `src/apps/<app>/serializers.py`, `src/apps/<app>/urls.py`.

`src/apps/<app>/views.py`:

```python
from rest_framework import status, viewsets
from rest_framework.response import Response

from config.services import get

from .services import MyEntityService
from .serializers import CreateMyEntitySerializer


class MyEntityViewSet(viewsets.ViewSet):
    def list(self, request):
        dtos = get(MyEntityService).list_entities()
        return Response([dto.model_dump() for dto in dtos])

    def create(self, request):
        serializer = CreateMyEntitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dto = get(MyEntityService).create_entity(**serializer.validated_data)
        return Response(dto.model_dump(), status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        dto = get(MyEntityService).get_entity(pk)
        return Response(dto.model_dump())
```

`src/apps/<app>/serializers.py`:

```python
from rest_framework import serializers


class CreateMyEntitySerializer(serializers.Serializer):
    name = serializers.CharField()
```

`src/apps/<app>/urls.py`:

```python
from rest_framework.routers import DefaultRouter

from .views import MyEntityViewSet

router = DefaultRouter()
router.register(r"my-entities", MyEntityViewSet, basename="my-entity")

urlpatterns = router.urls
```

Then include in `src/config/urls.py`:

```python
path("api/", include("apps.myapp.urls")),
```

For nested resources, use `drf-nested-routers`:

```python
from rest_framework_nested import routers

router = routers.DefaultRouter()
router.register(r"orders", OrderViewSet, basename="order")

orders_router = routers.NestedDefaultRouter(router, r"orders", lookup="order")
orders_router.register(r"items", OrderItemViewSet, basename="order-items")

urlpatterns = router.urls + orders_router.urls
```

RULES:
- ViewSets do NOT try/except — errors bubble up to the central exception handler in `src/config/exception_handler.py`
- Services raise `ValueError` → 400, `LookupError` → 404, `PermissionError` → 403

### Layer 7: Admin

File: `src/apps/<app>/admin.py`

Follow the **django-models** skill for full admin conventions. The key rules:

- Register every model with `@admin.register`
- `list_display` — `id` first, then 3-5 most useful columns
- `list_per_page = 25`
- `search_fields` — always include `id`
- `readonly_fields` — always include `id`
- `ordering` — explicit, usually `-created_at`
- `list_select_related` — specify FKs shown in `list_display`
- `raw_id_fields` or `autocomplete_fields` for FKs to large tables
- `TabularInline` for child models — `extra = 0`, `show_change_link = True`

---

## TESTS

Write three test layers in `src/apps/<app>/tests/`. No test file may be skipped.

### `test_repo.py` — Real database, validate ORM → DTO conversion

```python
@pytest.mark.django_db
def test_create_and_get():
    repo = MyEntityRepository()
    dto = repo.create(name="Test")
    assert isinstance(dto, MyEntityDTO)
    fetched = repo.get_by_id(dto.id)
    assert fetched == dto
```

### `test_service.py` — Mock the repos, validate business logic

```python
from unittest.mock import MagicMock

def test_create_delegates_to_repo():
    repo = MagicMock()
    expected = MyEntityDTO(id=1, name="Test")
    repo.create.return_value = expected

    service = MyEntityService(repo)
    result = service.create_entity(name="Test")

    assert result == expected
    repo.create.assert_called_once_with(name="Test")
```

### `test_api.py` — Integration through HTTP

```python
from rest_framework.test import APIClient

@pytest.fixture
def api_client():
    return APIClient()

@pytest.mark.django_db
def test_create(api_client):
    resp = api_client.post("/api/my-entities/", data={"name": "Test"}, format="json")
    assert resp.status_code == 201
    assert resp.data["name"] == "Test"
```

---

## RELIABLE SIGNALS (CELERY)

When a business operation needs to trigger async side-effects, use reliable signals — NOT standard Django signals. See the **django-signals** skill for full details.

---

## VERIFY

Run all four checks. ALL must pass before you report done.

```bash
make check    # lint + format-check + typecheck
make test
```

---

## COMPLETION CHECKLIST

- [ ] Model in `src/apps/<app>/models.py`: choices → fields → Meta → methods order, `BigAutoField` PK, zero logic
- [ ] DTO in `src/apps/<app>/dtos.py`: `from_attributes=True`
- [ ] Repository in `src/apps/<app>/repositories.py`: returns DTOs only
- [ ] Service in `src/apps/<app>/services.py`: repos via `__init__`, zero ORM
- [ ] Repo and service registered in `src/config/services.py`
- [ ] ViewSet in `src/apps/<app>/views.py`, serializers in `serializers.py`, router in `urls.py`
- [ ] App URLs included in `src/config/urls.py`
- [ ] Admin registered per **django-models** skill conventions
- [ ] App in `INSTALLED_APPS` (if new) using short dotted path (`"apps.<app>"`)
- [ ] Migrations generated and applied
- [ ] `test_repo.py`: real DB, asserts DTO type
- [ ] `test_service.py`: mocked repos, tests business logic
- [ ] `test_api.py`: HTTP integration with `APIClient`, asserts status codes + response shape
- [ ] `ruff check`, `ruff format --check`, `pyrefly check`, `pytest` all pass
