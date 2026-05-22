---
name: django-templates
description: Server-rendered HTML layer using Django templates + htmx + Alpine.js. Peer to django-api at the same architectural slot. Function-based views call services, render templates (or partials for htmx requests). Django Forms for input validation (never ModelForm). Middleware maps service exceptions to HTML responses. Both layers can coexist — DRF for JSON, templates for HTML. Use when building admin UIs, internal tools, or any server-rendered Django app.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Templates + htmx + Alpine.js

Peer to **django-api**. Same architectural slot (the presentation layer), different rendering strategy. Services don't care which one calls them — they return DTOs, the rest is presentation. Both layers can coexist in the same project: DRF for `/api/v1/` (mobile, third-party), templates for the web UI.

## What this layer is

- **Function-based views** — short, greppable, no inheritance soup.
- **Django Forms** — input validation. Never `ModelForm`.
- **htmx** — server-driven interactivity. Partial templates returned from views, swapped into the DOM.
- **Alpine.js** — minimal client-side state. Dropdowns, modals, tab toggles. NEVER for data fetching (that's htmx).
- **HTML exception middleware** — maps `ValueError`/`LookupError`/`PermissionError` from services to error templates. Peer to the DRF exception handler.

## What stays unchanged

The services, repos, models, signals, DTOs, settings, cache, deploy, CI, tests — all the deeper layers — work identically. Read **django-services**, **django-repositories**, **django-models** as-is. This skill only redefines what's above the service layer.

## Step 1: Dependencies

```bash
docker compose exec web uv add django-htmx
```

`django-htmx` provides middleware that adds `request.htmx` (True/False) and helpers for `HX-*` response headers.

Add to `src/config/settings/base.py`:

```python
INSTALLED_APPS = [
    # ...keep existing...
    "django_htmx",
]

MIDDLEWARE = [
    # ...keep existing django middleware (auth, csrf, etc.)...
    "django_htmx.middleware.HtmxMiddleware",
    "config.middleware.ServiceExceptionMiddleware",   # see Step 7
]
```

`HtmxMiddleware` must come after `AuthenticationMiddleware`. `ServiceExceptionMiddleware` comes last so it catches everything below it.

## Step 2: View pattern — function-based

File: `src/apps/<app>/views.py`

```python
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect

from config.services import get
from config.types import AuthedRequest

from .forms import CreateProductForm, UpdateProductForm
from .services import ProductService


@login_required
def product_list(request: AuthedRequest):
    products = get(ProductService).list_items()
    return render(request, "products/list.html", {"products": products})


@login_required
def product_detail(request: AuthedRequest, pk: int):
    product = get(ProductService).get_item(pk)
    return render(request, "products/detail.html", {"product": product})


@login_required
def product_create(request: AuthedRequest):
    if request.method == "POST":
        form = CreateProductForm(request.POST)
        if form.is_valid():
            product = get(ProductService).create_item(
                user_id=request.user.id,
                **form.cleaned_data,
            )
            if request.htmx:
                return render(request, "products/_card.html", {"product": product})
            return redirect("products:detail", pk=product.id)
    else:
        form = CreateProductForm()
    return render(request, "products/create.html", {"form": form})


@login_required
def product_update(request: AuthedRequest, pk: int):
    product = get(ProductService).get_item(pk)
    if request.method == "POST":
        form = UpdateProductForm(request.POST)
        if form.is_valid():
            updated = get(ProductService).update_item(
                pk,
                user_id=request.user.id,
                **form.cleaned_data,
            )
            if request.htmx:
                return render(request, "products/_card.html", {"product": updated})
            return redirect("products:detail", pk=pk)
    else:
        form = UpdateProductForm(initial=product.model_dump())
    return render(request, "products/edit.html", {"form": form, "product": product})


@login_required
def product_delete(request: AuthedRequest, pk: int):
    if request.method != "POST":
        return redirect("products:detail", pk=pk)
    get(ProductService).delete_item(pk)
    if request.htmx:
        return HttpResponse(status=200)    # empty 200 — caller's hx-swap removes the row
    return redirect("products:list")
```

### Rules

- **Function-based.** Class-based hides too much for thin dispatchers. Each view is one function, one purpose, one render.
- **`@login_required` on everything** unless explicitly public. Don't rely on global defaults — make it explicit per view.
- **No ORM imports.** No `Model.objects.filter(...)` in views. Always `get(SomeService).method()`.
- **`request.htmx`** decides partial vs full template. The pattern: htmx → partial swap; non-htmx → full page + redirect.
- **`user_id=request.user.id` passed explicitly** to services. Services stay HTTP-unaware.
- **Coerce path params at the boundary** — Django's URL converters (`<int:pk>`) handle this for you. Don't pass strings into services.
- **Return `redirect()` on POST success for non-htmx.** Otherwise a browser refresh re-submits.
- **No try/except in views.** Service exceptions propagate to `ServiceExceptionMiddleware` (Step 7).

## Step 3: Forms — Django Forms, never `ModelForm`

File: `src/apps/<app>/forms.py`

```python
from decimal import Decimal

from django import forms


class CreateProductForm(forms.Form):
    name = forms.CharField(max_length=255)
    price = forms.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))
    stock = forms.IntegerField(min_value=0)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Name cannot be blank.")
        return name


class UpdateProductForm(forms.Form):
    name = forms.CharField(max_length=255, required=False)
    price = forms.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False)
    stock = forms.IntegerField(min_value=0, required=False)
```

### Rules

- **`forms.Form`, never `ModelForm`.** Same ban as `ModelSerializer` in django-api — `ModelForm` couples the form to model fields, the view starts touching the model, the architecture rots.
- **Distinct Create and Update forms.** Don't conditional-mode one big form with `required=False` everywhere.
- **`clean_<field>`** for field-level. **`clean()`** for cross-field.
- **No DB queries in `clean*` methods.** Uniqueness checks, ownership checks → service. The form does shape and field-level rules only.
- **Hand `form.cleaned_data` to the service** as `**kwargs`. The form is the input boundary; the service owns invariants.

## Step 4: Template organization

```
src/templates/
  base.html                          # root template
  components/
    _form_field.html                 # reused form field rendering
    _pagination.html
    _flash_messages.html
  errors/
    400.html  403.html  404.html  500.html
    _inline.html                     # htmx error fragment
  products/
    list.html  detail.html
    create.html  edit.html
    _card.html                       # partial: one product card
    _row.html                        # partial: one product table row
    _form.html                       # partial: just the form (htmx form swap)
```

### Rules

- **Centralized `src/templates/`** — set `TEMPLATES['DIRS'] = [BASE_DIR / "templates"]` in settings. App-local templates work too but split state is harder to grep.
- **Partials prefixed with `_`.** Convention — not enforced by Django, enforced by this skill. Easy to grep: `find . -name "_*.html"` shows every fragment.
- **Always inherit from `base.html`** for full pages. Partials are bare HTML — no `{% extends %}`.
- **`{% block %}` regions:** `title`, `content`, `sidebar`, `scripts`, `extra_head`. Define them in `base.html`; override only what changes.

### Minimal `base.html`

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}App{% endblock %}</title>
  {% block extra_head %}{% endblock %}
  <script src="https://unpkg.com/htmx.org@2.0.0" defer></script>
  <script defer src="https://unpkg.com/alpinejs@3.x.x" defer></script>
</head>
<body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>
  {% include "components/_flash_messages.html" %}

  <main>
    {% block content %}{% endblock %}
  </main>

  {% block scripts %}{% endblock %}
</body>
</html>
```

The `hx-headers` on `<body>` sends the CSRF token with every htmx request. No per-form CSRF dance needed.

## Step 5: htmx conventions

```html
<!-- products/list.html -->
{% extends "base.html" %}
{% block content %}
<h1>Products</h1>

<button hx-get="{% url 'products:create' %}"
        hx-target="#modal"
        hx-swap="innerHTML">
  + New product
</button>

<div id="product-list">
  {% for product in products %}
    {% include "products/_card.html" %}
  {% endfor %}
</div>

<div id="modal"></div>
{% endblock %}
```

```html
<!-- products/_card.html (partial) -->
<div class="product-card" id="product-{{ product.id }}">
  <h3>{{ product.name }}</h3>
  <p>${{ product.price }}</p>

  <button hx-get="{% url 'products:update' product.id %}"
          hx-target="#product-{{ product.id }}"
          hx-swap="outerHTML">
    Edit
  </button>

  <form method="post"
        action="{% url 'products:delete' product.id %}"
        hx-post="{% url 'products:delete' product.id %}"
        hx-target="#product-{{ product.id }}"
        hx-swap="outerHTML swap:300ms"
        hx-confirm="Delete this product?">
    {% csrf_token %}
    <button type="submit">Delete</button>
  </form>
</div>
```

### Patterns

- **HTTP verb → htmx attr.** `hx-get`, `hx-post`, `hx-put`, `hx-delete`. Match the action.
- **`hx-target`** — CSS selector for the swap target. `closest` and `next` selectors work too.
- **`hx-swap`** — `innerHTML` (default), `outerHTML`, `beforeend`, `afterbegin`, `delete`. `swap:300ms` for transition delay.
- **`hx-confirm`** for destructive actions (browser native confirm — for fancier, use Alpine + modal).
- **Return a partial template from the view** for htmx requests. The partial replaces (or augments) the target.
- **`HX-Trigger` response header** — fire a client-side event from the server:

  ```python
  response = render(request, "products/_card.html", {"product": product})
  response["HX-Trigger"] = "productCreated"
  return response
  ```

  Then listen client-side: `<div hx-trigger="productCreated from:body">...</div>` or via Alpine.

- **`HX-Redirect`** — full-page redirect after an htmx request:

  ```python
  response = HttpResponse(status=200)
  response["HX-Redirect"] = reverse("products:list")
  return response
  ```

- **`HX-Refresh: true`** — tell the browser to do a full reload.

### Out-of-band swaps (OOB)

Update parts of the page that aren't the primary target:

```html
<!-- view returns a fragment containing the main response + OOB updates -->
{% include "products/_card.html" %}

<div id="counter" hx-swap-oob="true">
  Total products: {{ count }}
</div>
```

The `_card.html` swaps into the requested target; the `counter` div replaces `#counter` on the page. Use sparingly — OOB is harder to reason about.

## Step 6: Alpine.js — minimal use only

```html
<!-- Dropdown -->
<div x-data="{ open: false }">
  <button @click="open = !open">Menu</button>
  <ul x-show="open" @click.outside="open = false">
    <li><a href="...">Item</a></li>
  </ul>
</div>

<!-- Tab toggle -->
<div x-data="{ tab: 'overview' }">
  <button :class="{'active': tab === 'overview'}" @click="tab = 'overview'">Overview</button>
  <button :class="{'active': tab === 'details'}" @click="tab = 'details'">Details</button>

  <div x-show="tab === 'overview'">...</div>
  <div x-show="tab === 'details'">...</div>
</div>

<!-- Disable submit while a form is in flight -->
<form x-data="{ submitting: false }" @submit="submitting = true">
  <button :disabled="submitting" type="submit">Save</button>
</form>
```

### Rules

- **Use Alpine ONLY for UI state that doesn't need the server.** Dropdowns, modals, tabs, transient form state, disabled buttons.
- **NEVER use Alpine for data fetching.** `fetch()` or `axios` from Alpine = wrong tool. Use htmx.
- **Inline `x-data` is the default.** Extract to a named component (`Alpine.data('foo', () => ({...}))`) only if reused 3+ times.
- **No business logic.** If Alpine state needs to validate against the database, it's the wrong layer.
- **One framework at a time.** Don't add Stimulus, htmx-ext, Hotwire, etc. — htmx + Alpine covers the same ground simpler. Pick. Commit.

## Step 7: HTML exception middleware

`src/config/middleware.py`:

```python
from django.contrib import messages
from django.shortcuts import render


class ServiceExceptionMiddleware:
    """Maps service-layer exceptions to HTML responses.

    Counterpart to config.exception_handler.custom_exception_handler (DRF/JSON).
    Both can coexist — DRF's handler covers /api/* requests; this middleware
    covers everything else.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, ValueError):
            if getattr(request, "htmx", False):
                return render(
                    request,
                    "errors/_inline.html",
                    {"error": str(exception)},
                    status=400,
                )
            messages.error(request, str(exception))
            return render(request, "errors/400.html", {"error": str(exception)}, status=400)

        if isinstance(exception, LookupError):
            return render(request, "errors/404.html", {"error": str(exception)}, status=404)

        if isinstance(exception, PermissionError):
            return render(request, "errors/403.html", {"error": str(exception)}, status=403)

        return None    # let Django handle other exceptions
```

Register in `MIDDLEWARE` (Step 1). Lives after `AuthenticationMiddleware` and `HtmxMiddleware` (uses both).

### Coexistence with DRF

Both handlers can live in the same project without conflict:

- DRF's `custom_exception_handler` runs only for DRF views (configured via `REST_FRAMEWORK["EXCEPTION_HANDLER"]`).
- `ServiceExceptionMiddleware` runs for every non-DRF request.

For a request to `/api/v1/products/`, DRF catches the exception. For a request to `/products/`, the middleware catches it. Services don't change.

## Step 8: Authorization — two tiers

Same model as **django-api**. Just different sugar.

### Tier 1 — request-level decorators

```python
from django.contrib.auth.decorators import login_required, permission_required


@login_required
@permission_required("products.change_product", raise_exception=True)
def product_update(request, pk):
    ...
```

`raise_exception=True` makes the decorator raise `PermissionDenied` (= 403) instead of redirecting to login.

For role-based logic that decorators can't express, write a small custom decorator:

```python
from functools import wraps
from django.http import HttpResponseForbidden


def require_role(role: str):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.groups.filter(name=role).exists():
                return HttpResponseForbidden("Insufficient role.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
```

### Tier 2 — data-level via service

```python
# in apps/products/services.py
def update_item(self, pk: int, *, user_id: int, **fields):
    product = self.repo.get_by_id(pk)
    if product.owner_id != user_id:
        raise PermissionError(f"User {user_id} cannot update product {pk}")
    return self.repo.update(pk, **fields)
```

Service raises `PermissionError`; `ServiceExceptionMiddleware` renders `errors/403.html`.

## Step 9: Pagination — htmx-friendly

Server-rendered with infinite scroll via htmx. Use `CursorPage[T]` from **django-repositories**.

```python
# views.py
@login_required
def product_list(request: AuthedRequest):
    cursor = request.GET.get("cursor")
    page = get(ProductService).list_items_paginated(cursor=cursor, limit=20)
    template = "products/_list_rows.html" if request.htmx else "products/list.html"
    return render(request, template, {"page": page})
```

```html
<!-- products/list.html -->
{% extends "base.html" %}
{% block content %}
<table>
  <tbody id="product-rows">
    {% include "products/_list_rows.html" %}
  </tbody>
</table>
{% endblock %}
```

```html
<!-- products/_list_rows.html -->
{% for product in page.items %}
  {% include "products/_row.html" %}
{% endfor %}

{% if page.next_cursor %}
  <tr hx-get="{% url 'products:list' %}?cursor={{ page.next_cursor }}"
      hx-trigger="revealed"
      hx-swap="outerHTML"
      hx-target="this">
    <td colspan="3">Loading more...</td>
  </tr>
{% endif %}
```

The sentinel row triggers `hx-get` when it scrolls into view (`revealed`), fetches the next batch, and replaces itself with the new rows. The new batch carries its own sentinel until `next_cursor` is null.

## Step 10: URLs

`src/apps/<app>/urls.py`:

```python
from django.urls import path

from . import views

app_name = "products"

urlpatterns = [
    path("", views.product_list, name="list"),
    path("create/", views.product_create, name="create"),
    path("<int:pk>/", views.product_detail, name="detail"),
    path("<int:pk>/edit/", views.product_update, name="update"),
    path("<int:pk>/delete/", views.product_delete, name="delete"),
]
```

`src/config/urls.py`:

```python
urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.products.urls_api")),     # DRF (django-api)
    path("products/", include("apps.products.urls", namespace="products")),   # HTML (this skill)
    path("healthz", healthz),
    path("readyz", readyz),
]
```

Coexistence: `urls_api.py` mounts the DRF router; `urls.py` mounts the function-based views. Same app, two presentations, one service.

## Step 11: Static assets

For vanilla htmx + Alpine: just `collectstatic` (already configured in **django-deploy** → S3). Load both libraries via CDN in `base.html` (good enough for most apps) or self-host:

```bash
docker compose exec web uv run python manage.py download_htmx_alpine    # custom command, optional
```

If you reach for Tailwind / TypeScript / a build pipeline, use **`django-vite`** — Vite dev server in dev compose, build to `static/dist/`, collectstatic uploads. Separate concern; mention but don't deep-dive here. The skill stays opinionated against bundlers for simple cases.

## Step 12: Testing HTML views

`src/apps/<app>/tests/test_api.py` can be renamed `test_views.py` for clarity (or split — keep DRF tests in `test_api.py`, HTML tests in `test_views.py`).

```python
import pytest
from django.test import Client


@pytest.fixture
def html_client(django_user_model):
    user = django_user_model.objects.create_user(username="test", password="pw")
    c = Client()
    c.login(username="test", password="pw")
    return c


@pytest.mark.django_db
def test_product_list_renders(html_client, make_product):
    make_product(name="Widget")
    response = html_client.get("/products/")
    assert response.status_code == 200
    assert b"Widget" in response.content
    assert "products/list.html" in [t.name for t in response.templates]


@pytest.mark.django_db
def test_product_create_via_htmx(html_client):
    response = html_client.post(
        "/products/create/",
        data={"name": "Gadget", "price": "12.50", "stock": "3"},
        HTTP_HX_REQUEST="true",     # marks the request as htmx
    )
    assert response.status_code == 200
    assert "products/_card.html" in [t.name for t in response.templates]
    assert b"Gadget" in response.content


@pytest.mark.django_db
def test_product_create_non_htmx_redirects(html_client):
    response = html_client.post(
        "/products/create/",
        data={"name": "Gadget", "price": "12.50", "stock": "3"},
    )
    assert response.status_code == 302
    assert response.url.startswith("/products/")


@pytest.mark.django_db
def test_product_update_rejects_other_owner(html_client, make_product, django_user_model):
    other = django_user_model.objects.create_user(username="other", password="pw")
    product = make_product(name="Widget", owner_id=other.id)

    response = html_client.post(
        f"/products/{product.id}/edit/",
        data={"name": "Hacked"},
    )
    assert response.status_code == 403
    assert "errors/403.html" in [t.name for t in response.templates]
```

### Patterns

- **`Client` not `APIClient`** for HTML tests.
- **`HTTP_HX_REQUEST="true"`** simulates an htmx request — `request.htmx` becomes True in the view.
- **`assertTemplateUsed`** equivalent: check `response.templates`. Lets you verify the partial vs full template was rendered.
- **Same real-DB rule as everything else.** `@pytest.mark.django_db`. No mocking of own services.
- **Permission tests use a second user** — create another user, log in as one, hit an action that requires the other, assert 403 + the 403 template.

## Common Mistakes

- **`ModelForm` instead of `Form`.** Couples to model fields, leaks ORM into the view.
- **ORM access in templates.** `{{ user.products.all }}` triggers a query during render. Always fetch via service in the view, pass to context explicitly.
- **Class-based views with mixin soup.** Function-based is the rule for this skill — short, readable, no MRO surprises.
- **Alpine.js doing data fetching.** Use htmx. If Alpine is calling `fetch()` you've conflated layers.
- **Stimulus on top of htmx + Alpine.** Pick one. Don't have three frameworks doing similar things.
- **JS files for state that could be `x-data`.** Inline Alpine for simple state. Externalize only when reused.
- **CSRF token forgotten on POST forms.** Always `{% csrf_token %}` inside `<form>`. The `hx-headers` on body covers htmx; non-htmx forms still need the template tag.
- **Service exceptions caught in the view.** Let them propagate to `ServiceExceptionMiddleware`. Catching duplicates the mapping in two places.
- **Returning full pages from htmx requests.** Wastes bandwidth, breaks `hx-target` semantics. Return partials.
- **htmx swap with no transition.** Sudden DOM replacements feel janky. Use `hx-swap="outerHTML swap:200ms"` for subtle fade.
- **Embedding business rules in template logic.** `{% if product.price > 100 and user.is_premium %}` belongs in a DTO computed field or service method, not template prose.

## Verify

```bash
make check    # lint + format-check + typecheck
make test     # runs Client + APIClient tests both
docker compose exec web uv run python manage.py check
docker compose exec web uv run python manage.py validate_templates   # if django-template-partials or similar
```

## Checklist

- [ ] `django-htmx` installed; `HtmxMiddleware` in `MIDDLEWARE` after auth
- [ ] `ServiceExceptionMiddleware` in `MIDDLEWARE`, last position
- [ ] Views in `src/apps/<app>/views.py` are function-based
- [ ] `@login_required` (or explicit alternative) on every non-public view
- [ ] No ORM imports in views — every call goes through `get(SomeService)`
- [ ] `user_id=request.user.id` passed explicitly to services that need it
- [ ] Forms in `src/apps/<app>/forms.py` are `forms.Form` (NEVER `ModelForm`)
- [ ] Distinct Create and Update forms
- [ ] No DB queries in form `clean*` methods
- [ ] Templates in `src/templates/<app>/` (or per-app `templates/<app>/`)
- [ ] Partials prefixed with `_`
- [ ] `base.html` has `<body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>` for global CSRF
- [ ] htmx requests return partial templates; non-htmx return full pages + redirect on POST
- [ ] Alpine.js used ONLY for client-side UI state — never data fetching
- [ ] Error templates exist: `errors/{400,403,404,500}.html` + `errors/_inline.html` for htmx
- [ ] HTML view tests use `Client` with `HTTP_HX_REQUEST="true"` to exercise the htmx path
- [ ] DRF (django-api) and HTML (this skill) coexist via separate URL paths — `/api/v1/` for JSON, `/products/` (etc.) for HTML
