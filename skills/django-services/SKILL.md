---
name: django-services
description: Structure Django business logic as plain services that receive their dependencies via constructor injection, and wire them through an svcs registry so they can be resolved anywhere — views, Celery tasks, management commands, tests. Use when adding a new service, refactoring fat views or model methods into a service, wiring a service into the registry, or explaining where business logic should live in this project.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Services + svcs Dependency Injection

This project separates Django's framework concerns from business logic using a plain service layer, wired with the [svcs](https://svcs.hynek.me) service locator. The result:

- **Views are one-liners.** They pull a wired service and call a method.
- **Services contain the business logic.** They take repositories (and other services) via `__init__`, call methods on them, and return DTOs.
- **Services never import Django ORM or models.** All ORM access lives behind the repository, so the service is fully typed and easy to read in isolation. Service tests still use the real test database via real repositories — see **django-pytest**.
- **One registry, one `get[T]()` helper.** The same call works in views, tasks, commands, anywhere.

## Why svcs Instead of Module-Level Singletons or a Custom Container

- `svcs` is a tiny, typed, well-maintained service locator — no metaclasses, no decorators, no framework coupling.
- Factories are lazy: a service is constructed only when something asks for it.
- Generic `get[T](type[T]) -> T` preserves types through IDE/type-checker inference.
- Swapping an implementation in tests is a one-line factory override.

## The Registry — `src/config/services.py`

```python
import svcs

from apps.products.repositories import ProductRepository
from apps.products.services import ProductService
from apps.orders.repositories import OrderRepository
from apps.orders.services import OrderService

registry = svcs.Registry()

# --- Repositories ---------------------------------------------------------
registry.register_factory(ProductRepository, ProductRepository)
registry.register_factory(OrderRepository, OrderRepository)


# --- Services (factories pull repos from the container) ------------------
def _product_service_factory(container: svcs.Container) -> ProductService:
    repo = container.get(ProductRepository)
    return ProductService(repo)


def _order_service_factory(container: svcs.Container) -> OrderService:
    repo = container.get(OrderRepository)
    product_repo = container.get(ProductRepository)
    return OrderService(repo, product_repo)


registry.register_factory(ProductService, _product_service_factory)
registry.register_factory(OrderService, _order_service_factory)


def get[T](service_type: type[T]) -> T:
    """Resolve a service from the registry. Works anywhere — views, tasks, commands, tests."""
    return svcs.Container(registry).get(service_type)
```

Patterns to follow:

- **Repositories register themselves.** Use `register_factory(Repo, Repo)` — the class is its own factory because repositories take no constructor arguments.
- **Services register via a named factory.** `_<entity>_service_factory(container)` resolves every dependency from the container and hands it to the service's `__init__`.
- **Register in dependency order.** Repos before services, lower-level services before higher-level ones.
- **One registry per project.** Everything goes through `config.services.registry`.

## Writing a Service

File: `src/apps/<app>/services.py`

```python
from decimal import Decimal
from typing import List

from .dtos import ProductDTO
from .repositories import ProductRepository


class ProductService:
    def __init__(self, repo: ProductRepository):
        self.repo = repo

    def list_items(self) -> List[ProductDTO]:
        return self.repo.list_all()

    def get_item(self, pk: int) -> ProductDTO:
        return self.repo.get_by_id(pk)

    def create_item(self, name: str, price: Decimal, stock: int) -> ProductDTO:
        return self.repo.create(name=name, price=price, stock=stock)

    def update_item(self, pk: int, **fields) -> ProductDTO:
        return self.repo.update(pk, **fields)

    def delete_item(self, pk: int) -> None:
        self.repo.delete(pk)

    # Domain-specific actions keep their full names — see "Naming" below
    def archive_product(self, pk: int, *, user_id: int) -> ProductDTO:
        product = self.repo.get_by_id(pk)
        if product.owner_id != user_id:
            raise PermissionError(f"User {user_id} cannot archive product {pk}")
        return self.repo.set_archived(pk)
```

Rules:

- **Dependencies come in through `__init__`.** The service never instantiates its own repositories or services.
- **Zero ORM.** No `.objects`, no `F()` / `Q()`, no model imports, no `select_related`.
- **Every public method returns a DTO or `list[DTO]`** (or `None` for delete).
- **Business rules live here.** Validation, orchestration across repositories, invariant checks, error raising.
- **Services are stateless.** They hold references to their dependencies and nothing else.
- **Raise plain exceptions.** Use `ValueError`, `PermissionError`, `LookupError`, domain-specific exceptions — not `Http404` or anything Django-flavored.

### CRUD service convention

Services that back a resource ViewSet (`ServiceMixin` from **django-api**) MUST expose these five method names:

| Method | Purpose | Returns |
|---|---|---|
| `list_items()` | List all (or filter via kwargs like `user_id=...`) | `list[DTO]` |
| `get_item(pk: int)` | Fetch one by primary key | `DTO` (raises `LookupError` if missing) |
| `create_item(**fields)` | Create from validated input | `DTO` |
| `update_item(pk: int, **fields)` | Update from validated input | `DTO` |
| `delete_item(pk: int)` | Delete by primary key | `None` |

Generic names — not `list_products`, `create_order`, etc. The service's class name (`ProductService`, `OrderService`) already carries the resource. `ProductService.list_products()` reads as redundant.

Resource-specific names are reserved for **non-CRUD operations** — `archive_product`, `restock_product`, `recalculate_total`, etc. Those keep their full names because the operation is what's distinctive, not the resource.

Services that don't fit the CRUD shape (notification senders, payment processors, search) skip the convention entirely. Their viewsets use a vanilla `viewsets.ViewSet` (no `ServiceMixin`) and call into the service via `get(SomeService)` directly.

### Cross-Entity Logic

When a service method touches more than one aggregate, inject both repositories:

```python
class OrderService:
    def __init__(self, repo: OrderRepository, product_repo: ProductRepository):
        self.repo = repo
        self.product_repo = product_repo

    def create_item(self, items: List[Dict[str, Any]]) -> OrderDTO:
        for item in items:
            product = self.product_repo.get_by_id(item["product_id"])
            if product.stock < item["quantity"]:
                raise ValueError(
                    f"Insufficient stock for product {product.name}: "
                    f"requested {item['quantity']}, available {product.stock}"
                )

        order = self.repo.create(items=items)

        for item in items:
            self.product_repo.decrement_stock(item["product_id"], item["quantity"])

        return order
```

Notes:

- The service orchestrates two repositories but touches zero ORM.
- If a multi-repo write needs atomicity, wrap it in `with transaction.atomic():` — that's one of the very few `django.db` imports allowed in a service.
- Never call another service from inside a service unless that service is explicitly injected.

## Resolving a Service

### From a DRF ViewSet

For resource ViewSets, `ServiceMixin` resolves the service automatically — see **django-api**:

```python
from rest_framework import viewsets

from config.api import ServiceMixin

from .serializers import CreateProductSerializer, UpdateProductSerializer
from .services import ProductService


class ProductViewSet(ServiceMixin, viewsets.ViewSet):
    service_class = ProductService
    create_serializer = CreateProductSerializer
    update_serializer = UpdateProductSerializer
```

For services that don't fit the CRUD shape, resolve via `get()` directly:

```python
from rest_framework import status, viewsets
from rest_framework.response import Response

from config.api import dto_response, validate
from config.services import get

from .serializers import SendNotificationSerializer
from .services import NotificationService


class NotificationViewSet(viewsets.ViewSet):
    def create(self, request):
        data = validate(SendNotificationSerializer, request.data)
        result = get(NotificationService).send(**data)
        return dto_response(result, status.HTTP_202_ACCEPTED)
```

Auth is enforced via DRF's `permission_classes` on the ViewSet or globally in `REST_FRAMEWORK` settings.

### From a Celery task

```python
from celery import shared_task

from config.services import get
from apps.products.services import ProductService


@shared_task
def reprice_product(product_id: int, new_price: str) -> None:
    service = get(ProductService)
    service.update_price(product_id, Decimal(new_price))
```

### From a management command

```python
from django.core.management.base import BaseCommand

from config.services import get
from apps.products.services import ProductService


class Command(BaseCommand):
    def handle(self, *args, **options):
        service = get(ProductService)
        for dto in service.list_items():
            self.stdout.write(dto.name)
```

The same `get()` call works in all three contexts because the registry is global and the container is cheap to construct.

## Testing

Services are tested with **real repositories against the test DB** — not mocks of internal layers. The repo / service / DTO boundary already provides isolation; mocking on top of it hides bugs the real call would expose. See **django-pytest** for the full rationale.

```python
from decimal import Decimal

import pytest

from apps.products.repositories import ProductRepository
from apps.products.services import ProductService


@pytest.mark.django_db
def test_create_item():
    service = ProductService(ProductRepository())

    dto = service.create_item(name="Widget", price=Decimal("9.99"), stock=5)

    assert dto.name == "Widget"


@pytest.mark.django_db
def test_create_order_rejects_insufficient_stock(make_product):
    product = make_product(stock=1)
    service = OrderService(OrderRepository(), ProductRepository())

    with pytest.raises(ValueError, match="Insufficient stock"):
        service.create_item(items=[{"product_id": product.id, "quantity": 5}])
```

- Pass repositories into the service constructor explicitly. The wiring matches production; the test just constructs locally instead of going through `get(ServiceType)`.
- Use `make_*` builder fixtures (defined in `src/conftest.py`) to seed prerequisite rows. See **django-pytest** for the conftest setup.
- Assert on the exception **type and message fragment** for invariant violations.

### Mocks Belong at External Boundaries

If a service calls a third-party API (Stripe, Twilio, mail provider), mock *that* dependency — not the service, not its repos. When the external dep is itself a service registered with `svcs`, use `override_service` from the project conftest:

```python
def test_checkout_charges_card(override_service, make_product):
    fake = MagicMock(spec=PaymentService)
    fake.charge.return_value = ChargeResult(...)
    override_service(PaymentService, fake)

    # ...real CheckoutService + real repos + real DB:
    service = CheckoutService(OrderRepository(), get(PaymentService))
    service.checkout(...)

    fake.charge.assert_called_once()
```

## Common Mistakes

- **Importing models in a service.** If you see `from app.models import X`, the service is doing ORM work. Move it to the repository.
- **Calling `SomeRepository()` inside a service method.** Inject it via `__init__`.
- **Returning querysets or model instances from a service.** Always return DTOs.
- **Putting business logic in the view.** The view should only decode input, call `get(SomeService).method(...)`, and pass the result back.
- **Registering a service with `register_factory(Service, Service)`.** That only works for repositories. Services need a factory that resolves their dependencies.
- **Reaching into `request.user` from the service.** Pass the caller's identity as an explicit argument (`user_id: int`).

## Verify

```bash
make lint
make typecheck
make test
```
