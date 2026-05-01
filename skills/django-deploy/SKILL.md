---
name: django-deploy
description: Deploy a Django project to production at mid-scale — multi-stage production Dockerfile, production settings, gunicorn config, S3 for static and media, Sentry for errors, JSON logging, health and readiness endpoints, an Ansible playbook structure with Vault-encrypted secrets, and a rolling deploy across multiple web hosts behind a load balancer with a separate Celery worker host. Use when setting up production for the first time, adding deployment infrastructure to an existing project, or whenever the user mentions deploy, production, Ansible, gunicorn, staging, or prod.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Production Deployment

This skill covers a mid-scale production topology for the Django monolith this skill set produces — enough for tens of thousands of users on a single region, with room to grow to hundreds of thousands before architectural splits are needed. The deployment artifact is a Docker image; the deployment mechanism is Ansible.

## Topology

```
                    Cloud Load Balancer
                    (TLS termination)
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
        web-01          web-02          web-N         (N gunicorn hosts)
            │               │               │
            └───────────────┼───────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        worker-01     redis-broker   redis-cache     (separate Redis instances)
        (celery +          │             │
         celery-beat)      │             │
              │            │             │
              └────────────┼─────────────┘
                           ▼
                  Managed PostgreSQL
                  (with read replica when needed)

                  S3 / CloudFront        Sentry
                  (static + media)       (errors)
```

Components:

| Role | Count | What it runs |
|---|---|---|
| **Load balancer** | 1 (managed) | DigitalOcean LB, Hetzner LB, AWS ALB. Terminates TLS. Forwards to web hosts on port 8000. |
| **web** | 2+ | gunicorn container serving Django. Behind LB. Runs the production image. |
| **worker** | 1 | celery worker + celery-beat container. Runs the same image, different command. |
| **postgres** | managed | DigitalOcean Managed Postgres, AWS RDS, etc. The skill assumes managed; self-hosted is a footnote. |
| **redis-broker** | managed | Celery broker. Separate instance from cache. |
| **redis-cache** | managed | Application cache. Separate instance from broker. |
| **S3 + CDN** | managed | Static and media files. CloudFront/CloudFlare in front. |
| **Sentry** | SaaS | Error tracking and performance monitoring. |

The dev compose stack from **django-docker** is for local development only. Production runs the same image, different command, and never bind-mounts source code.

## Step 1: Production Dockerfile

Add a production target to the existing `Dockerfile` as a second stage. The dev target stays for local; CI builds the prod target.

```dockerfile
# syntax=docker/dockerfile:1.7

ARG UV_IMAGE=ghcr.io/astral-sh/uv:python3.13-bookworm-slim
ARG PY_IMAGE=python:3.13-slim-bookworm

# ---- builder ---------------------------------------------------------------
FROM ${UV_IMAGE} AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

# ---- prod runtime ---------------------------------------------------------
FROM ${PY_IMAGE} AS prod

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

# Postgres client libs for psycopg
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local /usr/local
COPY --from=builder --chown=app:app /app /app

COPY --chown=app:app entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER app
WORKDIR /app/src

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ---- dev runtime (unchanged from django-docker) ---------------------------
FROM ${UV_IMAGE} AS dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

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

The dev compose `build:` clause now points at the dev target: `target: dev`. CI builds with `--target prod` for the production image.

Notes:
- **Non-root user** in prod. Containers run as `app`, never root.
- **`uv sync --no-dev`** in the builder strips dev dependencies (pytest, ruff, etc.) — production image is leaner.
- **`libpq5`** is needed at runtime for psycopg's binary build to find Postgres client libraries on the slim base.
- **No collectstatic in the build** — static files go to S3 during deploy (Step 4), not into the image.
- **Same entrypoint script** as dev. The wait-for-postgres logic is useful in prod too. The `RUN_MIGRATIONS` flag stays unset in production (migrations run as an explicit Ansible step).

## Step 2: `compose.prod.yml`

```yaml
services:
  web:
    image: ${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}
    command: gunicorn config.wsgi:application -c /app/src/gunicorn_config.py
    env_file:
      - .env.production
    ports:
      - "8000:8000"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

  celery:
    image: ${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}
    command: celery -A config worker -l info --concurrency 4
    env_file:
      - .env.production
    restart: unless-stopped

  celery-beat:
    image: ${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}
    command: celery -A config beat -l info
    env_file:
      - .env.production
    restart: unless-stopped
```

Notes:
- **No `volumes`** — the image is the source of truth. Bind mounts in production defeat the entire point of immutable deploys.
- **No `postgres` / `redis` services** — those are managed externals. The web/celery containers connect to them over the network via `.env.production`.
- **Web hosts run only `web`**; the worker host runs `celery` and `celery-beat`. Ansible deploys the same `compose.prod.yml` everywhere and brings up only the services each host needs (Step 9).
- **`IMAGE_TAG`** is set by Ansible at deploy time — typically the git SHA or a release tag. Never `latest` in production.
- **`env_file`** points at `.env.production`, which Ansible writes from Vault-decrypted values (Step 10).

## Step 3: Production settings

`src/config/settings/production.py` builds on `base.py` with the security, logging, and storage hardening that matters in prod.

```python
from decouple import Csv, config

from .base import *  # noqa: F401, F403

# =============================================================================
# SECURITY
# =============================================================================
DEBUG = False
ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())

# LB terminates TLS — trust the X-Forwarded-Proto header it sets.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365  # 1 year, after you've verified things work
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", cast=Csv())

# =============================================================================
# DATABASE
# =============================================================================
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("POSTGRES_DB"),
        "USER": config("POSTGRES_USER"),
        "PASSWORD": config("POSTGRES_PASSWORD"),
        "HOST": config("POSTGRES_HOST"),
        "PORT": config("POSTGRES_PORT", default="5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"sslmode": config("POSTGRES_SSLMODE", default="require")},
    }
}

# =============================================================================
# CACHE (separate Redis instance from the broker)
# =============================================================================
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("REDIS_CACHE_URL"),
    }
}

# =============================================================================
# CELERY (broker is a separate Redis instance from the cache)
# =============================================================================
CELERY_BROKER_URL = config("CELERY_BROKER_URL")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=None)

# =============================================================================
# STORAGES (static + media on S3)
# =============================================================================
AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME")
AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME")
AWS_S3_CUSTOM_DOMAIN = config("AWS_S3_CUSTOM_DOMAIN", default=None)  # CloudFront domain
AWS_QUERYSTRING_AUTH = False  # public-read static files

STORAGES = {
    "default": {  # media
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {"location": "media", "default_acl": "private"},
    },
    "staticfiles": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {"location": "static", "default_acl": "public-read"},
    },
}

# =============================================================================
# LOGGING (JSON to stdout — captured by the host log shipper)
# =============================================================================
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# =============================================================================
# SENTRY
# =============================================================================
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.redis import RedisIntegration

sentry_sdk.init(
    dsn=config("SENTRY_DSN"),
    environment=config("SENTRY_ENVIRONMENT", default="production"),
    release=config("RELEASE_VERSION", default=None),  # set at deploy time
    integrations=[
        DjangoIntegration(),
        CeleryIntegration(),
        RedisIntegration(),
    ],
    traces_sample_rate=config("SENTRY_TRACES_SAMPLE_RATE", default=0.1, cast=float),
    send_default_pii=False,
)
```

Add `django-storages[s3]`, `sentry-sdk[django]`, and `python-json-logger` to dependencies:

```bash
docker compose exec web uv add 'django-storages[s3]>=1.14' 'sentry-sdk[django]>=2.0' python-json-logger
```

## Step 4: Static and media on S3

`STORAGES` config is in Step 3. Two operational details:

- **`collectstatic` runs at deploy time, not build time.** Build-time collection would require AWS credentials in the build pipeline and re-run on every image build even when static files haven't changed. The Ansible deploy play (Step 11) runs `python manage.py collectstatic --noinput` on one host as a one-shot — it uploads only changed files thanks to `S3Storage`'s checksum check.
- **CDN** — put CloudFront (or CloudFlare) in front of the bucket. Set `AWS_S3_CUSTOM_DOMAIN=cdn.example.com`. URLs in templates will resolve via the CDN.

## Step 5: Health and readiness endpoints

Production orchestrators need three flavors of probe; for this topology we expose two:

- **`/healthz`** — liveness. Process is alive, can return a response.
- **`/readyz`** — readiness. Process can do its job (DB reachable). LB and Compose healthcheck both poll this.

Add to `src/config/views.py`:

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

Wire in `src/config/urls.py`:

```python
from .views import healthz, readyz

urlpatterns = [
    # ...existing entries...
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
]
```

`/healthz` and `/readyz` use no trailing slash so the LB doesn't get redirected by `APPEND_SLASH`.

## Step 6: gunicorn config

`src/gunicorn_config.py`:

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

accesslog = "-"  # stdout, captured by Docker, formatted by JSON logger via app
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Restart workers periodically to recycle memory
max_requests = 1000
max_requests_jitter = 100

# Drain in-flight requests on SIGTERM (rolling restart safety)
worker_exit_on_app_exit = True
```

Tune `GUNICORN_WORKERS` per host based on CPU count and memory pressure. The default `2 * CPU + 1` is the gunicorn docs' rule of thumb for sync-ish workloads; for async-heavy code, use `uvicorn`-class workers instead.

## Step 7: Ansible deployment structure

The deploy lives in a `deploy/` directory at the repo root.

```
deploy/
  inventory.yml
  ansible.cfg
  group_vars/
    all/
      vars.yml          # plain config (image registry, repo URL)
      vault.yml         # encrypted secrets (Vault)
  playbooks/
    deploy.yml          # the rolling deploy
    rollback.yml        # rollback to a prior image tag
  roles/
    docker_login/       # log into the image registry on a host
    pull_image/
    write_env/          # render .env.production from group_vars + vault
    migrate/            # one-shot db migration
    collectstatic/      # one-shot static upload to S3
    restart_web/        # rolling restart of web compose service
    restart_worker/     # restart worker compose services
```

`deploy/inventory.yml`:

```yaml
all:
  children:
    web:
      hosts:
        web-01.example.com:
        web-02.example.com:
    worker:
      hosts:
        worker-01.example.com:
  vars:
    ansible_user: deploy
    ansible_python_interpreter: /usr/bin/python3
```

`deploy/ansible.cfg`:

```ini
[defaults]
inventory = ./inventory.yml
host_key_checking = True
forks = 5
retry_files_enabled = False
stdout_callback = yaml
roles_path = ./roles

[ssh_connection]
pipelining = True
```

The play structure stays small and focused. Heavyweight provisioning (installing Docker, configuring the firewall, swap, fail2ban) is **not** in this skill — point at community roles like `geerlingguy.docker`, `geerlingguy.security`, and `geerlingguy.swap`.

## Step 8: Ansible Vault for secrets

Production secrets — `SECRET_KEY`, `POSTGRES_PASSWORD`, `SENTRY_DSN`, `AWS_*`, `CELERY_BROKER_URL` (with credentials), etc. — live in `deploy/group_vars/all/vault.yml`, encrypted with Ansible Vault.

Setup:

```bash
# Generate a vault password and store it OUTSIDE the repo
echo "$(openssl rand -base64 32)" > ~/.config/django-deploy/vault_pass
chmod 400 ~/.config/django-deploy/vault_pass

# Create the vault file
ansible-vault create deploy/group_vars/all/vault.yml \
  --vault-password-file ~/.config/django-deploy/vault_pass
```

`deploy/group_vars/all/vault.yml` (decrypted view):

```yaml
vault_secret_key: "django-secret-key-here"
vault_postgres_password: "postgres-password-here"
vault_sentry_dsn: "https://abc@sentry.io/123"
vault_aws_access_key_id: "AKIA..."
vault_aws_secret_access_key: "..."
vault_redis_broker_url: "rediss://:pass@broker.host:6379/0"
vault_redis_cache_url: "rediss://:pass@cache.host:6379/0"
```

`deploy/group_vars/all/vars.yml` (committed, plain):

```yaml
image_registry: "registry.example.com"
image_name: "myapp"
postgres_host: "db.example.com"
postgres_db: "app"
postgres_user: "app"
allowed_hosts: "app.example.com,api.example.com"
csrf_trusted_origins: "https://app.example.com"
aws_storage_bucket_name: "myapp-static-prod"
aws_s3_region_name: "us-east-1"
aws_s3_custom_domain: "cdn.example.com"
sentry_environment: "production"

secret_key: "{{ vault_secret_key }}"
postgres_password: "{{ vault_postgres_password }}"
sentry_dsn: "{{ vault_sentry_dsn }}"
aws_access_key_id: "{{ vault_aws_access_key_id }}"
aws_secret_access_key: "{{ vault_aws_secret_access_key }}"
celery_broker_url: "{{ vault_redis_broker_url }}"
redis_cache_url: "{{ vault_redis_cache_url }}"
```

Edit later with `ansible-vault edit`. Rotate keys with `ansible-vault rekey`. **Never** commit the vault password file — `.gitignore` it explicitly.

## Step 9: The deploy playbook

`deploy/playbooks/deploy.yml`:

```yaml
- name: Resolve image tag
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Use IMAGE_TAG from environment, fall back to git SHA
      ansible.builtin.set_fact:
        image_tag: "{{ lookup('env', 'IMAGE_TAG') | default(lookup('pipe', 'git rev-parse --short HEAD'), true) }}"

- name: Push the env file to every host
  hosts: all
  become: true
  tasks:
    - name: Render .env.production from vars + vault
      ansible.builtin.template:
        src: ../templates/env.production.j2
        dest: /opt/app/.env.production
        owner: deploy
        group: deploy
        mode: "0600"

    - name: Push compose.prod.yml
      ansible.builtin.copy:
        src: ../../compose.prod.yml
        dest: /opt/app/compose.prod.yml
        owner: deploy
        group: deploy
        mode: "0644"

    - name: Log in to image registry
      community.docker.docker_login:
        registry_url: "{{ image_registry }}"
        username: "{{ vault_registry_user }}"
        password: "{{ vault_registry_password }}"

    - name: Pull the new image
      community.docker.docker_image:
        name: "{{ image_registry }}/{{ image_name }}:{{ hostvars['localhost'].image_tag }}"
        source: pull

- name: Run migrations on one host (any web host will do)
  hosts: web[0]
  become: true
  vars:
    image_tag: "{{ hostvars['localhost'].image_tag }}"
  tasks:
    - name: Run migrate
      ansible.builtin.command:
        cmd: >
          docker compose -f /opt/app/compose.prod.yml run --rm
          -e IMAGE_TAG={{ image_tag }} -e IMAGE_REGISTRY={{ image_registry }} -e IMAGE_NAME={{ image_name }}
          web python manage.py migrate --noinput
        chdir: /opt/app

    - name: Run collectstatic
      ansible.builtin.command:
        cmd: >
          docker compose -f /opt/app/compose.prod.yml run --rm
          -e IMAGE_TAG={{ image_tag }} -e IMAGE_REGISTRY={{ image_registry }} -e IMAGE_NAME={{ image_name }}
          web python manage.py collectstatic --noinput
        chdir: /opt/app

- name: Rolling restart of web hosts
  hosts: web
  serial: 1                       # one host at a time
  become: true
  vars:
    image_tag: "{{ hostvars['localhost'].image_tag }}"
  tasks:
    - name: Restart web service with new image
      ansible.builtin.command:
        cmd: docker compose -f /opt/app/compose.prod.yml up -d web
        chdir: /opt/app
      environment:
        IMAGE_TAG: "{{ image_tag }}"
        IMAGE_REGISTRY: "{{ image_registry }}"
        IMAGE_NAME: "{{ image_name }}"

    - name: Wait for /readyz to return 200
      ansible.builtin.uri:
        url: "http://{{ inventory_hostname }}:8000/readyz"
        status_code: 200
      retries: 30
      delay: 2

- name: Restart worker host
  hosts: worker
  become: true
  vars:
    image_tag: "{{ hostvars['localhost'].image_tag }}"
  tasks:
    - name: Restart celery worker and beat
      ansible.builtin.command:
        cmd: docker compose -f /opt/app/compose.prod.yml up -d celery celery-beat
        chdir: /opt/app
      environment:
        IMAGE_TAG: "{{ image_tag }}"
        IMAGE_REGISTRY: "{{ image_registry }}"
        IMAGE_NAME: "{{ image_name }}"
```

`deploy/templates/env.production.j2`:

```
DJANGO_SETTINGS_MODULE=config.settings.production
SECRET_KEY={{ secret_key }}
ALLOWED_HOSTS={{ allowed_hosts }}
CSRF_TRUSTED_ORIGINS={{ csrf_trusted_origins }}
RELEASE_VERSION={{ hostvars['localhost'].image_tag }}

POSTGRES_DB={{ postgres_db }}
POSTGRES_USER={{ postgres_user }}
POSTGRES_PASSWORD={{ postgres_password }}
POSTGRES_HOST={{ postgres_host }}
POSTGRES_PORT=5432
POSTGRES_SSLMODE=require

CELERY_BROKER_URL={{ celery_broker_url }}
REDIS_CACHE_URL={{ redis_cache_url }}

AWS_STORAGE_BUCKET_NAME={{ aws_storage_bucket_name }}
AWS_S3_REGION_NAME={{ aws_s3_region_name }}
AWS_S3_CUSTOM_DOMAIN={{ aws_s3_custom_domain }}
AWS_ACCESS_KEY_ID={{ aws_access_key_id }}
AWS_SECRET_ACCESS_KEY={{ aws_secret_access_key }}

SENTRY_DSN={{ sentry_dsn }}
SENTRY_ENVIRONMENT={{ sentry_environment }}
```

## Step 10: `make deploy`

In the project Makefile:

```makefile
.PHONY: deploy rollback

deploy: ## Deploy the current commit to production
	cd deploy && ansible-playbook playbooks/deploy.yml \
		--vault-password-file ~/.config/django-deploy/vault_pass

rollback: ## Roll back to a prior image tag — pass TAG=<sha>
	@test -n "$(TAG)" || (echo "TAG=<sha> required" && exit 1)
	cd deploy && IMAGE_TAG=$(TAG) ansible-playbook playbooks/deploy.yml \
		--vault-password-file ~/.config/django-deploy/vault_pass
```

Run from the repo root: `make deploy`. The image tag defaults to the current `git rev-parse --short HEAD`, so deploying after a `git push` cycles the production image to whatever's checked out.

Rolling deploy semantics:

1. Migrations run **before** any web container restarts. New code starts on a schema that's already migrated.
2. `serial: 1` on the web play restarts one host at a time. The LB drains in-flight requests, the host pulls the new image, the new container starts, `/readyz` is polled until 200, then the next host begins.
3. The worker host restarts last. Receivers pick up new code without losing in-flight tasks (graceful shutdown drains the queue).

This is "stop one, restart one" — not zero-downtime if a migration is non-backwards-compatible. For schema changes that the running code can't tolerate, use the expand-contract pattern (separate skill, `django-migrations-scale`).

## Step 11: Rollback

Image tags are pinned per deploy. To roll back:

```bash
make rollback TAG=abc1234
```

The play re-runs against the named tag. Migrations are NOT auto-reverted — that's a manual decision. If the bad deploy included a migration that's incompatible with the previous code, hand-write a reverse migration first.

## Provisioning (out of scope)

The skill assumes the hosts already have:
- Docker Engine + Compose plugin
- A `deploy` user with SSH key auth and Docker group membership
- Firewall rules: 22 (SSH from your IP), 8000 (only from LB)
- Time sync (chrony / systemd-timesyncd)
- Swap configured
- Log shipper to send Docker logs somewhere persistent

Use community Ansible roles for these — `geerlingguy.docker`, `geerlingguy.security`, `geerlingguy.swap`, etc. Don't reinvent these inside this skill.

## Common Mistakes

- **Running `runserver` in production.** Use gunicorn. `runserver` is single-threaded, prints stack traces with debug info, and is explicitly not for production.
- **Bind-mounting source code in `compose.prod.yml`.** Defeats the immutable-image guarantee. Production runs whatever's baked into the image; dev runs whatever's in your editor.
- **Missing `SECURE_PROXY_SSL_HEADER`.** Django thinks every request is HTTP and `request.is_secure()` returns False. Cookies marked secure get rejected. Forms break.
- **Pinning to `:latest`.** Reproducibility goes out the window. Always deploy a specific git SHA or release tag.
- **Forgetting `collectstatic`.** Static files 404. The deploy play handles this; if you bypass Ansible, you'll skip it.
- **Running migrations from every web host.** Race conditions. The deploy play runs migrate exactly once, on `web[0]`, before any restart.
- **Letting Postgres or Redis ports face the internet.** They should only be reachable from your VPCs / firewall'd hosts. Managed services handle this; if you self-host, configure pg_hba.conf and Redis ACLs accordingly.
- **`.env.production` committed to git.** Use Ansible Vault. The `.env.production` on each host is generated by Ansible and not version-controlled.
- **Vault password file inside the repo.** It belongs in `~/.config/django-deploy/`, never tracked.

## Verify

```bash
# Build the prod image locally
docker build --target prod -t myapp:test .

# Smoke-test it (requires a Postgres + Redis somewhere)
docker run --rm --env-file .env.staging myapp:test python manage.py check --deploy

# Ansible-side: dry run on a staging inventory
cd deploy && ansible-playbook playbooks/deploy.yml --check \
  --vault-password-file ~/.config/django-deploy/vault_pass

# After a real deploy: hit the endpoints
curl -sf https://app.example.com/healthz
curl -sf https://app.example.com/readyz

# Trigger a test error to verify Sentry receives it
docker compose -f compose.prod.yml exec web python -c "raise Exception('sentry-test')"
```

`python manage.py check --deploy` is Django's built-in audit for production settings (it flags missing `SECURE_*` settings, weak `SECRET_KEY`, etc.). Run it in CI too.

## Checklist

- [ ] `Dockerfile` has `prod` and `dev` targets; CI builds `--target prod`
- [ ] `compose.prod.yml` defines web / celery / celery-beat with the prod image and `restart: unless-stopped`
- [ ] `production.py` has `DEBUG=False`, `SECURE_*` settings, `STORAGES` for S3, `LOGGING` for JSON, Sentry init, separate `CACHES` and `CELERY_BROKER_URL`
- [ ] `entrypoint.sh` waits for postgres but does NOT auto-migrate (no `RUN_MIGRATIONS`)
- [ ] `gunicorn_config.py` exists with workers/threads/timeouts
- [ ] `/healthz` and `/readyz` endpoints wired in `config/urls.py`
- [ ] `deploy/inventory.yml` lists web and worker hosts
- [ ] `deploy/group_vars/all/vault.yml` encrypted with Ansible Vault, password stored outside the repo
- [ ] `deploy/group_vars/all/vars.yml` references vault values, no plaintext secrets
- [ ] `deploy/playbooks/deploy.yml` runs migrations once, restarts web hosts serially, restarts worker last
- [ ] `make deploy` and `make rollback TAG=…` targets in the Makefile
- [ ] `.gitignore` excludes the vault password file path
- [ ] `python manage.py check --deploy` passes against `production.py`
- [ ] LB health check points at `/readyz`
- [ ] Postgres/Redis network ports are not exposed to the internet
- [ ] Sentry receives a test error from the deployed environment
- [ ] First deploy logged + verified before automating subsequent deploys
