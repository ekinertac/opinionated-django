---
name: django-scaffold
description: Set up a Django project into the opinionated layout — config/ for project-level settings, urls, celery, and services registry; apps/ for self-contained Django apps with their own models, serializers, views, and tests. Use when starting a new project from scratch or converting an existing one.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Scaffold a Django Project

You are preparing a Django project to use the opinionated patterns. After this skill runs, the other skills can add features on top without any further setup.

Local development runs entirely in Docker Compose (postgres + redis + celery + web). After this skill, run `django-docker` to add the `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and `.env.example`. The settings below already assume those services exist by hostname.

## BEFORE WRITING CODE

Figure out which situation you're in:

- **Greenfield** — no Django project exists yet. You will run `uv init` and `django-admin startproject`, then transform the result.
- **Existing Django project** — a `manage.py`, `settings.py`, and at least one app already exist. You will add the `config/` shell alongside what's there and relocate files only if asked.

Read `pyproject.toml` (if present) and locate `manage.py` and `settings.py` so you know the project's current layout. Confirm with the user before moving any existing files.

## Target Layout

```
Dockerfile
docker-compose.yml
.dockerignore
.env.example
src/
  manage.py
  config/
    __init__.py
    urls.py
    wsgi.py
    asgi.py
    celery.py
    services.py          # svcs registry + get() helper
    types.py             # AuthedRequest and other shared typing aliases
    signals.py           # ReliableSignal base + send_reliable machinery
    exception_handler.py # Central DRF exception handler
    settings/
      __init__.py
      base.py            # Shared settings across all environments
      local.py           # Development overrides
      production.py      # Production overrides
  apps/
    __init__.py
    <app>/
      __init__.py
      apps.py
      models.py
      admin.py
      urls.py            # App-level URL routing with DRF router
      views.py           # DRF ViewSets
      serializers.py     # DRF Serializers for input validation
      services.py        # Business logic
      repositories.py    # ORM access, returns DTOs
      dtos.py            # Pydantic DTOs
      signals.py         # Optional, defines ReliableSignal instances
      receivers.py       # Optional, @receiver handlers — must be idempotent
      tests/
        __init__.py
        test_repo.py
        test_service.py
        test_api.py
pyproject.toml
```

Each app is a self-contained unit — models, views, serializers, services, repositories, and tests all live together. Apps use single files (not packages) unless a file grows large enough to warrant splitting.

## Step 1: Dependencies

Use `uv` for everything. Never `pip` or `poetry`.

```bash
uv add 'django>=6.0' 'djangorestframework>=3.16' 'drf-spectacular>=0.28' \
       'drf-nested-routers>=0.94' 'pydantic>=2.0' 'svcs>=25.1' \
       'celery>=5.4' 'psycopg[binary]>=3.2' python-decouple
uv add --dev ruff 'pyrefly>=0.42' django-stubs pytest pytest-django
```

`psycopg[binary]` is the Postgres driver — Compose's `postgres` service is the dev database.

Pyrefly auto-recognizes Django constructs as long as `django-stubs` is installed — no plugin, no `mypy_django_plugin`-style config.

## Step 2: `src/config/services.py`

```python
import svcs

registry = svcs.Registry()

# Register repositories and services here as the project grows.
# Example:
# from apps.products.repositories import ProductRepository
# from apps.products.services import ProductService
#
# registry.register_factory(ProductRepository, ProductRepository)
#
# def _product_service_factory(container: svcs.Container) -> ProductService:
#     return ProductService(container.get(ProductRepository))
#
# registry.register_factory(ProductService, _product_service_factory)


def get[T](service_type: type[T]) -> T:
    """Get a service from the registry. Works anywhere — views, tasks, commands."""
    return svcs.Container(registry).get(service_type)
```

## Step 3: `src/config/types.py`

Narrows `request.user` to a guaranteed-authenticated Django `User` so views don't have to deal with `AnonymousUser` unions.

```python
from django.contrib.auth.models import User
from django.http import HttpRequest


class AuthedRequest(HttpRequest):
    """
    An HttpRequest whose `user` attribute is guaranteed to be an authenticated User.

    Use as the first-argument annotation on any DRF view that requires auth.
    The narrowing is a typing contract, not runtime enforcement — pair this with
    DRF's permission classes or a middleware that rejects anonymous requests.
    """
    user: User  # type: ignore[assignment]
```

## Step 4: `src/config/exception_handler.py`

Services raise plain Python exceptions — `ValueError` for bad input, `LookupError` for missing records, `PermissionError` for forbidden access. They do NOT know about HTTP. The mapping happens once, centrally:

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

## Step 5: `src/config/urls.py`

```python
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    # App URLs — each app owns its own routing
    # path("api/", include("apps.products.urls")),
    # path("api/", include("apps.orders.urls")),
]
```

Each app defines its own `urls.py` with a DRF router and ViewSets. The root `urls.py` includes them.

## Step 6: App-Level URL Routing

Each app owns its own `urls.py` with a DRF router:

`src/apps/products/urls.py`:

```python
from rest_framework.routers import DefaultRouter

from .views import ProductViewSet

router = DefaultRouter()
router.register(r"products", ProductViewSet, basename="product")

urlpatterns = router.urls
```

For nested resources within an app, use `drf-nested-routers`:

```python
from rest_framework_nested import routers

from .views import OrderViewSet, OrderItemViewSet

router = routers.DefaultRouter()
router.register(r"orders", OrderViewSet, basename="order")

orders_router = routers.NestedDefaultRouter(router, r"orders", lookup="order")
orders_router.register(r"items", OrderItemViewSet, basename="order-items")

urlpatterns = router.urls + orders_router.urls
```

## Step 7: `src/config/signals.py` — Reliable Signals

This module provides the `ReliableSignal` base that apps import. Receivers run asynchronously via Celery, and `send_reliable()` enqueues them inside the current DB transaction so rollbacks are respected.

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
    """A Django Signal whose receivers run asynchronously via Celery.

    - `send_reliable()` must be called inside a `transaction.atomic()` block.
    - Receiver tasks are enqueued on transaction commit, so rollbacks are respected.
    - Delivery is at-least-once. Every receiver MUST be idempotent.
    - Arguments MUST be JSON-serializable (pass IDs, never model instances).
    """

    def send_reliable(self, sender, **kwargs) -> None:
        payload = json.dumps(kwargs)
        for _, receiver in self._live_receivers(sender):
            path = f"{receiver.__module__}.{receiver.__qualname__}"
            transaction.on_commit(
                lambda p=path: _dispatch_reliable_receiver.delay(p, payload)
            )
```

## Step 8: Celery

Create `src/config/celery.py`:

```python
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["apps"])
```

In `src/config/__init__.py`:

```python
from .celery import app as celery_app

__all__ = ("celery_app",)
```

## Step 9: Settings

Split settings into three files under `src/config/settings/`:

### `src/config/settings/__init__.py`

Empty or imports from the default environment:

```python
```

### `src/config/settings/base.py`

Shared settings across all environments:

```python
from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config("SECRET_KEY", default="change-me-in-production")
DEBUG = False
ALLOWED_HOSTS = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "drf_spectacular",
    # Project apps
    # "apps.products.apps.ProductsConfig",
    # "apps.orders.apps.OrdersConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- DRF ---
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "config.exception_handler.custom_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "API",
    "VERSION": "1.0.0",
}

# --- Celery ---
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://redis:6379/1")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="redis://redis:6379/2")
```

### `src/config/settings/local.py`

```python
from .base import *  # noqa: F401, F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
```

### `src/config/settings/production.py`

```python
from .base import *  # noqa: F401, F403

DEBUG = False
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="", cast=lambda v: v.split(","))
```

### `src/config/wsgi.py` and `src/config/asgi.py`

```python
# wsgi.py
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
application = get_wsgi_application()
```

```python
# asgi.py
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
application = get_asgi_application()
```

### `src/manage.py`

```python
#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
```

## Step 10: Tooling config in `pyproject.toml`

```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.pyrefly]
project-includes = ["src"]
python-version = "3.12"

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.local"
python_files = ["test_*.py"]
pythonpath = ["src"]
```

**Pyrefly + Django caveats** (from [pyrefly.org/en/docs/django](https://pyrefly.org/en/docs/django/)):

- Pyrefly has **built-in** Django support. Install `django-stubs` and it just works — no plugin to enable, no extra `[tool.pyrefly]` keys required.
- **Reverse relations are not yet supported.** Accessing `user.order_set` (the implicit reverse manager Django generates from a `ForeignKey`) will flag as an attribute error. Work around it in the repository layer by either (a) querying the child model directly or (b) using an explicit `related_name` and a narrow `cast` / `# type: ignore[attr-defined]` at the call site.
- **`ManyRelatedManager` is generic over `[Parent, Model]`** rather than the concrete child type (unlike mypy's django-plugin). For DTO coercion this doesn't matter — the `coerce_related_manager` validator handles it — but don't rely on pyrefly to catch mistyped M2M targets.
- Django's `QuerySet` typing beyond `.all()` is still thin. Keep chained queryset expressions inside the repository where you can annotate the return type as `list[SomeDTO]` and let the caller rely on that.
- Pyrefly's Django support is **actively evolving**; re-check the docs when upgrading pyrefly and remove workarounds as they become unnecessary.

## Step 11: Verify

All commands run inside the `web` container (after `django-docker` has scaffolded the Compose stack):

```bash
docker compose run --rm web uv run python manage.py check
docker compose run --rm web uv run ruff check src
docker compose run --rm web uv run ruff format --check src
docker compose run --rm web uv run pyrefly check src
docker compose run --rm web uv run pytest
```

All five must pass. Fix any issue rather than silencing it.

## COMPLETION CHECKLIST

- [ ] Dependencies added via `uv add` (including `djangorestframework`, `drf-spectacular`, `drf-nested-routers`)
- [ ] `src/config/services.py` with `registry` and `get()`
- [ ] `src/config/types.py` with `AuthedRequest`
- [ ] `src/config/exception_handler.py` with central DRF exception handler
- [ ] `src/config/signals.py` with `ReliableSignal` base
- [ ] `src/config/celery.py` + `__init__.py` export
- [ ] `src/config/settings/base.py` with `REST_FRAMEWORK`, `SPECTACULAR_SETTINGS`, `CELERY_*`
- [ ] `src/config/settings/local.py` and `src/config/settings/production.py`
- [ ] `src/config/urls.py` includes app URLs and drf-spectacular schema/docs views
- [ ] `src/config/wsgi.py` and `src/config/asgi.py` point to `config.settings.local`
- [ ] `src/manage.py` points to `config.settings.local`
- [ ] `pyproject.toml` has ruff / pyrefly / pytest config with `DJANGO_SETTINGS_MODULE = "config.settings.local"`
- [ ] `django check`, ruff, pyrefly, pytest all pass

Once this checklist is complete, the other skills can build features on top without any extra setup.
