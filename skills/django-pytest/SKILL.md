---
name: django-pytest
description: Set up and write pytest tests for a Django project — pytest-django configuration, Celery eager mode for reliable-signal tests, freezegun for time-sensitive logic, shared conftest fixtures for DTOs and svcs overrides, and the three-layer test convention (repository against a real DB, service against mocked repos, API through HTTP). Use when adding tests to a new project, writing tests for a new feature, setting up test infrastructure, or explaining how tests should be organized.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Pytest for Django

Testing in this project is layered the same way the code is. Each layer has its own rules, its own fixtures, and its own performance characteristics. The goal is to keep the fast tests fast — service tests should never touch a database — and to isolate the slow tests at the edges.

## The Three Layers

| File | What it covers | DB? | Speed |
|---|---|---|---|
| `test_repo.py` | ORM ↔ DTO conversion, prefetches, transactions | ✅ real | slow |
| `test_service.py` | Business logic, validation, orchestration | ❌ mocked | fast |
| `test_api.py` | HTTP integration — request → view → service → repo | ✅ real | slow |

Service tests are the most valuable layer and should outnumber the others. If a service test needs `@pytest.mark.django_db`, something has leaked — find the ORM call and push it into a repository.

Tests live inside each app: `src/apps/<app>/tests/test_repo.py`, `test_service.py`, `test_api.py`.

All commands run inside the `web` container — the project uses Docker Compose for local development. Outside Compose, `postgres` and `redis` won't resolve. See the `django-docker` skill for the stack.

## Dependencies

```bash
docker compose run --rm web uv add --dev pytest pytest-django pytest-celery freezegun pytest-mock
```

## Configuration

Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.local"
python_files = ["test_*.py"]
pythonpath = ["src"]
addopts = [
    "-ra",
    "--strict-markers",
    "--strict-config",
]
markers = [
    "slow: deselect with '-m \"not slow\"'",
]
```

Notes:
- `pythonpath = ["src"]` is what lets `from config.services import get` resolve without an editable install.
- `--strict-markers` catches typos in `@pytest.mark.xxx`. `--strict-config` does the same for the config file.

## Celery in Tests

Reliable signals enqueue Celery tasks. For receivers to execute in-process during tests, set Celery to eager mode. Add to `src/config/settings/local.py`:

```python
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
```

With eager mode on, `send_reliable()` still goes through `transaction.on_commit`, so tests that exercise reliable signals must run inside `@pytest.mark.django_db` with `transaction=True` so `on_commit` actually fires.

## `conftest.py`

A project-level `conftest.py` at `src/conftest.py` holds the fixtures every test layer can pull from. Keep it small and generic — feature-specific fixtures go in per-app `conftest.py` files.

```python
from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from apps.products.dtos import ProductDTO


# ---- time --------------------------------------------------------------

@pytest.fixture
def frozen_time():
    """Freeze time at a deterministic instant."""
    with freeze_time("2026-01-01T00:00:00Z") as frozen:
        yield frozen


# ---- svcs --------------------------------------------------------------

@pytest.fixture
def override_service():
    """
    Swap a real service factory for a fake for the duration of a test.

    Usage:
        def test_something(override_service):
            fake = MagicMock(spec=ProductService)
            fake.list_products.return_value = []
            override_service(ProductService, fake)
            ...
    """
    from config.services import registry

    originals: dict[type, Any] = {}

    def _override(service_type: type, fake: Any) -> None:
        originals.setdefault(service_type, registry._factories.get(service_type))
        registry.register_factory(service_type, lambda _container: fake)

    yield _override

    for service_type, original in originals.items():
        if original is not None:
            registry._factories[service_type] = original


# ---- DTO builders ------------------------------------------------------

@pytest.fixture
def make_product_dto():
    """Build a ProductDTO with sensible defaults; override anything via kwargs."""

    def _build(**overrides: Any) -> ProductDTO:
        fields: dict[str, Any] = {
            "id": 1,
            "name": "Widget",
            "price": Decimal("9.99"),
            "stock": 5,
        }
        fields.update(overrides)
        return ProductDTO(**fields)

    return _build


# ---- repository mocks --------------------------------------------------

@pytest.fixture
def mock_product_repo(make_product_dto):
    """A MagicMock spec'd against ProductRepository, pre-loaded with a DTO."""
    from apps.products.repositories import ProductRepository

    repo = MagicMock(spec=ProductRepository)
    repo.create.return_value = make_product_dto()
    repo.get_by_id.return_value = make_product_dto()
    repo.list_all.return_value = [make_product_dto()]
    return repo
```

A few patterns worth calling out:

- **Factories over fixtures for data.** `make_product_dto()` is more flexible than a `product_dto` fixture because tests can ask for `make_product_dto(stock=0)`.
- **`spec=` on mocks**. Always pass `spec=SomeRepository` to `MagicMock` — it makes the mock fail fast on attribute typos.
- **`override_service` lets API tests substitute a fake service** without monkey-patching imports.

## Writing Each Layer

### `test_repo.py` — Real database

```python
import pytest
from decimal import Decimal

from apps.products.repositories import ProductRepository
from apps.products.dtos import ProductDTO


@pytest.mark.django_db
def test_create_returns_dto():
    repo = ProductRepository()

    dto = repo.create(name="Widget", price=Decimal("9.99"), stock=5)

    assert isinstance(dto, ProductDTO)
    assert dto.price == Decimal("9.99")


@pytest.mark.django_db
def test_get_by_id_round_trips():
    repo = ProductRepository()
    created = repo.create(name="Widget", price=Decimal("9.99"), stock=5)

    fetched = repo.get_by_id(created.id)

    assert fetched == created
```

- Assert on the **DTO type** at least once per repo — catches a repo accidentally returning an ORM instance.
- Use `@pytest.mark.django_db(transaction=True)` only when you need to exercise `transaction.on_commit` behavior.

### `test_service.py` — No database

```python
from decimal import Decimal

import pytest

from apps.products.services import ProductService


def test_create_product_delegates_to_repo(mock_product_repo, make_product_dto):
    mock_product_repo.create.return_value = make_product_dto(name="Gadget")

    service = ProductService(mock_product_repo)
    result = service.create_product(name="Gadget", price=Decimal("9.99"), stock=5)

    assert result.name == "Gadget"
    mock_product_repo.create.assert_called_once_with(
        name="Gadget", price=Decimal("9.99"), stock=5
    )
```

- **No `@pytest.mark.django_db`.** If you reach for it in a service test, you've found a leak.
- Assert on **both** the return value and the repo calls.

### `test_api.py` — HTTP integration

```python
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def api_client():
    return APIClient()


@pytest.mark.django_db
def test_create_product(api_client):
    response = api_client.post(
        "/api/products/",
        data={"name": "Widget", "price": "9.99", "stock": 5},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["name"] == "Widget"


@pytest.mark.django_db
def test_list_products_empty(api_client):
    response = api_client.get("/api/products/")

    assert response.status_code == 200
    assert response.data == []
```

- Use DRF's `APIClient` instead of Django's `client`.
- Prefer asserting on **shape** over full dict comparison.

#### Testing the exception handler

Services raise plain Python exceptions; the central exception handler in `src/config/exception_handler.py` maps them to HTTP responses (400, 404, 403). API tests prove the round-trip:

```python
@pytest.mark.django_db
def test_create_order_rejects_insufficient_stock(api_client):
    # Create a product with only 1 in stock, then order 10
    product_resp = api_client.post(
        "/api/products/",
        data={"name": "Limited", "price": "5.00", "stock": 1},
        format="json",
    )
    product_id = product_resp.data["id"]

    response = api_client.post(
        "/api/orders/",
        data={"items": [{"product_id": product_id, "quantity": 10}]},
        format="json",
    )

    assert response.status_code == 400
    assert "Insufficient stock" in response.data["detail"]
```

### Reliable-signal tests

Receivers run via Celery. With `CELERY_TASK_ALWAYS_EAGER`, they execute in-process, but `transaction.on_commit` only fires when the transaction actually commits:

```python
@pytest.mark.django_db(transaction=True)
def test_order_created_triggers_receiver(mocker):
    spy = mocker.patch("apps.orders.receivers.on_order_created", wraps=on_order_created)
    # ...build real repos, call service.create_order(...), then:
    spy.assert_called_once()


def test_receiver_is_idempotent(mocker):
    send_email = mocker.patch("apps.orders.receivers.send_order_confirmation")

    on_order_created(order_id=1)
    on_order_created(order_id=1)

    assert send_email.call_count == 1
```

Every receiver needs an explicit "called twice, ran once" test.

## freezegun

Use `@freeze_time` for any test that asserts on timestamps or time-sensitive logic.

```python
from freezegun import freeze_time


@freeze_time("2026-01-15T12:00:00Z")
def test_expires_at_is_24h_from_now():
    service = SubscriptionService(MagicMock())
    dto = service.start_trial(user_id=1)

    assert dto.expires_at.isoformat() == "2026-01-16T12:00:00+00:00"
```

## Common Mistakes

- **Reaching for `@pytest.mark.django_db` in a service test.** The service has an ORM import hiding in it.
- **Using a fixture that returns a shared mutable object.** Use factories (`make_*`) instead.
- **Asserting on `response.json() == {...}` with the full dict.** Too brittle.
- **Forgetting `transaction=True` on reliable-signal tests.**
- **Testing Django internals.** Don't test that `.filter()` works — test your code.
- **Missing the idempotency test.** Every reliable-signal receiver needs one.

## Verify

```bash
docker compose run --rm web uv run pytest
docker compose run --rm web uv run pytest -m "not slow"       # fast loop
docker compose run --rm web uv run pytest src/apps/orders/    # one app
docker compose run --rm web uv run pytest --lf                # re-run last failures
```
