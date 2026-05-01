---
name: django-models
description: Structure Django models with proper Meta classes, verbose names, and optimized indexes. Use when creating or reviewing Django models to ensure consistent ordering, correct verbose_name/verbose_name_plural, and database indexes aligned to actual query patterns. Also registers every model in the admin with a clean, fast-loading configuration.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Structure a Django Model

You are defining or restructuring a Django model in an opinionated Django project. Every convention below is mandatory. Do not deviate.

## BEFORE WRITING CODE

Read the model file being created or modified, plus:

- Any existing models in the same app — for cross-model index considerations
- The repository that queries this model — to understand real query patterns
- `src/apps/<app>/admin.py` — existing admin registrations

---

## Model Structure

Every model follows this exact ordering of members:

```python
from django.db import models


class MyEntity(models.Model):
    # 1. Meta — ALWAYS first, before any field
    class Meta:
        verbose_name = "my entity"
        verbose_name_plural = "my entities"
        indexes = [
            models.Index(fields=["-created_at"], name="idx_%(class)s_recent"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["slug"], name="uq_%(class)s_slug"),
        ]

    # 2. Identifiers — slugs, external refs (PK is handled by Django's BigAutoField)
    slug = models.SlugField(max_length=255)

    # 3. Time fields — created, updated, any dates/datetimes
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # 4. Workflow / status / state (if applicable)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")

    # 5. Everything else — domain fields
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # 6. Relations — ForeignKey, OneToOne, ManyToMany (always last among fields)
    category = models.ForeignKey("categories.Category", on_delete=models.CASCADE)

    # 7. __str__ — only if useful, and the only method allowed
    def __str__(self) -> str:
        return self.name
```

---

## Rules

### Meta First

`class Meta` is **always** the first thing inside the model body — before any field. This is non-negotiable. It puts the most important structural information (naming, indexes, ordering, constraints) at the top where it's immediately visible.

The ordering inside `Meta` itself:

1. `verbose_name` and `verbose_name_plural`
2. `indexes`
3. `constraints` (unique constraints, check constraints)
4. Anything else (`ordering`, `abstract`, etc.)

### Primary Keys

Use Django's default `BigAutoField` — set via `DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"` in settings. Do NOT define explicit primary key fields unless there is a specific domain reason (e.g. a natural key). Django's auto-increment IDs are battle-tested and work correctly across all databases.

### Always Declare `verbose_name` and `verbose_name_plural`

Every model's `Meta` must include both:

```python
class Meta:
    verbose_name = "order item"
    verbose_name_plural = "order items"
```

- Use lowercase, human-readable English
- Never rely on Django's automatic pluralization — it gets edge cases wrong
- The `verbose_name` should read naturally in admin headers and log messages

### Field Ordering

Fields are grouped by role, in this order:

1. **Identifiers** — slugs, external reference codes, SKUs (NOT the primary key — that's auto-generated)
2. **Time fields** — `created_at`, `updated_at`, `published_at`, any date or datetime
3. **Workflow / status / state** — `status`, `stage`, `is_active`, `is_published` (skip if the model has no lifecycle)
4. **Domain fields** — everything else: `name`, `description`, `price`, `quantity`, etc.
5. **Relations** — `ForeignKey`, `OneToOneField`, `ManyToManyField` — always last among fields

This ordering makes scanning a model top-to-bottom predictable: "what is it, when was it, where is it in its lifecycle, what does it contain, what does it relate to."

### Uniqueness and Constraints in Meta

All uniqueness and constraints are declared in `Meta.constraints` — never use `unique=True` on individual fields. This keeps all structural rules in one place, right at the top of the model.

```python
class Meta:
    verbose_name = "product"
    verbose_name_plural = "products"
    indexes = [
        models.Index(fields=["-created_at"], name="idx_%(class)s_recent"),
    ]
    constraints = [
        models.UniqueConstraint(fields=["sku"], name="uq_%(class)s_sku"),
        models.UniqueConstraint(fields=["store", "slug"], name="uq_%(class)s_store_slug"),
        models.CheckConstraint(check=models.Q(price__gte=0), name="ck_%(class)s_price_pos"),
    ]
```

Constraint naming convention:
- **Unique:** `uq_%(class)s_<short_description>`
- **Check:** `ck_%(class)s_<short_description>`

### Field `verbose_name` and `help_text`

Any field whose name is more than one word (joined by underscores) should have an explicit `verbose_name` so it reads cleanly in the admin:

```python
price_at_purchase = models.DecimalField(
    verbose_name="price at purchase",
    max_digits=10,
    decimal_places=2,
)
```

Any field whose purpose is not immediately obvious from its name needs `help_text`:

```python
idempotency_key = models.CharField(
    max_length=255,
    help_text="Client-generated key to prevent duplicate order submissions.",
)
```

Rules:
- Single-word fields (`name`, `price`, `status`) don't need a `verbose_name` — Django infers it fine
- Multi-word fields (`price_at_purchase`, `is_published`, `created_by`) always get an explicit `verbose_name`
- Obscure or domain-specific fields always get `help_text`
- Keep `help_text` to one sentence, written for someone reading the admin form

### Specify Indexes in Meta

All indexes are declared in `Meta.indexes` — never use `db_index=True` on individual fields.

Index naming convention: `idx_%(class)s_<short_description>`

### Optimize Indexes for How the Model Is Used

Don't index speculatively. Read the repository that queries this model and index for the queries that actually exist:

- **Filter + order** → composite index with filter columns first, order column last: `fields=["status", "-created_at"]`
- **Foreign key lookups** → Django auto-creates indexes on `ForeignKey` fields, but if you always filter the FK *with* another column, replace it with a composite: `fields=["order", "product"]`
- **Prefix for descending sort** → use `-` prefix: `fields=["-created_at"]`
- **Covering queries** → if a query only reads a small set of columns, consider `include` (Postgres)
- **Partial indexes** → if a query always filters on a condition, use `condition`
- **Don't duplicate** — Django auto-creates an index for every `ForeignKey` and `UniqueConstraint`
- **Don't over-index** — every index slows writes. Three or four well-chosen indexes beat eight speculative ones.

### No Business Logic

Models contain ZERO business logic:

- No custom managers
- No `save()` overrides
- No signals
- No properties that compute
- `__str__` is the only method allowed

---

## Admin Registration

Every model gets registered in `src/apps/<app>/admin.py` with a clean, fast-loading configuration.

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
    list_display = ("id", "date", "total")
    list_per_page = 25
    search_fields = ("id",)
    readonly_fields = ("id",)
    ordering = ("-date",)
    fieldsets = (
        (None, {"fields": ("id", "date")}),
        ("Details", {"fields": ("total",)}),
    )
    inlines = [OrderItemInline]
```

### Admin Rules

- **`list_display`** — `id` first, then the most useful columns. 4-6 fields max.
- **`list_per_page = 25`** — keeps the admin snappy on large tables.
- **`search_fields`** — always include `id`. Never search on unindexed columns.
- **`readonly_fields`** — always include `id`. Add computed or auto-set fields.
- **`ordering`** — explicit, usually `-created_at` or the most natural time field.
- **`fieldsets`** — place `id` and timestamps in the first (untitled) fieldset at the top.
- **`list_select_related`** — specify FKs shown in `list_display` to avoid N+1 queries.
- **`raw_id_fields`** — use for any FK to a large table.
- **`extra = 0`** on inlines — never show empty inline forms by default.
- **`show_change_link = True`** on inlines.
- **`autocomplete_fields`** — prefer over `raw_id_fields` when the related model has `search_fields`.
- **`date_hierarchy`** — use on the primary date field for time-series models. Only on indexed fields.

---

## Full Example

```python
from django.db import models


class Order(models.Model):
    class Meta:
        verbose_name = "order"
        verbose_name_plural = "orders"
        indexes = [
            models.Index(fields=["-date"], name="idx_%(class)s_recent"),
            models.Index(fields=["status", "-date"], name="idx_%(class)s_status_recent"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                name="uq_%(class)s_idempotency",
            ),
        ]

    # Identifiers
    idempotency_key = models.CharField(
        verbose_name="idempotency key",
        max_length=255,
        help_text="Client-generated key to prevent duplicate order submissions.",
    )

    # Time
    date = models.DateTimeField(auto_now_add=True)

    # Status
    status = models.CharField(max_length=20, default="pending")

    # Domain
    total = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self) -> str:
        return f"Order {self.id} on {self.date}"


class OrderItem(models.Model):
    class Meta:
        verbose_name = "order item"
        verbose_name_plural = "order items"
        indexes = [
            models.Index(fields=["order", "product"], name="idx_%(class)s_ord_prd"),
        ]

    # Domain
    quantity = models.PositiveIntegerField()
    price_at_purchase = models.DecimalField(
        verbose_name="price at purchase",
        max_digits=10,
        decimal_places=2,
        help_text="Snapshot of the product price at the time the order was placed.",
    )

    # Relations
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("products.Product", on_delete=models.CASCADE)

    def __str__(self) -> str:
        return f"{self.quantity}x (Order {self.order_id})"
```

---

## Verify

After creating or modifying models:

```bash
make makemigrations && make migrate
make check    # lint + format-check + typecheck
make test
```

## Checklist

- [ ] `class Meta` is the first thing inside the model body
- [ ] `verbose_name` and `verbose_name_plural` are set
- [ ] Primary key uses Django's default `BigAutoField` — no explicit PK field unless required by domain
- [ ] Field order: identifiers → time → status/state → domain → relations
- [ ] All indexes in `Meta.indexes` — no `db_index=True` on fields
- [ ] All uniqueness in `Meta.constraints` via `UniqueConstraint` — no `unique=True` on fields
- [ ] Indexes match actual query patterns from the repository layer
- [ ] Multi-word fields have explicit `verbose_name`
- [ ] Obscure or domain-specific fields have `help_text`
- [ ] No business logic — no custom managers, `save()`, signals, or computed properties
- [ ] Model registered in admin with `list_display`, `list_per_page = 25`, `search_fields`, `readonly_fields`, `ordering`, `fieldsets`
- [ ] FKs to large tables use `raw_id_fields` or `autocomplete_fields`
- [ ] Inlines use `extra = 0` and `show_change_link = True`
- [ ] Migrations generated and applied
- [ ] ruff, pyrefly, pytest all pass
