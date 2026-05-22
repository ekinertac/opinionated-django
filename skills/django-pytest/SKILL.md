---
name: django-pytest
description: pytest setup + conventions. ALL layers use real test DB (NO mocks of own repos/services — mocks only at external boundaries). pytest-django + --reuse-db + transactional rollback per test. Three files per app: test_repo.py, test_service.py, test_api.py. Builder fixtures (make_*) create real rows. Celery eager mode for reliable signals. freezegun for time. Reliable receivers need "called twice, ran once" idempotency test.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Pytest for Django

Each test layer focuses on a different concern. **All layers hit the test database.** The project's repository / service / DTO discipline doesn't need mocks of internal layers to stay clean — the typed boundaries do that work already. Mocks are reserved for *external* dependencies: HTTP clients, payment SDKs, mail providers.

## The Three Layers

| File | What it tests | Strategy |
|---|---|---|
| `test_repo.py` | ORM ↔ DTO conversion, prefetches, transactions, query helpers, `coerce_related_manager` round-trip | Real repo, real DB rows, assert on DTO shape |
| `test_service.py` | Business logic: validation, orchestration, exception types, cross-repo coordination | Real repos + real DB; mock only at external boundaries |
| `test_api.py` | HTTP integration: serializer validation, status codes, response shape, exception handler mapping | DRF `APIClient`, full real round-trip |

The earlier "service tests use mocked repos" rule has been removed. Mocking your own repositories sounded principled — fast tests, isolated units — but the mocks returned whatever the test set up, not what the real repo would have returned. Bugs in prefetches, missing fields, wrong types: all hidden. Tests passed; production broke. Real DB closes that gap, and the cost is bounded by:

- **`--reuse-db`** keeps the test schema across runs (schema setup is the slow part, not the tests).
- **`@pytest.mark.django_db`** (default `transaction=False`) wraps each test in a transaction that rolls back at the end — fast.

Tests live inside each app: `src/apps/<app>/tests/test_repo.py`, `test_service.py`, `test_api.py`.

All commands run inside the `web` container — see the `django-docker` skill for the stack and the Makefile.

## Where Mocks Still Belong

Mocks at **external** boundaries only:

- ✅ Mock an HTTP client to a third-party API (Stripe, Twilio, SendGrid)
- ✅ Mock a Celery `.delay()` when verifying enqueue (the receiver itself gets its own real-execution test)
- ✅ Mock the system clock — but use `freezegun`, not `MagicMock(datetime)`
- ❌ Mock your own `Repository`
- ❌ Mock your own `Service` from another service
- ❌ Mock the ORM — use the test DB

If a test needs an internal mock to pass, the design has a coupling problem. Fix the design, not the test.

## Dependencies

```bash
docker compose exec web uv add --dev pytest pytest-django pytest-celery freezegun pytest-mock
```

## Configuration

```toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.local"
python_files = ["test_*.py"]
pythonpath = ["src"]
addopts = [
    "-ra",
    "--strict-markers",
    "--strict-config",
    "--reuse-db",
]
markers = [
    "slow: deselect with '-m \"not slow\"'",
]
```

Notes:
- `pythonpath = ["src"]` lets `from config.services import get` resolve without an editable install.
- `--strict-markers` catches typos in `@pytest.mark.xxx`. `--strict-config` does the same for the config file.
- `--reuse-db` keeps the test database between runs — schema is created once, reused. Pass `--create-db` after a migration to force a fresh build.

## Speed Tactics

- **`--reuse-db`** — single biggest win, already in `addopts`.
- **`@pytest.mark.django_db`** with the default `transaction=False` rolls back per-test via a transaction. Faster than `transaction=True`.
- **`@pytest.mark.django_db(transaction=True)`** only when needed (typically reliable-signal tests where `transaction.on_commit` must fire).
- **`pytest-xdist`** for parallelization once the suite gets long: `docker compose exec web uv run pytest -n auto`. Only worth it past ~5 seconds of total runtime.
- **`@pytest.mark.slow`** on genuinely slow tests, then skip them in tight loops with `-m "not slow"`.

## Celery in Tests

Reliable signals enqueue Celery tasks. For receivers to execute in-process during tests, set Celery to eager mode. Add to `src/config/settings/local.py`:

```python
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
```

With eager mode on, `send_reliable()` still goes through `transaction.on_commit`, so tests that exercise reliable signals must run inside `@pytest.mark.django_db(transaction=True)` so `on_commit` actually fires.

## `conftest.py`

A project-level `conftest.py` at `src/conftest.py` holds the fixtures every test layer can pull from. Keep it small and generic — feature-specific fixtures go in per-app `conftest.py` files.

```python
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from freezegun import freeze_time

from apps.products.dtos import ProductDTO
from apps.products.repositories import ProductRepository


# ---- time --------------------------------------------------------------

@pytest.fixture
def frozen_time():
    """Freeze time at a deterministic instant."""
    with freeze_time("2026-01-01T00:00:00Z") as frozen:
        yield frozen


# ---- builders that hit the real DB -------------------------------------

@pytest.fixture
def make_product():
    """Create a real Product row and return its DTO. Default for service/API tests."""
    repo = ProductRepository()

    def _build(**overrides: Any) -> ProductDTO:
        fields: dict[str, Any] = {
            "name": "Widget",
            "price": Decimal("9.99"),
            "stock": 5,
        }
        fields.update(overrides)
        return repo.create(**fields)

    return _build


# ---- DTO builders that DO NOT hit the DB -------------------------------

@pytest.fixture
def make_product_dto():
    """Build a ProductDTO without touching the DB.

    Reach for this only in narrow cases — typically when you need a DTO shape
    that's awkward to seed (e.g., a DTO with an id that intentionally doesn't
    exist in the DB, for testing an explicit "not found" path before the
    repo call). Most tests should use `make_product` instead.
    """
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


# ---- service overrides (external boundaries only) ----------------------

@pytest.fixture
def override_service():
    """Substitute a service factory for the duration of a test.

    Use ONLY when the service being substituted wraps an external boundary
    (third-party API, payment provider, mail sender). Do NOT use this to
    short-circuit internal layers — write real-DB tests instead.

    Usage:
        def test_checkout_flow(override_service):
            fake = MagicMock(spec=PaymentService)
            fake.charge.return_value = ChargeResult(...)
            override_service(PaymentService, fake)
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
```

Patterns worth calling out:

- **`make_product` (real row)** is the default for service and API tests. Hits the test DB, returns a real DTO.
- **`make_product_dto` (no DB)** is for narrow cases — testing the path where a service receives a DTO with a missing or fabricated value before any repo call.
- **`override_service`** wraps an *external* boundary, not an internal layer.

## Writing Each Layer

### `test_repo.py` — ORM ↔ DTO conversion

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


@pytest.mark.django_db
def test_get_by_id_raises_lookup_error_when_missing():
    repo = ProductRepository()

    with pytest.raises(LookupError):
        repo.get_by_id(99999)
```

- Assert on the **DTO type** at least once per repo — catches a repo accidentally returning an ORM instance.
- Test the `LookupError` path explicitly.
- Use `@pytest.mark.django_db(transaction=True)` only when you need to exercise `transaction.on_commit` behavior.

### `test_service.py` — Business logic, real repo, real DB

```python
import pytest
from decimal import Decimal

from apps.products.repositories import ProductRepository
from apps.products.services import ProductService


@pytest.mark.django_db
def test_create_product(make_product):
    service = ProductService(ProductRepository())

    dto = service.create_item(name="Widget", price=Decimal("9.99"), stock=5)

    assert dto.name == "Widget"
    assert dto.price == Decimal("9.99")


@pytest.mark.django_db
def test_create_product_rejects_negative_price():
    service = ProductService(ProductRepository())

    with pytest.raises(ValueError, match="price"):
        service.create_item(name="Widget", price=Decimal("-1"), stock=5)


@pytest.mark.django_db
def test_decrement_stock_below_zero_raises(make_product):
    product = make_product(stock=2)
    service = ProductService(ProductRepository())

    with pytest.raises(ValueError, match="Insufficient stock"):
        service.decrement_stock(product_id=product.id, quantity=5)
```

- Real repo, real DB. Pass the repo into the service constructor.
- Use `make_*` builders to seed prerequisite data.
- Assert on the exception **type and message fragment** for invariant violations — services raise `ValueError` / `LookupError` / `PermissionError` and the central exception handler maps them to HTTP.
- If a service depends on multiple repos, instantiate them all and inject. The wiring is the same as production; tests just construct the service explicitly instead of going through `get(ServiceType)`.

When a service calls an external dependency (payment gateway, mail provider), substitute *that* dependency with a mock — not the service itself, not its repos. Use `override_service` if the external dep is itself a service in the svcs registry.

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
    # ...build real repos, call service.create_item(...), then:
    spy.assert_called_once()


def test_receiver_is_idempotent(mocker):
    send_email = mocker.patch("apps.orders.receivers.send_order_confirmation")

    on_order_created(order_id=1)
    on_order_created(order_id=1)

    assert send_email.call_count == 1
```

Every receiver needs an explicit "called twice, ran once" test. The `send_email` mock here is correct — `send_order_confirmation` wraps an external mail provider, the canonical place mocks belong.

## freezegun

Use `@freeze_time` for any test that asserts on timestamps or time-sensitive logic.

```python
from freezegun import freeze_time


@freeze_time("2026-01-15T12:00:00Z")
@pytest.mark.django_db
def test_expires_at_is_24h_from_now():
    service = SubscriptionService(SubscriptionRepository())
    dto = service.start_trial(user_id=1)

    assert dto.expires_at.isoformat() == "2026-01-16T12:00:00+00:00"
```

## Common Mistakes

- **Mocking your own repository.** Use the real repo + test DB. Internal mocks pass tests that production fails.
- **Mocking your own service from another service.** Same reason. Wire the real service via `svcs` and let it run.
- **Using a fixture that returns a shared mutable object.** Use factories (`make_*`) instead.
- **Asserting on `response.json() == {...}` with the full dict.** Too brittle.
- **Forgetting `transaction=True` on reliable-signal tests.**
- **Testing Django internals.** Don't test that `.filter()` works — test your code.
- **Missing the idempotency test.** Every reliable-signal receiver needs one.
- **Forgetting to mock a real external boundary.** A test that hits Stripe is not a unit test.

## Verify

```bash
make test                                                  # full suite
docker compose exec web uv run pytest -m "not slow"        # fast loop
docker compose exec web uv run pytest src/apps/orders/     # one app
docker compose exec web uv run pytest --lf                 # re-run last failures
```
