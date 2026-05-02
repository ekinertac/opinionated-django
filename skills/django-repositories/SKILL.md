---
name: django-repositories
description: Design and write repository classes that own all ORM access in a Django project. Use when adding a new entity (the repo is the data-layer hinge in the feature stack), refactoring a service or view that imports models directly, dealing with prefetching for nested DTOs, choosing where a query lives, or any time the user mentions repositories, queries, or DAO/data-access patterns. Repositories are the boundary where Django ORM objects die and Pydantic DTOs are born.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Repositories

The repository layer is the only place in the project that imports Django models. Every other layer — services, views, Celery tasks, admin code — talks in DTOs. This is what lets services be database-free in tests, lets DTOs travel safely through Celery, and lets pyrefly catch type mismatches across the codebase.

If you find yourself reaching for `.objects` outside `repositories.py`, stop. That's the symptom that something is in the wrong layer.

## File and Class Shape

File: `src/apps/<app>/repositories.py`

```python
from decimal import Decimal

from django.db import transaction

from .dtos import ProductDTO
from .models import Product


class ProductRepository:
    def get_by_id(self, pk: int) -> ProductDTO:
        try:
            obj = Product.objects.get(pk=pk)
        except Product.DoesNotExist as e:
            raise LookupError(f"Product {pk} not found") from e
        return ProductDTO.model_validate(obj)

    def list_all(self) -> list[ProductDTO]:
        return [ProductDTO.model_validate(o) for o in Product.objects.all()]

    def create(self, *, name: str, price: Decimal, stock: int) -> ProductDTO:
        obj = Product.objects.create(name=name, price=price, stock=stock)
        return ProductDTO.model_validate(obj)
```

Notes:
- Plain class, no inheritance, no abstract base. Don't write a `BaseRepository` — too speculative; each repo's surface is shaped by its aggregate.
- Methods take primitives in (`pk: int`, `name: str`), return DTOs out. Never accept or return ORM instances.
- `LookupError` on missing rows. The exception handler in `config/exception_handler.py` maps it to HTTP 404; services pass it through unchanged.

## The Conversion Primitive

`DTO.model_validate(orm_obj)` is the only way an ORM object turns into a DTO. The DTO has `model_config = ConfigDict(from_attributes=True)` so Pydantic reads attributes off the model.

For relations, see "Reverse Relations and `RelatedManager`" below.

## Method Signatures

- **In:** primitives — `int` for IDs, `Decimal` / `str` / `bool` for values, `datetime` for timestamps. NEVER `User` model instances; pass `user_id: int`.
- **Out:** `DTO`, `list[DTO]`, `Page[DTO]` (paginated), or `None` for void operations — but consider returning the affected DTO instead.
- **NEVER:** querysets, model instances, model fields. If a method's return type involves Django ORM types, the boundary is leaking.
- **kwargs are keyword-only** for write methods to keep call sites readable: `def create(self, *, name: str, price: Decimal)`.

## Naming

| Prefix | Returns | Example |
|---|---|---|
| `get_by_*` | one DTO, raises `LookupError` if missing | `get_by_id`, `get_by_slug` |
| `list_*` | `list[DTO]` (possibly empty) | `list_all`, `list_active`, `list_for_user` |
| `count_*` | `int` | `count_active` |
| `exists_*` | `bool` | `exists_with_email` |
| `create` / `update` / `delete` | the affected DTO (or `None` for delete) | |
| `bulk_*` | `list[DTO]` for batch writes | `bulk_create`, `bulk_update` |

Consistent prefixes mean call sites read clearly: `get_*` raises, `list_*` doesn't.

## Transactions

```python
@transaction.atomic
def create_with_items(self, *, name: str, items: list[ItemInput]) -> OrderDTO:
    order = Order.objects.create(name=name)
    OrderItem.objects.bulk_create(
        [OrderItem(order=order, sku=i.sku, qty=i.qty) for i in items]
    )
    return OrderDTO.model_validate(order)
```

Rules:
- `@transaction.atomic` on any method that issues more than one write.
- A method called *inside* an open atomic block participates in the outer transaction — Django wraps it in a savepoint. Re-decorating with `@transaction.atomic` is safe and explicit.
- Side-effects that must respect rollback (sending Celery tasks, calling external APIs, writing to caches) DO NOT belong in the repo. The service handles those, often via `transaction.on_commit` (see **django-signals**).

## Prefetching for Nested DTOs

When a DTO has nested relations, the repo MUST prefetch them, or you get N+1 queries at validation time.

```python
class OrderRepository:
    def list_with_items(self) -> list[OrderDTO]:
        qs = (
            Order.objects
            .select_related("customer")          # forward FK -> JOIN
            .prefetch_related("items__product")  # reverse FK / M2M -> separate queries, in-memory join
        )
        return [OrderDTO.model_validate(o) for o in qs]
```

- **`select_related`** for forward `ForeignKey` and `OneToOneField`. Single SQL query, JOIN.
- **`prefetch_related`** for reverse relations (`order.items`), `ManyToManyField`, and chained relations.
- The shape of the DTO drives the prefetch. If `OrderDTO` has `items: list[OrderItemDTO]` with `product: ProductDTO`, you need `prefetch_related("items__product")`.

## Reverse Relations and `RelatedManager`

Django gives you a `RelatedManager`, not a list, for `order.items`. Pydantic doesn't know how to validate a manager. The DTO needs a `mode="before"` validator that turns the manager into a list:

```python
# src/apps/orders/dtos.py
from pydantic import BaseModel, ConfigDict, field_validator


class OrderDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    items: list["OrderItemDTO"]

    @field_validator("items", mode="before")
    @classmethod
    def coerce_related_manager(cls, v):
        if hasattr(v, "all"):
            return list(v.all())
        return v
```

This validator lives on the DTO, but it's tightly coupled to how the repository hands data over. Apply it on every DTO field that points at a reverse relation or M2M.

**Important:** if you forget to prefetch, this validator silently triggers `.all()` per row — N+1 returns invisibly. The full pattern is **prefetch in the repository, validate on the DTO**. Both halves matter.

## Query Helpers (replacing custom Managers)

Anything that would have been a custom Manager method on the model lives here as a plain method. The model has no `objects = MyManager()` (see **django-models**); the queries live where they can be tested without a database mock.

```python
class ProductRepository:
    def list_active(self) -> list[ProductDTO]:
        qs = Product.objects.filter(is_active=True)
        return [ProductDTO.model_validate(o) for o in qs]

    def search(self, *, query: str, limit: int = 50) -> list[ProductDTO]:
        qs = Product.objects.filter(name__icontains=query)[:limit]
        return [ProductDTO.model_validate(o) for o in qs]

    def list_for_user(self, *, user_id: int) -> list[ProductDTO]:
        qs = Product.objects.filter(owner_id=user_id)
        return [ProductDTO.model_validate(o) for o in qs]
```

When a query gets long enough that you want to compose pieces, write helper queryset functions in the same file (private, leading underscore). Do NOT put them on a custom Manager — that lives on the model and we don't allow that.

## Bulk Operations

For loops of `objects.create()` are O(N) round-trips. Use `bulk_create` / `bulk_update` for any batch larger than ~5 items.

```python
def bulk_create(self, *, products: list[ProductInput]) -> list[ProductDTO]:
    objs = [Product(name=p.name, price=p.price, stock=p.stock) for p in products]
    created = Product.objects.bulk_create(objs)
    return [ProductDTO.model_validate(o) for o in created]
```

Caveats:
- `bulk_create` does NOT call `save()` (good — the project doesn't override `save()` anyway).
- `bulk_update` skips signals — but the project uses reliable signals from the service layer, so model signals shouldn't be in play anyway.
- `bulk_create(..., update_conflicts=True)` for upserts on Django 4.1+.

## Aggregates: One Repo per Aggregate Root

An aggregate is a cluster of related models that live and die together. The "root" is the entry point; child models belong to the root.

| Example | Repos | Reasoning |
|---|---|---|
| `Order` + `OrderItem` | One `OrderRepository`. Items via methods like `add_item`, `remove_item`. | Items only exist inside an order; nothing outside the order asks for "all items where..." |
| `Product` + `Category` | Two repos. | Independent lifecycles; categories are queried, listed, edited on their own. |
| `User` + `Profile` (1:1) | One `UserRepository`. | The profile is internal; nothing needs to "list profiles". |
| `Article` + `Tag` (M2M) | Two repos. `ArticleRepository.set_tags(article_id, tag_ids)` for the relation. | Both sides are independently queried. |

Rule of thumb: **if the child can be queried, listed, or modified outside the parent's lifecycle, it's its own aggregate.**

## What NOT to Put Here

- **Business logic / validation** — services own this. Repos accept `price: Decimal` and store it; they don't decide whether the price is allowed.
- **Permission checks** — services enforce who can do what.
- **Signal sending** — `signal.send_reliable(...)` is service-layer; the repo just writes the row.
- **HTTP awareness** — no `request`, no `Response`, no status codes.
- **DTO transformations beyond `model_validate`** — repos build DTOs from ORM objects. They don't compose, derive, or shape DTOs further. That's service or view.

## Pagination

Two strategies. Use offset for small/bounded lists (admin tables, dashboards). Use cursor for unbounded lists (infinite scroll, public feeds, anything where the result set is large or growing).

Define both shapes in `src/config/dtos.py` so every app shares them:

```python
# src/config/dtos.py
from typing import Generic, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Offset pagination — knows total, supports random-access pages."""
    items: list[T]
    total: int
    offset: int
    limit: int


class CursorPage(BaseModel, Generic[T]):
    """Cursor pagination — opaque cursor, no total, no random access. Stable under writes."""
    items: list[T]
    next_cursor: str | None = None
```

### Offset pagination

```python
class ProductRepository:
    def list_paginated(self, *, offset: int = 0, limit: int = 50) -> Page[ProductDTO]:
        qs = Product.objects.order_by("-id")
        total = qs.count()
        items = [ProductDTO.model_validate(o) for o in qs[offset:offset + limit]]
        return Page(items=items, total=total, offset=offset, limit=limit)
```

Trade-offs:
- ✅ Total count for "page X of Y" UI.
- ✅ Random-access pages (`?offset=500&limit=50`).
- ❌ `count()` on a large table is slow (full scan unless an index covers the filter).
- ❌ **Inconsistent under concurrent writes** — if a row is inserted at offset 0 between `?offset=50` and `?offset=100`, the user sees one row twice or skips one.
- ❌ Deep pagination is `O(offset)` in Postgres — `OFFSET 100000` reads and discards 100,000 rows.

Use for moderate sizes (a few thousand rows max) where the count is needed.

### Cursor pagination

```python
class ProductRepository:
    def list_paginated_cursor(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> CursorPage[ProductDTO]:
        qs = Product.objects.order_by("-id")
        if cursor is not None:
            qs = qs.filter(id__lt=int(cursor))
        # Fetch one extra to detect whether there's a next page.
        rows = list(qs[: limit + 1])
        has_next = len(rows) > limit
        rows = rows[:limit]
        next_cursor = str(rows[-1].id) if has_next and rows else None
        return CursorPage(
            items=[ProductDTO.model_validate(o) for o in rows],
            next_cursor=next_cursor,
        )
```

Trade-offs:
- ✅ Stable under concurrent writes — each cursor points to a real row, not a position.
- ✅ Constant time per page regardless of depth.
- ✅ No `count()` cost.
- ❌ No total. No "page 5 of 200" UI.
- ❌ No backwards navigation without an extra mechanism (a `prev_cursor` requires reversing the query).
- ❌ The order-by column MUST be unique and indexed — if you order by `created_at` alone, ties produce duplicate cursors. Use `(created_at, id)` ordering and a tuple cursor for stability.

The cursor here is just `str(id)` — opaque to the client, internally a primary key. If you want it truly opaque, base64-encode it: `base64.urlsafe_b64encode(str(rows[-1].id).encode()).decode()`. Decode in the next request.

### Picking between them

| Case | Use |
|---|---|
| Admin table, paginated UI, "page 3 of 17" | Offset (`Page[T]`) |
| Infinite-scroll feed, public listings, mobile API | Cursor (`CursorPage[T]`) |
| API that exposes both? | Two methods on the repo (`list_paginated` and `list_paginated_cursor`) — let the view route between them based on query params |

The view passes pagination params through from query string. DRF's pagination machinery is bypassed because pagination is part of the repo/service contract, not view config — keeps it visible in tests.

## Testing

Repository tests live in `src/apps/<app>/tests/test_repo.py`, use a real database (`@pytest.mark.django_db`), and assert on DTO type at least once. See **django-pytest** for the full testing convention.

## Verify

```bash
make check    # lint + format-check + typecheck
make test
```

## Checklist

- [ ] Class lives at `src/apps/<app>/repositories.py`
- [ ] One repository per aggregate root
- [ ] All methods take primitives in, return `DTO` / `list[DTO]` / `Page[DTO]` out
- [ ] No querysets or ORM objects in any return type or argument
- [ ] `LookupError` raised on missing rows
- [ ] `@transaction.atomic` on multi-write methods
- [ ] DTOs with reverse relations have the `coerce_related_manager` validator
- [ ] List methods that build nested DTOs use `select_related` / `prefetch_related`
- [ ] Bulk writes use `bulk_create` / `bulk_update`
- [ ] Naming follows `get_by_*` / `list_*` / `count_*` / `exists_*` / `create` / `update` / `delete` / `bulk_*`
- [ ] No business logic, permission checks, signal sending, or HTTP concerns
- [ ] Repository tests exist with real DB and assert DTO type
