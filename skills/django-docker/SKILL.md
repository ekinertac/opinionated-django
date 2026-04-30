---
name: django-docker
description: Set up Docker for local Django development — Dockerfile, docker-compose.yml with web/postgres/redis/celery, .dockerignore, and .env.example. Use when scaffolding a new project, adding Docker to an existing one, or any time the user mentions docker, compose, containers, or local environment setup.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Docker for Local Development

This project uses Docker Compose for local development. Postgres, Redis, the Django dev server, and Celery all run as services. Code is bind-mounted, so edits hot-reload without rebuilds.

Production deployment is out of scope for this skill — `docker-compose.yml` here is dev-only.

## Files

Four files at the repository root:

- `Dockerfile` — single-stage dev image with `uv` and project deps
- `docker-compose.yml` — `web`, `postgres`, `redis`, `celery`, optional `celery-beat`
- `.dockerignore` — keep build context small
- `.env.example` — template for the local `.env` file

## Step 1: `Dockerfile`

```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps in a separate layer for caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

WORKDIR /app/src

EXPOSE 8000

CMD ["uv", "run", "python", "manage.py", "runserver", "0.0.0.0:8000"]
```

Notes:
- `UV_PROJECT_ENVIRONMENT=/usr/local` installs into the system Python so no `.venv/` ends up in the bind mount and shadowing host files.
- The `uv sync --no-install-project` first, then full `uv sync` is a standard Docker caching trick — dependency-only layer is cached unless `pyproject.toml`/`uv.lock` change.
- `WORKDIR /app/src` so `manage.py` is on the path; `pythonpath` config in `pyproject.toml` continues to handle test imports.

## Step 2: `docker-compose.yml`

```yaml
services:
  web:
    build: .
    command: uv run python manage.py runserver 0.0.0.0:8000
    volumes:
      - .:/app
    ports:
      - "8000:8000"
    env_file:
      - .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started

  postgres:
    image: postgres:16-alpine
    volumes:
      - postgres_data:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-app}
      POSTGRES_USER: ${POSTGRES_USER:-app}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-app}
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-app}"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  celery:
    build: .
    command: uv run celery -A config worker -l info
    volumes:
      - .:/app
    env_file:
      - .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started

  celery-beat:
    build: .
    command: uv run celery -A config beat -l info
    volumes:
      - .:/app
    env_file:
      - .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    profiles:
      - beat

volumes:
  postgres_data:
```

Notes:
- `celery` shares the `web` image — same code mount, same deps.
- `celery-beat` lives behind a `beat` profile so it doesn't auto-start. Bring it up with `docker compose --profile beat up`.
- Postgres and Redis ports are exposed to the host so external tools (psql, redis-cli, IDE DB browsers) can connect directly.

## Step 3: `.dockerignore`

```
.git
.venv
__pycache__
*.pyc
*.pyo
.pytest_cache
.ruff_cache
.mypy_cache
.coverage
htmlcov/
node_modules/
.env
.env.*
!.env.example
*.sqlite3
.DS_Store
```

`.env` is excluded because secrets get injected via `env_file`, not baked into the image.

## Step 4: `.env.example`

```
# Django
DJANGO_SETTINGS_MODULE=config.settings.local
SECRET_KEY=dev-secret-key-change-me
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1,web

# Postgres
POSTGRES_DB=app
POSTGRES_USER=app
POSTGRES_PASSWORD=app
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

# Redis / Celery
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2
```

After scaffolding, the user copies it: `cp .env.example .env`.

## Standard Commands

All dev work runs through Compose. Memorize this prefix: **`docker compose run --rm web`**.

```bash
# Bring the stack up (web, postgres, redis, celery)
docker compose up

# In the background
docker compose up -d

# Tear down (preserves the postgres volume)
docker compose down

# One-off Django command
docker compose run --rm web uv run python manage.py migrate
docker compose run --rm web uv run python manage.py createsuperuser
docker compose run --rm web uv run python manage.py shell

# Tests
docker compose run --rm web uv run pytest

# Lint / format / type-check
docker compose run --rm web uv run ruff check .
docker compose run --rm web uv run ruff format .
docker compose run --rm web uv run pyrefly check .

# Add a dependency (writes to pyproject.toml + uv.lock on the host via bind mount)
docker compose run --rm web uv add <package>

# Shell into the running web container
docker compose exec web bash

# Reset the database
docker compose down -v   # destroys postgres_data volume
docker compose up
docker compose run --rm web uv run python manage.py migrate
```

`--rm` removes the throwaway container after the command exits — without it, stopped containers pile up.

## How Settings Wire to Compose

`src/config/settings/local.py` reads env vars (set by `env_file: .env` in compose) for service hostnames. Defaults match the compose service names so the project boots with no manual configuration:

```python
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

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://redis:6379/1")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="redis://redis:6379/2")
```

Inside the `web` container, `postgres` and `redis` resolve via Compose's built-in DNS. Outside (e.g. running `manage.py` from the host), override `POSTGRES_HOST=localhost` and `REDIS_URL=redis://localhost:6379/0` in your shell — but the recommended path is to always work through `docker compose run --rm web`.

## Rules

- `psycopg[binary]>=3.2` must be in dependencies (Postgres driver). Add via `uv add 'psycopg[binary]'`.
- Do NOT commit `.env`. Do commit `.env.example`.
- Do NOT add a production target to `docker-compose.yml` — this file is dev-only.
- Do NOT pin Python or Postgres major versions inside multiple files — Python lives in the `Dockerfile` `FROM` line, Postgres in `docker-compose.yml`. Bump them in one place.
- Code changes hot-reload because `.:/app` is bind-mounted. Dependency changes (`pyproject.toml`/`uv.lock`) require `docker compose build` (or `docker compose up --build`).
- Database state lives in the `postgres_data` named volume. `docker compose down` keeps it; `docker compose down -v` destroys it.

## Verify

```bash
docker compose build
docker compose up -d
docker compose run --rm web uv run python manage.py migrate
docker compose run --rm web uv run python manage.py check
docker compose down
```

All four steps must succeed.
