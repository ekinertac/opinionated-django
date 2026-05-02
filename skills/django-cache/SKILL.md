---
name: django-cache
description: Application-level caching using Redis (the cache instance, not the Celery broker) — cache-aside pattern in repository methods, key naming convention, TTL strategy, explicit invalidation on writes, and what NOT to cache. Use when adding caching to repository read methods, debugging stale data, or whenever the user mentions caching, Redis cache, hot reads, or cache invalidation.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Application Caching

The cache is the **second** Redis instance in the production topology (`redis_cache` from **django-deploy** — separate from the Celery broker). Caching lives in the **repository layer**, not the service or view. Reasoning:

- Caching is a data-access optimization, and the repository owns "how data gets fetched."
- Services don't need to know whether a DTO came from the database or from cache.
- Putting caching in the service layer means every service method has cache logic; in the view layer, it's worse — the architecture's whole point is that views are thin.

When a repository method becomes a hot read, it gets a cache. The interface stays the same: caller passes primitives, gets a DTO back. Only the implementation changes.

## Step 1: Cache helpers in `config/cache.py`

```python
from typing import Type, TypeVar

from django.core.cache import cache
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_TTL = 300  # 5 minutes — the right starting point for most reads


def cache_get(key: str, dto_type: Type[T]) -> T | None:
    """Return a DTO from the cache, or None if missing.

    The Redis cache backend pickles values; pickled DTOs round-trip cleanly.
    """
    return cache.get(key)


def cache_set(key: str, dto: BaseModel, ttl: int = DEFAULT_TTL) -> None:
    """Store a DTO under `key` with the given TTL (seconds)."""
    cache.set(key, dto, timeout=ttl)


def cache_delete(*keys: str) -> None:
    """Remove one or more keys from the cache."""
    cache.delete_many(keys)


def cache_get_or_set(key: str, fetch, dto_type: Type[T], ttl: int = DEFAULT_TTL) -> T:
    """Fetch from cache; on miss, call `fetch()`, store the result, and return it."""
    cached = cache.get(key)
    if cached is not None:
        return cached
    fresh = fetch()
    cache.set(key, fresh, timeout=ttl)
    return fresh
```

The Django `RedisCache` backend (configured in `production.py` from **django-deploy**) pickles values by default — Pydantic DTOs pickle and unpickle cleanly without needing JSON serialization. This is safe because the cache is on a private network, written only by the application.

## Step 2: Cache-aside pattern in a repository

Add caching to specific read methods, not to the whole repo. Caching adds invalidation complexity; pay it only on hot paths.

```python
from django.db import transaction

from config.cache import cache_delete, cache_get_or_set, cache_set

from .dtos import ProductDTO
from .models import Product


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

    def _fetch_by_id(self, pk: int) -> ProductDTO:
        try:
            obj = Product.objects.get(pk=pk)
        except Product.DoesNotExist as e:
            raise LookupError(f"Product {pk} not found") from e
        return ProductDTO.model_validate(obj)

    @transaction.atomic
    def update(self, pk: int, **fields) -> ProductDTO:
        Product.objects.filter(pk=pk).update(**fields)
        cache_delete(_product_key(pk))         # invalidate before returning
        return self._fetch_by_id(pk)            # re-fetch fresh; will repopulate on next read

    @transaction.atomic
    def delete(self, pk: int) -> None:
        Product.objects.filter(pk=pk).delete()
        cache_delete(_product_key(pk))

    def list_active(self) -> list[ProductDTO]:
        # NOT cached — list queries are usually not worth caching at small scale.
        # If they become hot, cache the list under a key like "product:list:active"
        # AND invalidate it from every write method. The bookkeeping multiplies fast.
        qs = Product.objects.filter(is_active=True)
        return [ProductDTO.model_validate(o) for o in qs]
```

Notes:

- **Key helper at module top.** `_product_key(pk)` is one source of truth — every write method invalidates with the same helper, no copy-paste typos.
- **Invalidate before returning.** `cache_delete()` runs before `_fetch_by_id()` so a concurrent reader doesn't beat the write to the cache.
- **`update` returns the re-fetched DTO** rather than building one from `**fields` — guarantees the response reflects what's in the DB, including any default/computed columns.
- **List methods often skip the cache.** Caching individual records is cheap to invalidate (one key per write). Caching list results is expensive — every write of any record in the list has to invalidate the cached list. Skip until profiling says otherwise.

## Step 3: Cache key naming

| Pattern | Use for | Example |
|---|---|---|
| `<resource>:<id>` | Single record by primary key | `product:42` |
| `<resource>:slug:<slug>` | Single record by alternate identifier | `product:slug:widget-pro` |
| `<resource>:<id>:<related>` | Nested data | `order:1:items` |
| `<resource>:list:<filter>` | Filtered list (rarely cached — see above) | `product:list:active` |
| `user:<id>:<resource>:<id>` | Per-user resource (security: never share keys across users) | `user:42:cart:1` |

Rules:

- **Always include the resource name.** Prevents collisions across repos.
- **Lowercase, colon-separated.** Predictable.
- **Per-user data MUST include the user ID in the key.** Sharing a cache key across users is how you leak someone's data to someone else.
- **Avoid query-parameter-derived keys.** A key like `product:list:active=true&limit=50&offset=100` has too many variants — cache hit rate is near zero. Cache the underlying query, not the paginated slice.

## Step 4: TTL strategy

Default to 5 minutes. Adjust based on tolerance for staleness:

| Tolerance | TTL | Use for |
|---|---|---|
| Up-to-the-second | Don't cache | Order status, payment state, anything that drives a UX decision |
| 1 minute | 60 | Hot reads with strict freshness |
| 5 minutes (default) | 300 | Most reads — product details, user profiles, lookup tables |
| 1 hour | 3600 | Slow-moving config, feature flags, public catalog |
| 1 day | 86400 | Rarely-changing references (countries list, currency codes) |

Add **jitter** to TTLs that are set on many keys at once to avoid thundering herd at expiration:

```python
import random

def _jitter_ttl(base: int, pct: float = 0.1) -> int:
    """Return base TTL ± pct% — prevents many keys expiring simultaneously."""
    delta = int(base * pct)
    return base + random.randint(-delta, delta)
```

Use `cache_set(key, dto, ttl=_jitter_ttl(300))` for hot read patterns.

## Step 5: Invalidation patterns

Cache invalidation is one of the two hard problems in computer science. The discipline:

**1. Explicit eviction on writes (default).** Every write method that changes a cached entity also calls `cache_delete()`:

```python
def update(self, pk: int, **fields) -> ProductDTO:
    # ... ORM update
    cache_delete(_product_key(pk))
    return self._fetch_by_id(pk)
```

**2. TTL as a safety net.** Even with explicit eviction, keep a TTL — it bounds the staleness window if an invalidation is missed (e.g. a write goes through `bulk_update` or raw SQL).

**3. Versioned keys for "invalidate everything related."** Bump a version counter, all keys derived from it are now stale:

```python
def _category_version() -> int:
    return cache.get("category:version", 0)

def _product_for_category_key(category_id: int, product_id: int) -> str:
    version = _category_version()
    return f"category:v{version}:product:{category_id}:{product_id}"

def invalidate_category(category_id: int) -> None:
    cache.set(f"category:v{_category_version() + 1}:lock", 1)  # bump
    cache.incr("category:version")
```

Use this when individual key tracking is too messy — typically for grouped data (everything in a category, everything for a user).

**4. Pub/sub invalidation across replicas** is unnecessary for the project's topology — there's one Redis cache instance, and all web hosts share it. Skip.

## What NOT to cache

- **User-specific data without per-user keys.** Cache leaking between users is a real outage waiting to happen.
- **Authentication / authorization checks.** Permission state is exactly what you DON'T want stale.
- **Money, inventory counts, anything that drives correctness.** Stale stock count → oversell. Stale balance → double-spend. The TTL window IS the bug.
- **Already-fast queries.** A query that takes 2ms gains nothing from being a 1ms cache hit, and you've added a write hop and an invalidation path.
- **Computed results that aren't data.** "What is this user's cart total?" — that's a service-level computation. If it's slow, fix the underlying query; don't cache the derivation.
- **Frequently-changing data.** If the average TTL exceeds the average write interval, cache hit rate is zero and you're just adding work.

## Common Mistakes

- **Caching list queries without invalidating from every write.** A `list_active()` cache that doesn't get invalidated when a single product is created or deactivated returns yesterday's list forever.
- **Forgetting per-user key isolation.** `cart:1` cached for user A, returned to user B. Production data leak.
- **Using the broker Redis as the cache.** They're separate instances for a reason — broker has AOF on (durability), cache has LRU eviction (no AOF). Cross-pollinating breaks both.
- **Caching writes.** Cache reads, never writes. A write goes to the DB, then invalidates the cache.
- **Caching by query result instead of by entity.** Per-record caching is robust to ad-hoc filters; per-query caching shatters into thousands of keys.
- **No TTL.** Every cached value MUST have a TTL, even if it's a day. "Cache forever" is how stale data outlives the bug that created it.
- **Pickling untrusted input.** The Redis cache pickles values — if anything other than your own app writes to this cache, switch the serializer to JSON. With self-hosted Redis on a private network, pickle is fine.
- **Building a caching abstraction before measuring.** Don't add cache to a method until profiling shows it's actually hot. Premature caching is premature complexity.

## Verify

```bash
# Confirm the cache backend is reachable
docker compose exec web uv run python -c "from django.core.cache import cache; cache.set('ping', 'pong', 10); print(cache.get('ping'))"

# Watch the cache while the app runs (separate Redis instance — port differs from broker)
docker compose exec redis-cache redis-cli MONITOR

# In production: confirm hit rate via Redis INFO
docker compose -f compose.prod.yml exec redis-cache redis-cli INFO stats | grep keyspace
```

A healthy cache shows a hit ratio above 80% for hot keys. Below 50% suggests TTLs are too short, keys are too granular, or invalidation is too aggressive.

## Checklist

- [ ] Caching lives in the repository layer — no `cache.get` / `cache.set` calls in services or views
- [ ] Cache helpers (`cache_get`, `cache_set`, `cache_delete`, `cache_get_or_set`) in `src/config/cache.py`
- [ ] Per-resource key helper at the top of each repository (`_product_key`, `_order_key`, etc.)
- [ ] Every cached method has a TTL
- [ ] Every write method that touches a cached entity calls `cache_delete()` on the relevant keys
- [ ] Per-user data uses keys with the user ID; never shared across users
- [ ] List queries are NOT cached unless profiling justifies the invalidation cost
- [ ] Money / inventory / authorization state is NOT cached
- [ ] Cache uses the dedicated `redis_cache` instance (separate from the Celery broker — see **django-deploy**)
- [ ] TTLs include jitter when many keys are set together
