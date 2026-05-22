---
name: django-stack
description: Self-contained one-skill version of the opinionated-django stack. All 16 layered skills compressed into a single file with canonical code samples — models, repos, services, API, signals, tests, cache, email, settings, Docker, deploy, CI, migrations. Use when you want the whole stack in one skill instead of installing 17 separate skills, or when you need to read everything at once.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Opinionated Django Stack — All-in-One

Self-contained. Everything below is the source of truth — no need to invoke other skills if this one is installed.

## Stack

```
API     → DRF ViewSet, ServiceMixin
Service → svcs DI, _item(s) methods
DTO     → Pydantic v2, from_attributes
Repo    → ORM lives here, returns DTOs
Model   → BaseModel + BigAutoField
Signals → ReliableSignal (Celery, on_commit)
```

Dev: Docker Compose. Prod: Ansible + self-hosted infra.

## Project Layout

```
Dockerfile
docker-compose.yml
compose.prod.yml
entrypoint.sh
Makefile
.dockerignore
.env.example
pyproject.toml
deploy/
  inventory.yml ansible.cfg
  group_vars/all/{vars.yml,vault.yml}
  playbooks/{provision.yml,deploy.yml}
  roles/{docker,haproxy,postgres,redis,glitchtip,...}
.github/workflows/ci.yml
src/
  manage.py
  conftest.py
  templates/emails/<name>/{subject,body}.txt
  config/
    settings/{__init__,base,local,production}.py
    services.py    # svcs registry + get[T]()
    api.py         # ServiceMixin + validate + dto_response
    models.py      # BaseModel
    cache.py       # cache helpers
    signals.py     # ReliableSignal
    types.py       # AuthedRequest
    exception_handler.py
    urls.py wsgi.py asgi.py celery.py __init__.py
    gunicorn_config.py
  apps/<app>/
    apps.py models.py admin.py
    dtos.py repositories.py
    services.py signals.py receivers.py
    serializers.py views.py urls.py
    tests/{test_repo,test_service,test_api}.py
```

---

## 1. Scaffold

**Use official tools first, then customize. Never transcribe what `django-admin startproject` writes.**

```bash
uv init
uv add 'django>=6.0' 'djangorestframework>=3.16' 'drf-spectacular>=0.28' \
       'drf-nested-routers>=0.94' 'pydantic>=2.0' 'drf-pydantic>=2.0' 'svcs>=25.1' \
       'celery>=5.4' 'psycopg[binary]>=3.2' python-decouple

uv add --dev ruff 'pyrefly>=0.42' django-stubs pytest pytest-django pytest-celery freezegun pytest-mock

mkdir -p src && cd src
uv run django-admin startproject config .
mv config/settings.py config/settings/__init__.py
mkdir -p config/settings && mv config/settings/__init__.py config/settings/base.py
touch config/settings/__init__.py
```

Then edit `manage.py`, `wsgi.py`, `asgi.py` so they reference `config.settings.local` instead of `config.settings`.

### `src/config/services.py`

```python
import svcs

registry = svcs.Registry()


def get[T](service_type: type[T]) -> T:
    return svcs.Container(registry).get(service_type)
```

### `src/config/models.py`

```python
from django.db import models


class BaseModel(models.Model):
    """Timestamps only. Never bloat."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
```

### `src/config/api.py`

```python
from typing import Any, TypeVar

from pydantic import BaseModel
from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.serializers import Serializer


def validate(serializer_class: type[Serializer], data: Any) -> dict[str, Any]:
    s = serializer_class(data=data)
    s.is_valid(raise_exception=True)
    return s.validated_data


def dto_response(dto: BaseModel | list[BaseModel], status: int = http_status.HTTP_200_OK) -> Response:
    if isinstance(dto, list):
        return Response([d.model_dump() for d in dto], status=status)
    return Response(dto.model_dump(), status=status)


class ServiceMixin:
    service_class: type
    create_serializer: type[Serializer] | None = None
    update_serializer: type[Serializer] | None = None

    @property
    def service(self):
        from config.services import get
        return get(self.service_class)

    def list(self, request):
        return dto_response(self.service.list_items())

    def retrieve(self, request, pk=None):
        return dto_response(self.service.get_item(int(pk)))

    def create(self, request):
        data = validate(self.create_serializer, request.data)
        return dto_response(self.service.create_item(**data), http_status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        data = validate(self.update_serializer, request.data)
        return dto_response(self.service.update_item(int(pk), **data))

    def partial_update(self, request, pk=None):
        return self.update(request, pk)

    def destroy(self, request, pk=None):
        self.service.delete_item(int(pk))
        return Response(status=http_status.HTTP_204_NO_CONTENT)
```

### `src/config/signals.py`

```python
import json

from celery import shared_task
from django.db import transaction
from django.dispatch import Signal
from django.utils.module_loading import import_string


@shared_task
def _dispatch_reliable_receiver(receiver_path: str, kwargs_json: str) -> None:
    receiver = import_string(receiver_path)
    receiver(**json.loads(kwargs_json))


class ReliableSignal(Signal):
    def send_reliable(self, sender, **kwargs) -> None:
        payload = json.dumps(kwargs)
        for _, receiver in self._live_receivers(sender):
            path = f"{receiver.__module__}.{receiver.__qualname__}"
            transaction.on_commit(
                lambda p=path: _dispatch_reliable_receiver.delay(p, payload)
            )
```

### `src/config/exception_handler.py`

```python
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def custom_exception_handler(exc, context):
    if isinstance(exc, ValueError):
        return Response({"detail": str(exc)}, status=400)
    if isinstance(exc, LookupError):
        return Response({"detail": str(exc)}, status=404)
    if isinstance(exc, PermissionError):
        return Response({"detail": str(exc)}, status=403)
    return drf_exception_handler(exc, context)
```

### `src/config/types.py`

```python
from django.contrib.auth.models import User
from django.http import HttpRequest


class AuthedRequest(HttpRequest):
    user: User  # type: ignore[assignment]
```

### `src/config/celery.py` + `__init__.py`

```python
# celery.py
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["apps"])

# __init__.py
from .celery import app as celery_app
__all__ = ("celery_app",)
```

---

## 2. Settings (`src/config/settings/`)

Split base / local / production. Banner section headers (77 chars `=`). `python-decouple` for env vars.

### `base.py` — edits to what `django-admin` generated

```python
from decouple import config

# Edit existing lines:
SECRET_KEY = config("SECRET_KEY", default="change-me")
DEBUG = False
ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    # ...keep what django-admin generated...
    # Third-party
    "rest_framework",
    "drf_spectacular",
    # Project apps (short dotted path, NEVER .apps.FooConfig)
    # "apps.products",
    # "apps.orders",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("POSTGRES_DB", default="app"),
        "USER": config("POSTGRES_USER", default="app"),
        "PASSWORD": config("POSTGRES_PASSWORD", default="app"),
        "HOST": config("POSTGRES_HOST", default="postgres"),
        "PORT": config("POSTGRES_PORT", default="5432"),
    }
}

# Already in Django 3.2+ default — leave alone:
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- DRF ---
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "config.exception_handler.custom_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/hour",
        "user": "1000/hour",
        "login": "5/min",
        "signup": "10/hour",
        "password_reset": "3/hour",
    },
}

SPECTACULAR_SETTINGS = {"TITLE": "API", "VERSION": "1.0.0"}

# --- Celery ---
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://redis:6379/1")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="redis://redis:6379/2")
```

### `local.py`

```python
from .base import *  # noqa: F401, F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
```

### `production.py`

See [Deploy](#11-deploy) for the production block.

---

## 3. Models (`src/apps/<app>/models.py`)

**Inherit `BaseModel`. Member order: choices → fields → manager (rare) → Meta → methods. Field order: identifiers → time → status → domain → relations.**

```python
from django.db import models

from config.models import BaseModel


class Order(BaseModel):
    # Choices
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        SHIPPED = "shipped", "Shipped"
        CANCELLED = "cancelled", "Cancelled"

    # Identifiers
    idempotency_key = models.CharField(
        verbose_name="idempotency key",
        max_length=255,
        help_text="Client-generated key to prevent duplicate order submissions.",
    )

    # Time (created_at/updated_at inherited)
    paid_at = models.DateTimeField(null=True, blank=True)

    # Status
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )

    # Domain
    total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = "order"
        verbose_name_plural = "orders"
        indexes = [
            models.Index(fields=["-created_at"], name="idx_%(class)s_recent"),
            models.Index(fields=["status", "-created_at"], name="idx_%(class)s_status_recent"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                name="uq_%(class)s_idempotency",
            ),
        ]

    def __str__(self) -> str:
        return f"Order {self.id}"
```

### Banned in models — and where it lives instead

| Banned | Lives in |
|---|---|
| Custom manager / `objects = MyManager()` | **Repository** as a method |
| `save()` override / pre-save normalization | **Service** in the create/update method |
| `pre_save` / `post_save` signals | **Reliable signal + receiver** (Celery task) |
| Computed `@property` | **DTO** (Pydantic computed field) or service method |
| Validators that need other rows | **Service** in create/update |

Field-level `MaxValueValidator` / `MinValueValidator` / `RegexValidator` and `Meta.CheckConstraint` ARE allowed — they're declarative.

### Admin (`src/apps/<app>/admin.py`)

```python
from django.contrib import admin
from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    fields = ("id", "product", "quantity", "price_at_purchase")
    readonly_fields = ("id",)
    extra = 0
    show_change_link = True


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "status", "total")
    list_per_page = 25
    search_fields = ("id", "idempotency_key")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)
    inlines = [OrderItemInline]
```

---

## 4. DTOs (`src/apps/<app>/dtos.py`)

Pydantic v2 with `from_attributes=True`. Use `drf_pydantic.BaseModel` (drop-in) so DTOs can be referenced in `@extend_schema(responses=...)`.

```python
from decimal import Decimal
from typing import Generic, TypeVar

from drf_pydantic import BaseModel
from pydantic import ConfigDict, field_validator


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class OrderItemDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    quantity: int
    price_at_purchase: Decimal


class OrderDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    idempotency_key: str
    status: str
    total: Decimal
    items: list[OrderItemDTO]

    @field_validator("items", mode="before")
    @classmethod
    def coerce_related_manager(cls, v):
        # Django gives a RelatedManager, not a list — coerce here.
        # PAIR THIS WITH prefetch_related IN THE REPO or N+1 sneaks in.
        if hasattr(v, "all"):
            return list(v.all())
        return v
```

---

## 5. Repositories (`src/apps/<app>/repositories.py`)

**Only layer with `.objects` / model imports.** Primitives in, DTOs out. `LookupError` on missing. `@transaction.atomic` on multi-writes.

```python
from decimal import Decimal

from django.db import transaction

from .dtos import OrderDTO, Page, CursorPage
from .models import Order


class OrderRepository:
    def get_by_id(self, pk: int) -> OrderDTO:
        try:
            obj = (
                Order.objects
                .select_related("customer")
                .prefetch_related("items__product")
                .get(pk=pk)
            )
        except Order.DoesNotExist as e:
            raise LookupError(f"Order {pk} not found") from e
        return OrderDTO.model_validate(obj)

    def list_all(self) -> list[OrderDTO]:
        qs = Order.objects.prefetch_related("items__product")
        return [OrderDTO.model_validate(o) for o in qs]

    def list_active(self) -> list[OrderDTO]:
        qs = Order.objects.filter(status="pending").prefetch_related("items__product")
        return [OrderDTO.model_validate(o) for o in qs]

    @transaction.atomic
    def create(self, *, idempotency_key: str, total: Decimal) -> OrderDTO:
        obj = Order.objects.create(idempotency_key=idempotency_key, total=total)
        return OrderDTO.model_validate(obj)

    @transaction.atomic
    def update(self, pk: int, **fields) -> OrderDTO:
        Order.objects.filter(pk=pk).update(**fields)
        return self.get_by_id(pk)

    def delete(self, pk: int) -> None:
        Order.objects.filter(pk=pk).delete()

    def list_paginated(self, *, offset: int = 0, limit: int = 50) -> Page[OrderDTO]:
        qs = Order.objects.order_by("-id")
        total = qs.count()
        items = [OrderDTO.model_validate(o) for o in qs[offset:offset + limit]]
        return Page(items=items, total=total, offset=offset, limit=limit)

    def list_paginated_cursor(self, *, cursor: str | None = None, limit: int = 50) -> CursorPage[OrderDTO]:
        qs = Order.objects.order_by("-id")
        if cursor is not None:
            qs = qs.filter(id__lt=int(cursor))
        rows = list(qs[: limit + 1])
        has_next = len(rows) > limit
        rows = rows[:limit]
        next_cursor = str(rows[-1].id) if has_next and rows else None
        return CursorPage(
            items=[OrderDTO.model_validate(o) for o in rows],
            next_cursor=next_cursor,
        )

    def bulk_create(self, *, orders: list[dict]) -> list[OrderDTO]:
        objs = [Order(**o) for o in orders]
        created = Order.objects.bulk_create(objs)
        return [OrderDTO.model_validate(o) for o in created]
```

### Aggregates

One repo per aggregate root. Order owns OrderItem (managed via `add_item`/`remove_item` on `OrderRepository`). Product is its own repo. If the child has independent queries / lifecycle → its own repo.

### Naming

- `get_by_*` — raises `LookupError`
- `list_*` — returns list (possibly empty)
- `count_*` — int
- `exists_*` — bool
- `create` / `update` / `delete` — single mutation
- `bulk_*` — batch

---

## 6. Services (`src/apps/<app>/services.py`)

**Zero ORM. Zero model imports. Repos via `__init__`.**

Resource services use the `_item(s)` suffix on **ALL** methods. Class name carries the resource. `archive_item`, not `archive_order`.

```python
from decimal import Decimal

from django.db import transaction

from .repositories import OrderRepository
from .signals import order_created


class OrderService:
    def __init__(self, repo: OrderRepository, product_repo):
        self.repo = repo
        self.product_repo = product_repo

    def list_items(self) -> list:
        return self.repo.list_all()

    def get_item(self, pk: int):
        return self.repo.get_by_id(pk)

    def create_item(self, *, idempotency_key: str, items: list[dict], user_id: int):
        if not items:
            raise ValueError("Order must have at least one item.")

        # Cross-repo validation
        for item in items:
            product = self.product_repo.get_by_id(item["product_id"])
            if product.stock < item["quantity"]:
                raise ValueError(f"Insufficient stock for product {product.id}")

        total = sum(
            self.product_repo.get_by_id(i["product_id"]).price * i["quantity"]
            for i in items
        )

        with transaction.atomic():
            order = self.repo.create(idempotency_key=idempotency_key, total=total)
            for item in items:
                self.product_repo.decrement_stock(item["product_id"], item["quantity"])

            order_created.send_reliable(sender=None, order_id=order.id)

        return order

    def update_item(self, pk: int, **fields):
        return self.repo.update(pk, **fields)

    def delete_item(self, pk: int) -> None:
        self.repo.delete(pk)

    def archive_item(self, pk: int, *, user_id: int):
        order = self.repo.get_by_id(pk)
        if order.user_id != user_id:
            raise PermissionError(f"User {user_id} cannot archive order {pk}")
        return self.repo.update(pk, status="archived")
```

### Service contract

| For ServiceMixin | Required methods |
|---|---|
| All five CRUD | `list_items`, `get_item(pk)`, `create_item(**fields)`, `update_item(pk, **fields)`, `delete_item(pk)` |

Plus domain ops with the same suffix: `archive_item`, `restock_item`, `publish_item`, `bulk_create_items`, etc.

**Non-resource services** (notification, payment, search): skip the convention. `NotificationService.send(...)`, `PaymentService.charge(...)`, `SearchService.query(...)`.

### Register in svcs (`src/config/services.py`)

```python
from apps.orders.repositories import OrderRepository
from apps.orders.services import OrderService
from apps.products.repositories import ProductRepository

registry.register_factory(OrderRepository, OrderRepository)
registry.register_factory(ProductRepository, ProductRepository)


def _order_service_factory(container):
    return OrderService(
        repo=container.get(OrderRepository),
        product_repo=container.get(ProductRepository),
    )


registry.register_factory(OrderService, _order_service_factory)
```

---

## 7. API (`src/apps/<app>/{serializers,views,urls}.py`)

### Serializers — INPUT ONLY

Never `ModelSerializer`. Distinct Create and Update serializers (no big `required=False` bag).

```python
from decimal import Decimal
from rest_framework import serializers


class OrderItemInputSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class CreateOrderSerializer(serializers.Serializer):
    idempotency_key = serializers.CharField(max_length=255)
    items = OrderItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Order must have at least one item.")
        return value


class UpdateOrderSerializer(serializers.Serializer):
    status = serializers.CharField(max_length=20, required=False)
```

**No DB queries in `validate`.** Check uniqueness etc. in the service.

### ViewSets — `ServiceMixin` + override to customize

```python
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config.api import ServiceMixin, dto_response, validate
from config.types import AuthedRequest

from .dtos import OrderDTO
from .serializers import CreateOrderSerializer, UpdateOrderSerializer
from .services import OrderService


class OrderViewSet(ServiceMixin, viewsets.ViewSet):
    service_class = OrderService
    create_serializer = CreateOrderSerializer
    update_serializer = UpdateOrderSerializer

    # Override to pass user_id — most common customization
    def create(self, request: AuthedRequest):
        data = validate(self.create_serializer, request.data)
        return dto_response(
            self.service.create_item(user_id=request.user.id, **data),
            status.HTTP_201_CREATED,
        )

    @extend_schema(responses={200: OrderDTO.drf_serializer}, tags=["Orders"])
    @action(detail=True, methods=["post"])
    def archive(self, request: AuthedRequest, pk=None):
        return dto_response(
            self.service.archive_item(int(pk), user_id=request.user.id)
        )
```

**Never `ModelViewSet`. Never `GenericViewSet` with a `queryset`.**

### URLs (`src/apps/<app>/urls.py`)

```python
from rest_framework.routers import DefaultRouter
from rest_framework_nested import routers

from .views import OrderViewSet, OrderItemViewSet

router = DefaultRouter()
router.register(r"orders", OrderViewSet, basename="order")

# Nested
orders_router = routers.NestedDefaultRouter(router, r"orders", lookup="order")
orders_router.register(r"items", OrderItemViewSet, basename="order-items")

urlpatterns = router.urls + orders_router.urls
```

Then in `src/config/urls.py`:

```python
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from .views import healthz, readyz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/v1/", include("apps.orders.urls")),
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
]
```

### Permissions — two tiers

```python
from rest_framework.permissions import IsAdminUser, IsAuthenticated


class OrderViewSet(ServiceMixin, viewsets.ViewSet):
    def get_permissions(self):
        # Tier 1 — request-level (auth, role)
        if self.action in ("create", "update", "destroy", "archive"):
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]
```

Tier 2 — data-level — lives in the service:

```python
def archive_item(self, pk: int, *, user_id: int):
    order = self.repo.get_by_id(pk)
    if order.user_id != user_id:
        raise PermissionError(...)
    return self.repo.update(pk, status="archived")
```

### Throttling

```python
from rest_framework.throttling import ScopedRateThrottle


class AuthViewSet(viewsets.ViewSet):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @action(detail=False, methods=["post"])
    def login(self, request):
        ...
```

### File upload (small)

```python
from rest_framework.parsers import MultiPartParser


class DocumentViewSet(viewsets.ViewSet):
    parser_classes = [MultiPartParser]

    def create(self, request: AuthedRequest):
        data = validate(UploadSerializer, request.data)
        return dto_response(
            self.service.upload_item(user_id=request.user.id, **data),
            status.HTTP_201_CREATED,
        )
```

Large files → S3 signed URLs. Service uses `boto3.client("s3").generate_presigned_post(...)`.

### Health endpoints (`src/config/views.py`)

```python
from django.db import connection
from django.http import HttpResponse, JsonResponse


def healthz(_request):
    return HttpResponse("ok", content_type="text/plain")


def readyz(_request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:
        return JsonResponse({"status": "not ready", "error": str(exc)}, status=503)
    return JsonResponse({"status": "ready"})
```

---

## 8. Reliable Signals

### Define (`src/apps/<app>/signals.py`)

```python
from config.signals import ReliableSignal

order_created = ReliableSignal()
```

### Send (inside `transaction.atomic`)

```python
with transaction.atomic():
    order = self.repo.create(...)
    order_created.send_reliable(sender=None, order_id=order.id)
```

### Receive (`src/apps/<app>/receivers.py`)

```python
from django.dispatch import receiver

from config.services import get
from apps.email.services import EmailService

from .signals import order_created


@receiver(order_created)
def on_order_created(order_id: int, **kwargs):
    # MUST be idempotent — at-least-once delivery
    get(EmailService).send(
        template="order_confirmation",
        to=...,
        context={"order_id": order_id},
        idempotency_key=f"order_confirmation:{order_id}",
    )
```

### Load in `apps.py`

```python
class OrdersConfig(AppConfig):
    name = "apps.orders"

    def ready(self):
        from . import receivers  # noqa: F401
```

**Args MUST be JSON-serializable.** Pass IDs, not models.

---

## 9. Cache (`src/config/cache.py`)

Caching lives in the **repository layer**, not service or view. Use the `redis_cache` instance (NOT the broker).

```python
from typing import Type, TypeVar
from django.core.cache import cache
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_TTL = 300


def cache_get(key: str, dto_type: Type[T]) -> T | None:
    return cache.get(key)


def cache_set(key: str, dto: BaseModel, ttl: int = DEFAULT_TTL) -> None:
    cache.set(key, dto, timeout=ttl)


def cache_delete(*keys: str) -> None:
    cache.delete_many(keys)


def cache_get_or_set(key: str, fetch, dto_type: Type[T], ttl: int = DEFAULT_TTL) -> T:
    cached = cache.get(key)
    if cached is not None:
        return cached
    fresh = fetch()
    cache.set(key, fresh, timeout=ttl)
    return fresh
```

### Cache in a repo (cache-aside + invalidate on write)

```python
from config.cache import cache_delete, cache_get_or_set


def _product_key(pk: int) -> str:
    return f"product:{pk}"


class ProductRepository:
    def get_by_id(self, pk: int) -> ProductDTO:
        return cache_get_or_set(
            _product_key(pk),
            fetch=lambda: self._fetch_by_id(pk),
            dto_type=ProductDTO,
            ttl=300,
        )

    def update(self, pk: int, **fields) -> ProductDTO:
        Product.objects.filter(pk=pk).update(**fields)
        cache_delete(_product_key(pk))
        return self._fetch_by_id(pk)
```

### Keys

- Single record: `<resource>:<id>` (`product:42`)
- Alt identifier: `<resource>:slug:<slug>` (`product:slug:widget-pro`)
- Per-user: `user:<uid>:<resource>:<id>` (key MUST include user_id — cross-user leak otherwise)
- List: `<resource>:list:<filter>` (cache list only after profiling — invalidation cost is high)

### NEVER cache

Money, inventory counts, auth state, anything that drives correctness. The TTL window IS the bug.

---

## 10. Testing

**Real test DB at every layer. NO mocks of own repos / services.** Mocks only at external boundaries (Stripe, SES, HTTP clients).

### `pyproject.toml`

```toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.local"
python_files = ["test_*.py"]
pythonpath = ["src"]
addopts = ["-ra", "--strict-markers", "--strict-config", "--reuse-db"]
markers = ["slow: deselect with '-m \"not slow\"'"]
```

### `src/conftest.py`

```python
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from freezegun import freeze_time

from apps.products.dtos import ProductDTO
from apps.products.repositories import ProductRepository


@pytest.fixture
def frozen_time():
    with freeze_time("2026-01-01T00:00:00Z") as f:
        yield f


@pytest.fixture
def make_product():
    """Creates a real Product row, returns its DTO. Default for service / API tests."""
    repo = ProductRepository()

    def _build(**overrides) -> ProductDTO:
        fields = {"name": "Widget", "price": Decimal("9.99"), "stock": 5}
        fields.update(overrides)
        return repo.create(**fields)

    return _build


@pytest.fixture
def override_service():
    """Substitute a service factory. ONLY for external boundaries (payments, mail)."""
    from config.services import registry

    originals: dict[type, Any] = {}

    def _override(service_type: type, fake: Any) -> None:
        originals.setdefault(service_type, registry._factories.get(service_type))
        registry.register_factory(service_type, lambda _: fake)

    yield _override
    for k, v in originals.items():
        if v is not None:
            registry._factories[k] = v
```

### `test_repo.py`

```python
@pytest.mark.django_db
def test_create_returns_dto():
    repo = ProductRepository()
    dto = repo.create(name="Widget", price=Decimal("9.99"), stock=5)

    assert isinstance(dto, ProductDTO)
    assert dto.price == Decimal("9.99")


@pytest.mark.django_db
def test_get_by_id_raises_lookup_error_on_missing():
    repo = ProductRepository()
    with pytest.raises(LookupError):
        repo.get_by_id(99999)
```

### `test_service.py` — real repo, real DB

```python
@pytest.mark.django_db
def test_create_item_rejects_negative_price():
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

### `test_api.py`

```python
from rest_framework.test import APIClient


@pytest.fixture
def api_client():
    return APIClient()


@pytest.mark.django_db
def test_create_product(api_client):
    response = api_client.post(
        "/api/v1/products/",
        data={"name": "Widget", "price": "9.99", "stock": 5},
        format="json",
    )
    assert response.status_code == 201
    assert response.data["name"] == "Widget"
```

### Reliable-signal test

```python
@pytest.mark.django_db(transaction=True)
def test_order_created_triggers_receiver(mocker):
    spy = mocker.patch("apps.orders.receivers.on_order_created", wraps=on_order_created)
    # build real repos, call service.create_item(...)
    spy.assert_called_once()


def test_receiver_is_idempotent(mocker):
    send = mocker.patch("apps.orders.receivers.send_order_confirmation")
    on_order_created(order_id=1)
    on_order_created(order_id=1)
    assert send.call_count == 1
```

---

## 11. Docker (dev)

### `Dockerfile` — multi-stage (dev + prod targets)

```dockerfile
# syntax=docker/dockerfile:1.7
ARG UV_IMAGE=ghcr.io/astral-sh/uv:python3.13-bookworm-slim
ARG PY_IMAGE=python:3.13-slim-bookworm

# ---- builder ----
FROM ${UV_IMAGE} AS builder
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/usr/local
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY . .
RUN uv sync --frozen --no-dev

# ---- prod ----
FROM ${PY_IMAGE} AS prod
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /usr/local /usr/local
COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
USER app
WORKDIR /app/src
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ---- dev ----
FROM ${UV_IMAGE} AS dev
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/usr/local
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
WORKDIR /app/src
EXPOSE 8000
```

### `docker-compose.yml` (dev)

```yaml
services:
  web:
    build:
      context: .
      target: dev
    command: uv run python manage.py runserver 0.0.0.0:8000
    volumes:
      - .:/app
    ports:
      - "8000:8000"
    env_file: [.env]
    environment:
      RUN_MIGRATIONS: "true"
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_started}

  postgres:
    image: postgres:16-alpine
    volumes:
      - postgres_data:/var/lib/postgresql/data
    env_file: [.env]
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER}"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  celery:
    build:
      context: .
      target: dev
    command: uv run celery -A config worker -l info
    volumes:
      - .:/app
    env_file: [.env]
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_started}

volumes:
  postgres_data:
```

### `entrypoint.sh`

```bash
#!/usr/bin/env bash
set -e

echo "Waiting for postgres at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
until python -c "
import os, psycopg
psycopg.connect(
    host=os.environ['POSTGRES_HOST'],
    port=os.environ['POSTGRES_PORT'],
    user=os.environ['POSTGRES_USER'],
    password=os.environ['POSTGRES_PASSWORD'],
    dbname=os.environ['POSTGRES_DB'],
).close()
" >/dev/null 2>&1; do
  sleep 1
done
echo "Postgres is ready."

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  python manage.py makemigrations
  python manage.py migrate --noinput
fi

exec "$@"
```

### `Makefile`

```makefile
.PHONY: help up up-d down build logs shell bash migrate makemigrations superuser test lint format format-check typecheck check resetdb psql

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Start the stack
	docker compose up
up-d: ## Start detached
	docker compose up -d
down: ## Stop
	docker compose down
build: ## Rebuild image
	docker compose build
logs: ## Tail logs
	docker compose logs -f

shell: ## Django shell
	docker compose exec web uv run python manage.py shell
bash: ## bash in web container
	docker compose exec web bash

migrate:
	docker compose exec web uv run python manage.py migrate
makemigrations:
	docker compose exec web uv run python manage.py makemigrations
superuser:
	docker compose exec web uv run python manage.py createsuperuser

test:
	docker compose exec web uv run pytest
lint:
	docker compose exec web uv run ruff check .
format:
	docker compose exec web uv run ruff format .
format-check:
	docker compose exec web uv run ruff format --check .
typecheck:
	docker compose exec web uv run pyrefly check src

check: lint format-check typecheck ## Lint + format check + typecheck

resetdb: ## Nuke postgres volume; bring stack back up
	docker compose down -v
	docker compose up -d

psql:
	docker compose exec postgres sh -c 'psql -U $$POSTGRES_USER $$POSTGRES_DB'
```

### `.env.example`

```
DJANGO_SETTINGS_MODULE=config.settings.local
SECRET_KEY=dev-secret-change-me
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1,web

POSTGRES_DB=app
POSTGRES_USER=app
POSTGRES_PASSWORD=app
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2
```

### Dev command rule

`docker compose exec web ...` is the default (stack is up). `docker compose run --rm web ...` only for pre-stack-up commands: `uv add`, `manage.py startapp`.

---

## 12. Deploy

### Topology (self-hosted by Ansible)

```
                  HAProxy (TLS via certbot)
                  [lb-01]
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    web-01           web-02          web-N
        │
        └─→ worker-beat-01 (singleton) + worker-02..N + redis-broker + redis-cache + db (Postgres + pgbouncer) + glitchtip

External: S3 (static/media), AWS SES (email), Let's Encrypt (TLS)
```

### `compose.prod.yml`

```yaml
services:
  web:
    image: ${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}
    command: gunicorn config.wsgi:application -c /app/src/gunicorn_config.py
    env_file: [.env.production]
    ports: ["127.0.0.1:8000:8000"]
    restart: unless-stopped
    stop_grace_period: 35s
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

  celery:
    image: ${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}
    command: celery -A config worker -l info --concurrency 4
    env_file: [.env.production]
    restart: unless-stopped

  celery-beat:
    image: ${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}
    command: celery -A config beat -l info
    env_file: [.env.production]
    restart: unless-stopped
```

No bind mounts. No exposed Postgres/Redis. Web hosts run `web`; `worker_beat` runs `celery + celery-beat`; `worker` hosts run only `celery`.

### `src/config/settings/production.py`

```python
from decouple import Csv, config
from .base import *  # noqa: F401, F403

DEBUG = False
ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", cast=Csv())

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("POSTGRES_DB"),
        "USER": config("POSTGRES_USER"),
        "PASSWORD": config("POSTGRES_PASSWORD"),
        "HOST": config("POSTGRES_HOST"),
        "PORT": config("POSTGRES_PORT", default="6432"),  # pgbouncer
        "CONN_MAX_AGE": 0,  # pgbouncer pools, not Django
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "OPTIONS": {"sslmode": config("POSTGRES_SSLMODE", default="require")},
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("REDIS_CACHE_URL"),
    }
}

CELERY_BROKER_URL = config("CELERY_BROKER_URL")

# S3
AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME")
AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME")
AWS_S3_CUSTOM_DOMAIN = config("AWS_S3_CUSTOM_DOMAIN", default=None)
AWS_QUERYSTRING_AUTH = False
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {"location": "media", "default_acl": "private"},
    },
    "staticfiles": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {"location": "static", "default_acl": "public-read"},
    },
}

# Email (SES)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL")

# JSON logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# Errors via GlitchTip (Sentry-compatible)
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.redis import RedisIntegration

sentry_sdk.init(
    dsn=config("GLITCHTIP_DSN"),
    environment=config("DEPLOY_ENVIRONMENT", default="production"),
    release=config("RELEASE_VERSION", default=None),
    integrations=[DjangoIntegration(), CeleryIntegration(), RedisIntegration()],
    traces_sample_rate=config("SENTRY_TRACES_SAMPLE_RATE", default=0.1, cast=float),
    send_default_pii=False,
)
```

### `src/gunicorn_config.py`

```python
import multiprocessing
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", 4))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 60))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
max_requests = 1000
max_requests_jitter = 100
```

### Ansible — inventory

```yaml
all:
  children:
    lb:
      hosts: {lb-01.internal: ~}
    web:
      hosts:
        web-01.internal: ~
        web-02.internal: ~
    worker_beat:
      hosts: {worker-beat-01.internal: ~}     # EXACTLY ONE
    worker:
      hosts:
        worker-02.internal: ~
        worker-03.internal: ~
    db:
      hosts: {db-01.internal: ~}
    redis_broker:
      hosts: {redis-broker-01.internal: ~}
    redis_cache:
      hosts: {redis-cache-01.internal: ~}
    glitchtip:
      hosts: {glitchtip-01.internal: ~}
  vars:
    ansible_user: deploy
```

### Vault for secrets

```bash
openssl rand -base64 32 > ~/.config/django-deploy/vault_pass
chmod 400 ~/.config/django-deploy/vault_pass

ansible-vault create deploy/group_vars/all/vault.yml \
  --vault-password-file ~/.config/django-deploy/vault_pass
```

`vault.yml` contains `vault_secret_key`, `vault_postgres_password`, `vault_redis_password`, `vault_glitchtip_dsn`, `vault_aws_access_key_id`, `vault_aws_secret_access_key`, `vault_email_host_user`, `vault_email_host_password`. Reference from plain `vars.yml`: `secret_key: "{{ vault_secret_key }}"` etc.

### Deploy playbook — zero-downtime rolling

Critical pieces:

```yaml
- name: Run migrations on one host
  hosts: web[0]
  tasks:
    - command: >
        docker compose -f /opt/app/compose.prod.yml run --rm
        -e IMAGE_TAG={{ image_tag }} web python manage.py migrate --noinput
      args: {chdir: /opt/app}
    - command: >
        docker compose -f /opt/app/compose.prod.yml run --rm web
        python manage.py collectstatic --noinput
      args: {chdir: /opt/app}

- name: Rolling restart with HAProxy drain
  hosts: web
  serial: 1
  vars:
    short_name: "{{ inventory_hostname.split('.')[0] }}"
  tasks:
    - name: Drain via HAProxy admin socket
      shell: |
        echo "set server django_web/{{ short_name }} state drain" \
          | socat stdio unix-connect:/var/run/haproxy/admin.sock
      delegate_to: "{{ groups['lb'][0] }}"

    - pause: {seconds: 30}    # let in-flight drain

    - name: Restart web
      command: docker compose -f /opt/app/compose.prod.yml up -d web
      args: {chdir: /opt/app}
      environment:
        IMAGE_TAG: "{{ image_tag }}"

    - name: Wait for /readyz
      uri:
        url: "http://{{ inventory_hostname }}:8000/readyz"
        status_code: 200
      retries: 30
      delay: 2

    - name: Resume routing
      shell: |
        echo "set server django_web/{{ short_name }} state ready" \
          | socat stdio unix-connect:/var/run/haproxy/admin.sock
      delegate_to: "{{ groups['lb'][0] }}"

  rescue:
    - name: Resume on failure
      shell: |
        echo "set server django_web/{{ short_name }} state ready" \
          | socat stdio unix-connect:/var/run/haproxy/admin.sock
      delegate_to: "{{ groups['lb'][0] }}"

- name: Restart beat host
  hosts: worker_beat
  tasks:
    - command: docker compose -f /opt/app/compose.prod.yml up -d celery celery-beat
      args: {chdir: /opt/app}

- name: Restart additional worker hosts
  hosts: worker
  tasks:
    - command: docker compose -f /opt/app/compose.prod.yml up -d celery
      args: {chdir: /opt/app}
```

### Makefile targets

```makefile
provision: ## Bootstrap infra
	cd deploy && ansible-playbook playbooks/provision.yml --vault-password-file ~/.config/django-deploy/vault_pass

deploy: ## Deploy current commit
	cd deploy && ansible-playbook playbooks/deploy.yml --vault-password-file ~/.config/django-deploy/vault_pass

rollback: ## Roll back — TAG=<sha>
	@test -n "$(TAG)" || (echo "TAG required" && exit 1)
	cd deploy && IMAGE_TAG=$(TAG) ansible-playbook playbooks/deploy.yml --vault-password-file ~/.config/django-deploy/vault_pass
```

### Backups

`pg_dump` cron on `db-01` → gzip → `rclone copy` to S3. Restore drill quarterly.

### pgbouncer

Runs alongside Postgres on `db-01`. Mode `transaction`. App connects to port `6432`. `CONN_MAX_AGE = 0` (pgbouncer pools, not Django). No `LISTEN/NOTIFY`. `DISABLE_SERVER_SIDE_CURSORS = True`.

### Provisioning (out of scope)

Install Docker, harden firewall, configure swap — use community roles (`geerlingguy.docker`, `geerlingguy.security`, `geerlingguy.swap`). Not transcribed here.

---

## 13. Email (`src/apps/email/`)

### Service

```python
import hashlib
import json

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .repositories import EmailRepository


class EmailService:
    def __init__(self, repo: EmailRepository):
        self.repo = repo

    def send(self, *, template: str, to: str, context: dict, idempotency_key: str | None = None):
        key = idempotency_key or self._derive_key(template, to, context)

        if self.repo.exists_by_idempotency_key(key):
            return self.repo.get_by_idempotency_key(key)

        if self.repo.is_suppressed(to):
            return self.repo.record(template=template, to=to, key=key, status="suppressed", sent_at=None)

        subject = render_to_string(f"emails/{template}/subject.txt", context).strip()
        body_txt = render_to_string(f"emails/{template}/body.txt", context)

        try:
            body_html = render_to_string(f"emails/{template}/body.html", context)
        except Exception:
            body_html = None

        msg = EmailMultiAlternatives(
            subject=subject, body=body_txt,
            from_email=settings.DEFAULT_FROM_EMAIL, to=[to],
        )
        if body_html:
            msg.attach_alternative(body_html, "text/html")
        msg.send(fail_silently=False)

        return self.repo.record(template=template, to=to, key=key, status="sent", sent_at=timezone.now())

    def _derive_key(self, template: str, to: str, context: dict) -> str:
        payload = json.dumps({"template": template, "to": to, "context": context}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()
```

### Trigger via reliable signal

```python
@receiver(user_registered)
def on_user_registered(user_id: int, **kwargs):
    user = get(UserRepository).get_by_id(user_id)
    get(EmailService).send(
        template="welcome",
        to=user.email,
        context={"user": user.model_dump()},
        idempotency_key=f"welcome:user:{user_id}",
    )
```

### Celery retry

```python
@shared_task(
    bind=True,
    autoretry_for=(smtplib.SMTPException, ConnectionError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def send_email_task(self, **kwargs):
    get(EmailService).send(**kwargs)
```

### SNS bounce/complaint webhook

```python
class SESWebhookViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]

    @action(detail=False, methods=["post"])
    def sns(self, request):
        envelope = json.loads(request.body)
        # VERIFY SNS SIGNATURE — non-negotiable
        if envelope.get("Type") == "Notification":
            payload = json.loads(envelope["Message"])
            event = payload.get("eventType") or payload.get("notificationType")
            if event == "Bounce":
                for r in payload["bounce"]["bouncedRecipients"]:
                    get(EmailService).suppress_address(address=r["emailAddress"], reason="bounce")
            elif event == "Complaint":
                for r in payload["complaint"]["complainedRecipients"]:
                    get(EmailService).suppress_address(address=r["emailAddress"], reason="complaint")
        return Response(status=200)
```

AWS-side once: domain verification, DKIM, SPF, DMARC, production access, SNS topic + subscription.

---

## 14. CI (`.github/workflows/ci.yml`)

```yaml
name: CI
on:
  pull_request:
  push:
    branches: [master]

jobs:
  check:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env: {POSTGRES_DB: app, POSTGRES_USER: app, POSTGRES_PASSWORD: app}
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U app" --health-interval 5s --health-timeout 5s --health-retries 5
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
    env:
      DJANGO_SETTINGS_MODULE: config.settings.local
      SECRET_KEY: ci-secret
      POSTGRES_HOST: localhost
      POSTGRES_DB: app
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      CELERY_BROKER_URL: redis://localhost:6379/1
      REDIS_CACHE_URL: redis://localhost:6379/0
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with: {enable-cache: true, cache-dependency-glob: uv.lock}
      - run: uv python install
      - run: uv sync --frozen
      - run: uv run ruff check src
      - run: uv run ruff format --check src
      - run: uv run pyrefly check src
      - run: uv run pytest
      - run: uv run python src/manage.py spectacular --validate

  build:
    runs-on: ubuntu-latest
    needs: check
    if: github.event_name == 'push' && github.ref == 'refs/heads/master'
    permissions: {contents: read, packages: write}
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          target: prod
          push: true
          tags: |
            ghcr.io/${{ github.repository }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

Then deploy: `make deploy IMAGE_TAG=<sha-from-ci>`. No auto-deploy. No `:latest`.

---

## 15. Migrations at Scale

### Safety table

| Operation | Safe in rolling deploy? |
|---|---|
| `AddField` (nullable / has default) | ✅ |
| `CreateModel` | ✅ |
| `AddIndex` (small) | ✅ |
| `AddIndex` (large) | ⚠️ — use `CREATE INDEX CONCURRENTLY` |
| Metadata `AlterField` (verbose_name, max_length+) | ✅ |
| `AddField` (NOT NULL, no default) | ❌ |
| `RemoveField` | ❌ |
| `RenameField` | ❌ |
| `AlterField` (type change) | ❌ |
| `RunPython` on large table | ❌ — use management command |

### Expand-contract for a column rename

```
A. EXPAND   — add `handle` column (nullable). Schema only.
B. WRITE    — service writes username AND handle. Reads username.
C. BACKFILL — management command (batched, idempotent) copies username → handle.
D. READ     — service reads handle. Still writes both.
E. STOP-OLD — service stops referencing username.
F. CONTRACT — migration drops username.
```

Each is a separate PR / deploy.

### `CREATE INDEX CONCURRENTLY`

```python
class Migration(migrations.Migration):
    atomic = False  # required

    operations = [
        migrations.RunSQL(
            sql="CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON tbl (col);",
            reverse_sql="DROP INDEX IF EXISTS idx_x;",
        ),
    ]
```

### NOT NULL via NOT VALID → VALIDATE

```python
# Deploy 1: add nullable + Django-level default for new writes
# Deploy 2: backfill via management command
# Deploy 3:
migrations.RunSQL(
    "ALTER TABLE x ADD CONSTRAINT x_handle_not_null CHECK (handle IS NOT NULL) NOT VALID;",
    reverse_sql="ALTER TABLE x DROP CONSTRAINT x_handle_not_null;",
),
# Deploy 4:
migrations.RunSQL("""
    ALTER TABLE x VALIDATE CONSTRAINT x_handle_not_null;
    ALTER TABLE x ALTER COLUMN handle SET NOT NULL;
    ALTER TABLE x DROP CONSTRAINT x_handle_not_null;
""", reverse_sql="ALTER TABLE x ALTER COLUMN handle DROP NOT NULL;"),
```

### Backfill management command (batched, idempotent)

```python
class Command(BaseCommand):
    def handle(self, *args, **opts):
        batch = 1000
        last_id = 0
        while True:
            qs = User.objects.filter(id__gt=last_id, handle__isnull=True).order_by("id")[:batch]
            ids = list(qs.values_list("id", flat=True))
            if not ids:
                break
            User.objects.filter(id__in=ids).update(handle=models.F("username"))
            last_id = ids[-1]
```

### `statement_timeout`

```python
operations = [
    migrations.RunSQL(sql="SET statement_timeout = '5s'; ALTER TABLE ..."),
]
```

Stuck locks fail fast.

---

## 16. Lint

```bash
make check    # ruff check + ruff format --check + pyrefly check
```

Fix anything reported. Don't silence with `# type: ignore` unless documented why.

---

## Cross-cutting Rules

1. **Official tools first.** Run `django-admin startproject`, `startapp`, `uv init`. Edit the result. Never transcribe boilerplate.
2. **Real DB testing.** Never mock own repos/services. pytest-django + `--reuse-db` + transaction rollback.
3. **`BaseModel` is minimal.** Two timestamps. Don't bloat with `is_active`/soft-delete/UUID/audit FK/JSON metadata.
4. **`_item(s)` everywhere** on resource services. Strict.
5. **One thin `ServiceMixin`.** No new mixins. No config knobs. Override the method to customize.
6. **Reliable signals** (Celery, on_commit) for cross-service side effects. Never standard Django signals.
7. **Models lose, others gain.** Custom manager → repo. `save()` → service. `post_save` → reliable receiver. `@property` (computed) → DTO or service.
8. **Two-tier permissions.** DRF classes for request-level. Service exceptions for data-level.
9. **`docker compose exec`** for dev commands. `run --rm` only for pre-stack-up cases.
10. **CI never `:latest`.** SHA-tagged images. Deploy is `make deploy IMAGE_TAG=<sha>` — manual.

## Verify

```bash
make check
make test
docker compose exec web uv run python manage.py spectacular --validate
docker compose exec web uv run python manage.py check --deploy
```
