---
name: django-api
description: Design and write the DRF API layer — Serializers for input validation, bare-bones ViewSets that dispatch to services, URL routing with DRF and drf-nested-routers, two-tier authentication and permissions (DRF classes for request-level, service exceptions for data-level), error mapping through the central exception handler, OpenAPI schemas via drf-spectacular, URL-path API versioning, and file upload patterns including S3 signed URLs. Use when adding new endpoints, designing a new resource's API surface, refactoring views that contain business logic or import models, configuring DRF authentication or permissions, or whenever the user mentions ViewSets, serializers, DRF, REST API, OpenAPI, or endpoints.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# DRF API Layer

The API layer is where every external request crosses into the project. Its job is narrow: validate input, dispatch to a service, return the service's DTO as JSON. Nothing else.

The architectural rule that drives every pattern in this skill: **Serializers are for input only. Output is `dto.model_dump()`.** This is the line that keeps DRF from leaking into the rest of the architecture — `ModelSerializer` and friends pull a queryset directly into the response, bypassing the repository, the service, and the entire DTO discipline. That's the wrong direction.

## What each DRF layer is for in this project

| DRF concept | Role here |
|---|---|
| **Serializer** | Validate incoming request data. Produces `validated_data` that the view passes to a service. NEVER used to shape responses. |
| **ViewSet** | Thin dispatcher. Each action method validates input via a serializer, calls `get(SomeService).method(**serializer.validated_data)`, returns the resulting DTO via `model_dump()`. |
| **Router** | URL composition. `DefaultRouter` per app, `NestedDefaultRouter` for nested resources. |
| **Authentication** | DRF auth classes resolve `request.user`. The choice of class (Token / JWT / Session) is a project decision, not a skill prescription. |
| **Permissions** | Two tiers: DRF `permission_classes` for request-level checks (auth, role); services raise `PermissionError` for data-level checks ("does this user own this row?"). |
| **Renderer / Parser** | Defaults to JSON in, JSON out. Override only for file uploads (`MultiPartParser`). |

## Step 1: Serializers — input validation only

File: `src/apps/<app>/serializers.py`

Plain `serializers.Serializer`. Never `ModelSerializer`. Never `serializers.ModelSerializer` "just for read access" — that's how the architecture rots.

```python
from decimal import Decimal

from rest_framework import serializers


class CreateProductSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))
    stock = serializers.IntegerField(min_value=0)

    def validate_name(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("Name cannot be blank.")
        return normalized


class UpdateProductSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False)
    stock = serializers.IntegerField(min_value=0, required=False)
```

Rules:

- **Distinct Create and Update serializers.** Don't conditionally swap fields between create and update via `required=False` everywhere — every check at the boundary becomes "does this field exist or not?" and the schema turns into noise. Two small serializers beat one big one.
- **`validate_<field>(self, value)`** for field-level validation. Return the cleaned value.
- **`validate(self, attrs)`** for cross-field validation. Return `attrs`.
- **`raise serializers.ValidationError(...)`** for failures. DRF maps it to HTTP 400 with field-keyed errors automatically.
- **Don't query the database** in a Serializer. If validation depends on DB state ("is this email already taken?"), do the check in the service. The serializer's job is shape and field-level rules; the service owns invariants.

### Nested input

```python
class OrderItemInputSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class CreateOrderSerializer(serializers.Serializer):
    items = OrderItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Order must have at least one item.")
        return value
```

`validated_data["items"]` is a list of dicts — flat data the service can consume.

### When a serializer feels like the wrong tool

If you find yourself reaching for `Serializer.save()`, `ModelSerializer`, or anything that touches the ORM from the serializer, stop. The serializer is doing too much. Move the work to a service method.

## Step 2: ViewSets — thin dispatchers

File: `src/apps/<app>/views.py`

Use **`viewsets.ViewSet`** — the bare base class. NOT `ModelViewSet`, NOT `GenericViewSet` with `queryset = ...`. Both of those couple the view to the ORM directly and bypass the repository / service stack.

```python
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config.services import get
from config.types import AuthedRequest

from .serializers import CreateProductSerializer, UpdateProductSerializer
from .services import ProductService


class ProductViewSet(viewsets.ViewSet):
    def list(self, request):
        dtos = get(ProductService).list_products()
        return Response([d.model_dump() for d in dtos])

    def retrieve(self, request, pk=None):
        dto = get(ProductService).get_product(int(pk))
        return Response(dto.model_dump())

    def create(self, request):
        serializer = CreateProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dto = get(ProductService).create_product(**serializer.validated_data)
        return Response(dto.model_dump(), status=status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        serializer = UpdateProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dto = get(ProductService).update_product(int(pk), **serializer.validated_data)
        return Response(dto.model_dump())

    def partial_update(self, request, pk=None):
        return self.update(request, pk)

    def destroy(self, request, pk=None):
        get(ProductService).delete_product(int(pk))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def archive(self, request: AuthedRequest, pk=None):
        dto = get(ProductService).archive_product(int(pk), user_id=request.user.id)
        return Response(dto.model_dump())
```

Rules:

- **Action methods only.** Each one: validate, dispatch to a service, return a DTO via `model_dump()`. If a method is more than ~6 lines, the logic belongs in the service.
- **No try/except.** Service exceptions propagate to `config/exception_handler.py` which maps them to HTTP. The view is HTTP-aware only insofar as it sets status codes.
- **Status codes:**
  - `200` for `list` / `retrieve` / `update` / `partial_update`
  - `201` for `create`
  - `204` for `destroy` (no body)
- **`pk` is a string** (URL path component). Coerce to `int` at the boundary before calling the service.
- **`@action(detail=True/False, methods=[...])`** for endpoints that don't fit CRUD. URL becomes `/products/{pk}/archive/` (`detail=True`) or `/products/archive/` (`detail=False`).
- **Pass `user_id=request.user.id`** explicitly when the service needs it. The service does NOT receive `request` — it stays HTTP-unaware. Use the `AuthedRequest` type alias from `config.types` so `request.user` is narrowed past `AnonymousUser`.

### `partial_update` shorthand

If `update` and `partial_update` go through the same Update serializer (which has `required=False` on every field), the simple `return self.update(...)` delegation is fine. If you need stricter `update` semantics (require all fields), give them separate serializers.

## Step 3: URL routing

File: `src/apps/<app>/urls.py`

```python
from rest_framework.routers import DefaultRouter

from .views import ProductViewSet

router = DefaultRouter()
router.register(r"products", ProductViewSet, basename="product")

urlpatterns = router.urls
```

Then include in `src/config/urls.py` under the version prefix (Step 7):

```python
urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.products.urls")),
    # ...
]
```

### Nested resources — `drf-nested-routers`

Use nested routes when a resource only makes sense in the context of its parent (`/orders/{id}/items/{id}/`). Don't use them when both sides are independently queryable — that's two flat resources.

```python
from rest_framework_nested import routers

from .views import OrderItemViewSet, OrderViewSet

router = routers.DefaultRouter()
router.register(r"orders", OrderViewSet, basename="order")

orders_router = routers.NestedDefaultRouter(router, r"orders", lookup="order")
orders_router.register(r"items", OrderItemViewSet, basename="order-items")

urlpatterns = router.urls + orders_router.urls
```

In `OrderItemViewSet`, the parent's `pk` arrives as `kwargs["order_pk"]`. Pass it to the service as `order_id=int(kwargs["order_pk"])`.

## Step 4: Authentication

Configure DRF's authentication classes once in `src/config/settings/base.py`:

```python
REST_FRAMEWORK = {
    # ...existing entries (EXCEPTION_HANDLER, DEFAULT_SCHEMA_CLASS)...
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}
```

The choice between Token / JWT (`djangorestframework-simplejwt`) / Session / custom is a project decision — pick one and commit. Whichever you pick, the rule is the same: the auth class resolves `request.user`, and the view passes `user_id=request.user.id` to the service.

The service NEVER receives `request`, NEVER imports `request.user`. It takes `user_id: int` and queries the user via the repository if it needs to.

## Step 5: Permissions — two tiers

DRF permissions answer **request-level** questions. Services answer **data-level** questions. Don't blur these.

### Tier 1: Request-level (DRF `permission_classes`)

What this tier checks: "is the request authenticated?", "is this user an admin?", "is this user a member of group X?". These don't require knowledge of which specific row is being touched.

```python
from rest_framework.permissions import IsAdminUser, IsAuthenticated


class ProductViewSet(viewsets.ViewSet):
    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy", "archive"):
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]
```

Per-action via `get_permissions()`. The class-level `permission_classes = [...]` works for the simple case where every action shares the same rule.

### Tier 2: Data-level (service raises `PermissionError`)

What this tier checks: "does this user own this specific resource?", "is this row in a state where this action is allowed?". These require querying the data being acted on.

```python
# in apps/products/services.py
def archive_product(self, product_id: int, *, user_id: int) -> ProductDTO:
    product = self.repo.get_by_id(product_id)
    if product.owner_id != user_id:
        raise PermissionError(f"User {user_id} cannot archive product {product_id}")
    return self.repo.set_archived(product_id)
```

The view passes `user_id` from `request.user.id`; the service raises `PermissionError`; the central exception handler maps it to HTTP 403.

### Why split

Putting "does this user own this row?" in a DRF `BasePermission.has_object_permission` requires the permission class to either fetch the object (which means ORM access in the view layer — banned) or duplicate the lookup. Cleaner to push it to the service where the data is already in hand.

DRF permissions stay at the request level; data-aware authorization rides on top of the service's normal exception path.

## Step 6: Error handling

DRF's default exception handler covers serializer validation. The project's `config/exception_handler.py` catches the standard Python exceptions services raise and maps them to HTTP. Together they form a single coherent response layer.

| Source | Exception | Status | Body shape |
|---|---|---|---|
| Serializer | `ValidationError` (DRF) | 400 | `{"field": ["error", ...], ...}` |
| Service | `ValueError` (Python) | 400 | `{"detail": "..."}` |
| Service | `LookupError` (Python) | 404 | `{"detail": "..."}` |
| Service | `PermissionError` (Python) | 403 | `{"detail": "..."}` |
| DRF | `NotAuthenticated` | 401 | `{"detail": "..."}` |
| DRF | `Http404` (e.g. invalid `pk`) | 404 | `{"detail": "..."}` |

Why services use Python's standard exceptions, not DRF's: the service is HTTP-unaware. It signals "bad input", "missing record", "forbidden" using language-standard semantics; the handler at the edge translates them to HTTP. This keeps services testable without an HTTP context and keeps the HTTP-mapping logic in exactly one file.

## Step 7: OpenAPI schemas via drf-spectacular

`drf-spectacular` introspects request schemas from Serializers automatically. **Response schemas are not auto-discovered** because views return `dto.model_dump()` (a plain dict), not a Serializer.

The recommended bridge is `drf-pydantic`, which exposes a `.drf_serializer` attribute on Pydantic models that drf-spectacular treats as a Serializer for schema purposes.

Add the dependency:

```bash
docker compose exec web uv add 'drf-pydantic>=2.0'
```

Update DTO base class to inherit `drf_pydantic.BaseModel` (drop-in replacement for `pydantic.BaseModel`):

```python
# src/apps/products/dtos.py
from decimal import Decimal

from drf_pydantic import BaseModel
from pydantic import ConfigDict


class ProductDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    price: Decimal
    stock: int
```

Annotate views with `@extend_schema`:

```python
from drf_spectacular.utils import OpenApiExample, extend_schema

from .dtos import ProductDTO


class ProductViewSet(viewsets.ViewSet):
    @extend_schema(
        responses={200: ProductDTO.drf_serializer(many=True)},
        tags=["Products"],
    )
    def list(self, request):
        ...

    @extend_schema(
        request=CreateProductSerializer,
        responses={201: ProductDTO.drf_serializer},
        tags=["Products"],
        examples=[
            OpenApiExample(
                "Create a widget",
                value={"name": "Widget", "price": "9.99", "stock": 5},
                request_only=True,
            ),
        ],
    )
    def create(self, request):
        ...
```

For `@action` endpoints, the same `@extend_schema` decorator goes on the action method itself.

If you'd rather not add `drf-pydantic`, the alternative is maintaining a parallel output Serializer per DTO (`ProductOutputSerializer`) and using it in `responses=`. That's two definitions of the same shape, with drift risk; not recommended.

### Schema customization basics

- **`tags=["..."]`** — group endpoints in the docs UI. Use one tag per resource (`"Products"`, `"Orders"`).
- **`description="..."`** — long-form description. Picked up from the docstring if not supplied.
- **`examples=[OpenApiExample(...)]`** — request and response examples. `request_only=True` / `response_only=True` if asymmetric.
- **`parameters=[OpenApiParameter(...)]`** — for query string parameters not bound to a serializer field.

`drf-spectacular`'s defaults are sensible; reach for `@extend_schema` to fix specific issues, not preemptively.

## Step 8: API versioning — URL path

Recommendation: URL-path versioning. Visible in logs, easy to test, curl-friendly. Header-based versioning is academically cleaner but operationally annoying.

```python
# src/config/urls.py
urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/v1/", include("apps.products.urls")),
    path("api/v1/", include("apps.orders.urls")),
]
```

Don't introduce `/v2/` until you have a concrete breaking change and consumers who can't immediately migrate. Premature versioning multiplies maintenance with no benefit.

When `/v2/` becomes necessary:

1. New apps live at the new prefix immediately.
2. For existing endpoints, copy the v1 ViewSet to `views_v2.py` (or per-app `apps/<app>/v2/views.py`) and edit the v2 copy. Don't try to share code — versioning that needs `if version == 2:` everywhere is the worst of both worlds.
3. `/v1/` keeps working until consumers migrate.
4. Deprecate publicly with a sunset date.

## Step 9: File uploads

Two patterns. Pick by file size.

### Small files (< ~10 MiB) — direct multipart upload

```python
from rest_framework import serializers, viewsets
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from config.services import get
from config.types import AuthedRequest

from .services import DocumentService


class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    ALLOWED_TYPES = {"image/jpeg", "image/png", "application/pdf"}
    MAX_SIZE = 10 * 1024 * 1024  # 10 MiB

    def validate_file(self, value):
        if value.size > self.MAX_SIZE:
            raise serializers.ValidationError("File exceeds 10 MiB.")
        if value.content_type not in self.ALLOWED_TYPES:
            raise serializers.ValidationError(f"Content type {value.content_type} not allowed.")
        return value


class DocumentViewSet(viewsets.ViewSet):
    parser_classes = [MultiPartParser]

    def create(self, request: AuthedRequest):
        serializer = UploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        upload = serializer.validated_data["file"]

        dto = get(DocumentService).upload(
            user_id=request.user.id,
            filename=upload.name,
            content_type=upload.content_type,
            stream=upload,
        )
        return Response(dto.model_dump(), status=201)
```

The service writes the file via `default_storage.save(...)` (which goes to S3 via `STORAGES` config from `django-deploy`). Validation lives on the serializer; storage details live on the service.

### Large files — S3 signed URLs

For files larger than ~10 MiB, the API should hand the client a signed URL and let it upload directly to S3. The API server never streams the bytes.

```python
class SignedUrlRequestSerializer(serializers.Serializer):
    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=128)


class DocumentViewSet(viewsets.ViewSet):
    @action(detail=False, methods=["post"], url_path="signed-url")
    def signed_url(self, request: AuthedRequest):
        serializer = SignedUrlRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        url_dto = get(UploadService).create_signed_upload(
            user_id=request.user.id,
            **serializer.validated_data,
        )
        return Response(url_dto.model_dump())

    def create(self, request: AuthedRequest):
        # Client confirms the S3 upload completed — record it
        serializer = ConfirmUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dto = get(UploadService).record_upload(
            user_id=request.user.id,
            **serializer.validated_data,
        )
        return Response(dto.model_dump(), status=201)
```

The service uses `boto3.client("s3").generate_presigned_post(...)` to produce the URL + form fields. The DTO returned is `{ "url": "...", "fields": {...}, "key": "..." }` which the client posts to directly. After success, the client calls `POST /documents/` with the S3 key to record metadata.

## Common Mistakes

- **`ModelViewSet` or `GenericViewSet` with a `queryset`**. Couples the view to the ORM directly, bypasses the repository, defeats the architecture. Use `viewsets.ViewSet` and dispatch to services.
- **`ModelSerializer`** for either input or output. Input: it leaks model fields straight to the wire. Output: not the right tool — output goes through `dto.model_dump()`.
- **Returning a Serializer instance from a view**. Output goes through Pydantic DTOs. If you find yourself doing `return Response(MySerializer(instance).data)`, you're shaping output via DRF. Use a service that returns a DTO and `model_dump()` it.
- **Querying the database from a Serializer's `validate`**. Move the check to the service. Serializers do shape and field-level rules.
- **Conditional fields in one Serializer for both create and update.** Two small serializers beat one big one with `required=False` everywhere.
- **`request.user` reaching the service.** Pass `user_id=request.user.id`. Services stay HTTP-unaware.
- **Catching exceptions in views.** They propagate to `config/exception_handler.py`. Catching in the view duplicates the mapping in two places.
- **`detail=True` action that doesn't take a `pk`.** The router won't generate the URL correctly. `detail=True` always implies a per-resource endpoint.
- **Creating `/api/v2/` without a concrete reason.** Versioning is a tax. Don't pay it before you have to.
- **Streaming large file uploads through the API.** Use S3 signed URLs. The API gets one POST per upload (the metadata record), not the bytes.
- **Skipping `parser_classes = [MultiPartParser]`** on a file-upload viewset. The default JSON parser will reject `multipart/form-data` requests.

## Verify

```bash
make check    # lint + format-check + typecheck
make test     # the API tests in test_api.py exercise these patterns end-to-end
docker compose exec web uv run python manage.py spectacular --validate    # OpenAPI schema is valid
```

The OpenAPI validation catches schema issues before they reach API consumers — broken response declarations, missing examples, malformed parameters.

## Checklist

- [ ] Serializers in `src/apps/<app>/serializers.py` use `serializers.Serializer`, NEVER `ModelSerializer`
- [ ] Distinct Create and Update serializers (no big-bag-of-optional-fields pattern)
- [ ] Field-level validation in `validate_<field>`, cross-field in `validate`
- [ ] No DB queries inside serializer `validate` methods
- [ ] ViewSet inherits from `viewsets.ViewSet` (bare), NOT `ModelViewSet` / `GenericViewSet`
- [ ] No try/except in views — exceptions propagate to `config/exception_handler.py`
- [ ] No model imports in `views.py`
- [ ] Each action method validates input, dispatches via `get(SomeService)`, returns `dto.model_dump()`
- [ ] `pk` coerced to `int` before passing to the service
- [ ] `user_id=request.user.id` passed explicitly when the service needs the caller's identity
- [ ] Status codes: 201 create, 200 read/update, 204 destroy
- [ ] Authentication classes configured globally in `REST_FRAMEWORK`; project-specific class is documented somewhere
- [ ] Request-level permissions in DRF `permission_classes` / `get_permissions()`; data-level checks raise `PermissionError` from the service
- [ ] DTO inherits from `drf_pydantic.BaseModel` so `@extend_schema(responses={...: DTO.drf_serializer})` works
- [ ] `@extend_schema` declarations on every action that returns data, with `tags`, `responses`, and at least one `OpenApiExample` per shape
- [ ] URLs include `api/v1/` prefix; new apps wired in `config/urls.py`
- [ ] Nested resources use `drf-nested-routers`; flat resources don't
- [ ] File uploads: `parser_classes = [MultiPartParser]`, validation on the serializer (size + type), service handles storage
- [ ] Large file uploads use S3 signed URLs (the API handles the metadata, not the bytes)
- [ ] `python manage.py spectacular --validate` passes
