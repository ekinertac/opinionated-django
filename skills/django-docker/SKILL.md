---
name: django-docker
description: Set up Docker for local Django development — Dockerfile, docker-compose.yml with web/postgres/redis/celery, .dockerignore, and .env.example. Use when scaffolding a new project, adding Docker to an existing one, or any time the user mentions docker, compose, containers, or local environment setup.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Docker for Local Development

This project uses Docker Compose for local development. Postgres, Redis, the Django dev server, and Celery all run as services. Code is bind-mounted, so edits hot-reload without rebuilds.

Production deployment is out of scope for this skill — `docker-compose.yml` here is dev-only.

## Files

Six files at the repository root:

- `Dockerfile` — single-stage dev image with `uv` and project deps
- `docker-compose.yml` — `web`, `postgres`, `redis`, `celery`, optional `celery-beat`
- `entrypoint.sh` — waits for postgres, optionally runs migrations, then `exec`s the service command
- `Makefile` — wraps the common `docker compose run --rm web ...` invocations behind short targets (`make test`, `make migrate`, etc.)
- `.dockerignore` — keep build context small
- `.env.example` — template for the local `.env` file

## Step 1: `Dockerfile`

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /app

# Install deps in a separate layer for caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

# Entrypoint script: waits for postgres, optionally migrates, then execs the service command
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

WORKDIR /app/src

EXPOSE 8000
```

Notes:
- Base image is Astral's official uv-on-python-slim variant — single `FROM`, no `COPY --from` dance. Astral maintains the combination.
- `UV_PROJECT_ENVIRONMENT=/usr/local` installs into the system Python so no `.venv/` ends up in the bind mount and shadowing host files. It also means `psycopg` is on the system Python path, so the entrypoint's wait-for-postgres check works without `uv run`.
- The `uv sync --no-install-project` first, then full `uv sync` is a standard Docker caching trick — dependency-only layer is cached unless `pyproject.toml`/`uv.lock` change.
- `WORKDIR /app/src` so `manage.py` is on the path; `pythonpath` config in `pyproject.toml` continues to handle test imports.
- `ENTRYPOINT` is fixed in the image; the actual service command (e.g. `runserver`) lives in `docker-compose.yml` so the port can change without rebuilding.

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
    environment:
      RUN_MIGRATIONS: "true"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started

  postgres:
    image: postgres:16-alpine
    volumes:
      - postgres_data:/var/lib/postgresql/data
    env_file:
      - .env
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER}"]
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
- All four services source credentials from the same `.env` file — single source of truth. The postgres image reads `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` directly from the env vars compose injects.
- `RUN_MIGRATIONS: "true"` is set only on `web` so `entrypoint.sh` runs migrations there. Celery and celery-beat hit the same entrypoint, wait for postgres, but skip migrate to avoid races.
- The healthcheck uses `$${POSTGRES_USER}` (escaped `$`) so the variable is expanded inside the container at runtime, not by Compose at parse time.
- `celery` shares the `web` image — same code mount, same deps.
- `celery-beat` lives behind a `beat` profile so it doesn't auto-start. Bring it up with `docker compose --profile beat up`.
- Postgres and Redis ports are exposed to the host so external tools (psql, redis-cli, IDE DB browsers) can connect directly.

## Step 3: `entrypoint.sh`

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
  echo "Running makemigrations + migrate..."
  python manage.py makemigrations
  python manage.py migrate --noinput
fi

exec "$@"
```

Notes:
- Uses `psycopg` (already a dependency) for the readiness check — no extra apt install. The system Python has it because `UV_PROJECT_ENVIRONMENT=/usr/local`.
- Migrations are gated behind `RUN_MIGRATIONS=true` so only the `web` service runs them. Without the gate, celery and celery-beat would race against web on `migrate` at startup.
- `exec "$@"` hands off to the `command:` defined per service in `docker-compose.yml`. PID 1 becomes the actual service, so signals propagate cleanly.
- Auto-`makemigrations` is convenient because the bind mount writes generated migration files back to the host. The trade-off: it can produce noisy migrations from incomplete model edits. If that becomes annoying, drop the `makemigrations` line — `migrate` alone is enough since you'll generate migrations explicitly during dev.

After writing the file, mark it executable on the host: `chmod +x entrypoint.sh`.

## Step 4: `.dockerignore`

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

## Step 5: `Makefile`

```makefile
.PHONY: help up up-d down build logs shell bash migrate makemigrations superuser test lint format format-check typecheck check resetdb psql

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---- stack ----
up: ## Start the stack (foreground)
	docker compose up

up-d: ## Start the stack (detached)
	docker compose up -d

down: ## Stop the stack (preserves postgres volume)
	docker compose down

build: ## Rebuild the web image
	docker compose build

logs: ## Tail logs from all services
	docker compose logs -f

# ---- shells ----
shell: ## Open Django shell in a one-off web container
	docker compose run --rm web uv run python manage.py shell

bash: ## Open bash inside the running web container
	docker compose exec web bash

# ---- django ----
migrate: ## Apply pending migrations
	docker compose run --rm web uv run python manage.py migrate

makemigrations: ## Create migrations from model changes
	docker compose run --rm web uv run python manage.py makemigrations

superuser: ## Create a Django superuser
	docker compose run --rm web uv run python manage.py createsuperuser

# ---- quality ----
test: ## Run the pytest suite
	docker compose run --rm web uv run pytest

lint: ## Lint with ruff
	docker compose run --rm web uv run ruff check .

format: ## Auto-format with ruff
	docker compose run --rm web uv run ruff format .

format-check: ## Verify formatting (no changes)
	docker compose run --rm web uv run ruff format --check .

typecheck: ## Static type analysis with pyrefly
	docker compose run --rm web uv run pyrefly check src

check: lint format-check typecheck ## Lint + format check + typecheck (read-only pre-commit gate)

# ---- data ----
resetdb: ## Destroy and recreate the postgres volume, then migrate
	docker compose down -v
	docker compose up -d postgres
	docker compose run --rm web uv run python manage.py migrate

psql: ## Open psql against the postgres service
	docker compose exec postgres sh -c 'psql -U $$POSTGRES_USER $$POSTGRES_DB'
```

Notes:
- `help` is the default convention — `make help` prints every target with its `##` description. Keep new targets tagged so they show up.
- `$$` in `psql` escapes Make so the shell inside the postgres container sees `$POSTGRES_USER` / `$POSTGRES_DB` and expands them against the container's env.
- `check` chains `lint typecheck` — useful as a single pre-commit gate. Add `test` to it once test runtime is acceptable.
- All targets are `.PHONY` because none produce files. Skipping `.PHONY` causes silent breakage when a directory matches a target name.

## Step 6: `.env.example`

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

Use the Makefile. `make help` lists every target.

```bash
make up            # start the stack
make down          # stop it (postgres volume kept)
make build         # rebuild image after dep changes
make logs          # tail all service logs

make migrate       # apply migrations
make makemigrations
make superuser     # create a Django admin user
make shell         # django shell
make bash          # bash inside the running web container

make test          # pytest
make lint          # ruff check
make format        # ruff format (auto-fix)
make format-check  # ruff format --check (no changes)
make typecheck     # pyrefly
make check         # lint + format-check + typecheck

make resetdb       # nuke postgres volume and re-migrate
make psql          # psql into the postgres service
```

Anything not covered by a target falls through to the raw form, e.g. adding a dep:

```bash
docker compose run --rm web uv add <package>
```

`--rm` removes the throwaway container after the command exits — without it, stopped containers pile up. The Makefile already includes `--rm` everywhere.

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
- All postgres credentials (`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`) come from `.env` — never hardcode them in `docker-compose.yml`, never use Compose's `${VAR:-default}` interpolation. The postgres service uses `env_file: .env` like every other service. Single source of truth.
- Do NOT add a production target to `docker-compose.yml` — this file is dev-only.
- Do NOT pin Python or Postgres major versions inside multiple files — Python lives in the `Dockerfile` `FROM` line, Postgres in `docker-compose.yml`. Bump them in one place.
- Code changes hot-reload because `.:/app` is bind-mounted. Dependency changes (`pyproject.toml`/`uv.lock`) require `docker compose build` (or `docker compose up --build`).
- Database state lives in the `postgres_data` named volume. `docker compose down` keeps it; `docker compose down -v` destroys it.

## Verify

```bash
make build
make up-d
make migrate
docker compose run --rm web uv run python manage.py check
make down
```

All five steps must succeed.
