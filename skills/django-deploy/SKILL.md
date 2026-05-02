---
name: django-deploy
description: Deploy a Django project to production at mid-scale with self-hosted infrastructure — multi-stage production Dockerfile, gunicorn, health and readiness endpoints, JSON logging, an Ansible playbook structure with Vault-encrypted secrets, a rolling deploy across multiple gunicorn hosts behind a self-hosted HAProxy load balancer, separate beat-singleton + N celery worker hosts, self-hosted Postgres and Redis (broker and cache as separate instances), self-hosted GlitchTip for errors, plus pragmatic external services (S3 for static and media, AWS SES for email, Let's Encrypt for TLS). Use when setting up production for the first time, adding deployment infrastructure, or whenever the user mentions deploy, production, Ansible, gunicorn, HAProxy, staging, or prod.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Production Deployment

This skill targets a mid-scale production topology that an agent can stand up end-to-end with no GUI clicking. The infrastructure is self-hosted by Ansible; the only external services are the ones whose self-hosted alternatives have unreasonable tradeoffs (email deliverability, object storage durability) or are invisible plumbing (TLS roots).

The deployment artifact is a Docker image. The deployment mechanism is Ansible.

## Topology

```
                  HAProxy (TLS via certbot)
                  [lb-01]
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    web-01           web-02          web-N         (gunicorn)
        │               │               │
        └───────────────┼───────────────┘
                        │
              ┌─────────┼─────────┐
              ▼         ▼         ▼
        worker-beat  worker-02  worker-N           (celery)
              │         │         │
              └─────────┼─────────┘
                        │
        ┌──────────┬────┼────┬───────────┐
        ▼          ▼    ▼    ▼           ▼
       db-01  redis-broker  redis-cache  glitchtip-01
       (PG)   (AOF on)      (LRU eviction)  (errors)

External services:
  S3 + CloudFront            AWS SES                Let's Encrypt
  (static + media)           (email)                (TLS, via certbot)
```

| Role | Count | Self-hosted? | What it runs |
|---|---|---|---|
| **lb** | 1+ | ✅ HAProxy + certbot | TLS termination, forwards to web hosts |
| **web** | 2+ | ✅ gunicorn container | Django HTTP, behind LB |
| **worker_beat** | exactly 1 | ✅ celery + celery-beat | Beat is a singleton |
| **worker** | 0+ | ✅ celery worker only | Add hosts here to scale |
| **db** | 1 | ✅ Postgres container | App database, backups via pg_dump |
| **redis_broker** | 1 | ✅ Redis container, AOF on | Celery broker — durability matters |
| **redis_cache** | 1 | ✅ Redis container, no AOF | Application cache, LRU eviction |
| **glitchtip** | 1 | ✅ GlitchTip docker-compose | Sentry-compatible error tracker |
| S3 + CDN | — | external (AWS) | Static and media. CloudFront/CloudFlare for edge caching. |
| Email | — | external (AWS SES) | Outbound transactional email. Self-hosted SMTP is a deliverability dead-end. |
| TLS | — | external (Let's Encrypt) | Certs via certbot in Ansible — invisible plumbing. |

For smaller deployments, `redis_broker` and `redis_cache` can co-locate on one host as two containers on different ports. The skill keeps them split so the inventory matches the eventual scale-out.

## Step 1: Production Dockerfile

Add a production target to the existing `Dockerfile` as a second stage. The dev target stays for local; `make provision` and `make deploy` build the prod target.

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

Notes:
- **Non-root user** in prod. Containers run as `app`, never root.
- **`uv sync --no-dev`** strips dev dependencies — production image is leaner.
- **`libpq5`** is the runtime Postgres client lib for psycopg's binary build.
- **No collectstatic in the build** — static files go to S3 during deploy (Step 4).
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
      - "127.0.0.1:8000:8000"   # only the LB on the same private network reaches this
    restart: unless-stopped
    stop_grace_period: 35s     # ≥ gunicorn graceful_timeout so in-flight requests complete
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
- **No `volumes`** — the image is the source of truth. Bind mounts in production defeat immutable deploys.
- **No `postgres` / `redis` services** — those are self-hosted on dedicated hosts (Step 9). Web and celery containers connect to them over the private network via `.env.production`.
- **Web hosts** run `web` only; **`worker_beat`** runs `celery` + `celery-beat`; **`worker`** hosts run only `celery`.
- **Port bound to `127.0.0.1`** so only the LB (which connects over the private network) reaches gunicorn. Internet traffic terminates at HAProxy.
- **`IMAGE_TAG`** is set by Ansible at deploy time — typically the git SHA. Never `latest`.
- **`env_file`** points at `.env.production`, which Ansible writes from Vault-decrypted values (Step 11).

## Step 3: gunicorn config

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

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

max_requests = 1000
max_requests_jitter = 100

worker_exit_on_app_exit = True
```

Tune `GUNICORN_WORKERS` per host based on CPU and memory. The default `2 * CPU + 1` is the gunicorn docs' rule of thumb for sync-ish workloads.

## Step 4: Health and readiness endpoints

- **`/healthz`** — liveness. Process is alive, can return a response. HAProxy uses this for the backend health check.
- **`/readyz`** — readiness. Process can do its job (DB reachable). Used by the deploy play to gate the rolling restart.

`src/config/views.py`:

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

`src/config/urls.py`:

```python
from .views import healthz, readyz

urlpatterns = [
    # ...existing entries...
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
]
```

No trailing slash so the LB doesn't get redirected by `APPEND_SLASH`.

## Step 5: Production settings

`src/config/settings/production.py`:

```python
from decouple import Csv, config

from .base import *  # noqa: F401, F403

# =============================================================================
# SECURITY
# =============================================================================
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

# =============================================================================
# DATABASE — self-hosted Postgres on the `db` host
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
    }
}

# =============================================================================
# CACHE — self-hosted Redis (separate instance from the broker)
# =============================================================================
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("REDIS_CACHE_URL"),
    }
}

# =============================================================================
# CELERY — self-hosted Redis broker (separate instance from the cache)
# =============================================================================
CELERY_BROKER_URL = config("CELERY_BROKER_URL")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=None)

# =============================================================================
# STORAGES — static + media on S3 (external)
# =============================================================================
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

# =============================================================================
# EMAIL — AWS SES (external; transactional patterns live in django-email)
# =============================================================================
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST")            # e.g. email-smtp.us-east-1.amazonaws.com
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL")

# =============================================================================
# LOGGING — JSON to stdout, captured by Docker
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
# ERROR TRACKING — self-hosted GlitchTip (Sentry-compatible)
# =============================================================================
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.redis import RedisIntegration

sentry_sdk.init(
    dsn=config("GLITCHTIP_DSN"),                       # https://glitchtip.example.com/...
    environment=config("DEPLOY_ENVIRONMENT", default="production"),
    release=config("RELEASE_VERSION", default=None),   # set at deploy time
    integrations=[
        DjangoIntegration(),
        CeleryIntegration(),
        RedisIntegration(),
    ],
    traces_sample_rate=config("SENTRY_TRACES_SAMPLE_RATE", default=0.1, cast=float),
    send_default_pii=False,
)
```

Add the production-only dependencies:

```bash
docker compose exec web uv add 'django-storages[s3]>=1.14' 'sentry-sdk[django]>=2.0' python-json-logger
```

The `sentry-sdk` package works against GlitchTip unchanged because GlitchTip implements the Sentry ingest API.

## Step 6: Static and media on S3 (external)

`STORAGES` config is in Step 5. Two operational details:

- **`collectstatic` runs at deploy time, not build time.** Build-time would require AWS credentials in the build pipeline and re-run on every image build. The deploy play (Step 13) runs `python manage.py collectstatic --noinput` on one host as a one-shot — it uploads only changed files thanks to `S3Storage`'s checksum check.
- **CDN** — put CloudFront (or CloudFlare) in front of the bucket. Set `AWS_S3_CUSTOM_DOMAIN=cdn.example.com`. URLs in templates resolve via the CDN.

Bucket creation, IAM users, and policies are out of scope for this skill — they're a one-time AWS-side task. The `vault.yml` only stores the resulting access keys.

## Step 7: Email via AWS SES (external)

Settings from Step 5 already configure SMTP against SES. Two more concerns:

- **Domain verification + DKIM/SPF** is a one-time DNS step on the SES side, AWS-side. Deliverability suffers without it.
- **Transactional email patterns** (templates, sending via Celery, bounce handling) live in the `django-email` skill — not here. The deploy skill only configures the connection.

## Step 8: Self-hosted infrastructure (Ansible-managed)

This is the section that flips with managed services. Each component runs in Docker on a dedicated host and is configured by Ansible roles in `deploy/roles/`.

### 8a. HAProxy + Let's Encrypt — the `lb` host

HAProxy terminates TLS and forwards to the web hosts. Certificates come from Let's Encrypt via certbot, automatically renewed.

`deploy/roles/haproxy/templates/haproxy.cfg.j2` (key parts):

```
global
    log stdout format raw local0
    maxconn 4096
    # Admin socket used by the deploy play to drain hosts before container swap
    stats socket /var/run/haproxy/admin.sock mode 660 level admin expose-fd listeners

defaults
    log     global
    mode    http
    option  httplog
    option  forwardfor
    timeout connect 5s
    timeout client  60s
    timeout server  60s

frontend http_front
    bind *:80
    http-request redirect scheme https code 301 unless { ssl_fc }

frontend https_front
    bind *:443 ssl crt /etc/letsencrypt/live/{{ lb_domain }}/haproxy.pem alpn h2,http/1.1
    http-request set-header X-Forwarded-Proto https
    default_backend django_web

backend django_web
    option httpchk GET /healthz
    http-check expect status 200
    balance roundrobin
{% for host in groups['web'] %}
    server {{ host.split('.')[0] }} {{ hostvars[host].ansible_host | default(host) }}:8000 check
{% endfor %}
```

The admin socket lets Ansible mark a server as "draining" before the container swap. The `server` directive uses the short hostname (`web-01`) so the deploy play can refer to it without the full FQDN.

Install `socat` on the LB host (one-line role task) so Ansible can talk to the admin socket via a single shell command.

Renewal hook concatenates the cert+key into HAProxy's combined PEM format:

```
# /etc/letsencrypt/renewal-hooks/deploy/haproxy.sh
cat /etc/letsencrypt/live/{{ lb_domain }}/fullchain.pem \
    /etc/letsencrypt/live/{{ lb_domain }}/privkey.pem \
    > /etc/letsencrypt/live/{{ lb_domain }}/haproxy.pem
chmod 600 /etc/letsencrypt/live/{{ lb_domain }}/haproxy.pem
docker exec haproxy kill -USR2 1   # graceful reload
```

The role:
1. Installs Docker (use `geerlingguy.docker`)
2. Issues the cert via `certbot certonly --standalone` on first run
3. Sets up the systemd timer for auto-renewal
4. Renders `haproxy.cfg` from the inventory's `web` group
5. Runs the haproxy container with `--network host`

For LB high-availability, run a second `lb` host with `keepalived` for a floating IP. Out of scope for v1.

### 8b. Postgres — the `db` host

A single Postgres container with a named volume for data and a backup cron. `geerlingguy.postgresql` handles the bare-metal install if you prefer; the Docker route below is consistent with the rest of the stack.

`deploy/roles/postgres/templates/compose.postgres.yml.j2`:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    volumes:
      - /srv/postgres/data:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: "{{ postgres_db }}"
      POSTGRES_USER: "{{ postgres_user }}"
      POSTGRES_PASSWORD: "{{ postgres_password }}"
    ports:
      - "{{ private_ip }}:5432:5432"   # bind to private interface only
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U {{ postgres_user }}"]
      interval: 10s
      timeout: 5s
      retries: 5
    shm_size: 256m
```

The role:
1. Installs Docker on `db-01`
2. Renders the compose file with the private-network IP
3. Creates `/srv/postgres/data` with the right permissions
4. Brings up the container with `docker compose up -d`
5. Installs the backup script (Step 14)
6. Configures the firewall to allow Postgres traffic only from the `web` and `worker*` host IPs

This is a single-host Postgres — a planned single point of failure for the v1 topology. Streaming replication via `repmgr` or a managed standby is a follow-on (`django-postgres-ha`, future skill).

#### pgbouncer — connection pooling on the `db` host

Django opens one Postgres connection per gunicorn worker. With `workers = 2 * CPU + 1` and a few CPUs across N web hosts, you're easily into hundreds of concurrent connections — Postgres handles this poorly (each connection is a forked process with its own memory). pgbouncer pools incoming connections so Postgres only sees a small fixed number of backend connections, regardless of how many workers your fleet has.

Add a pgbouncer container next to Postgres on the `db` host:

`deploy/roles/postgres/templates/compose.pgbouncer.yml.j2`:

```yaml
services:
  pgbouncer:
    image: edoburu/pgbouncer:latest
    restart: unless-stopped
    environment:
      DB_HOST: postgres
      DB_PORT: "5432"
      DB_USER: "{{ postgres_user }}"
      DB_PASSWORD: "{{ postgres_password }}"
      DB_NAME: "{{ postgres_db }}"
      POOL_MODE: transaction
      MAX_CLIENT_CONN: "1000"
      DEFAULT_POOL_SIZE: "20"
      AUTH_TYPE: scram-sha-256
    ports:
      - "{{ private_ip }}:6432:5432"
    depends_on:
      postgres:
        condition: service_healthy
    networks:
      - default
```

Run pgbouncer in the same Docker network as Postgres so it reaches `postgres:5432` directly. The published port `6432` is the one web/worker hosts connect to.

Update `.env.production` so the app talks to pgbouncer instead of Postgres directly:

```
POSTGRES_HOST=db-01.internal
POSTGRES_PORT=6432         # pgbouncer, not 5432
```

And update the production settings — **drop `CONN_MAX_AGE`** when using pgbouncer in transaction mode:

```python
# src/config/settings/production.py
DATABASES = {
    "default": {
        # ...
        "CONN_MAX_AGE": 0,    # let pgbouncer pool, not Django
        "OPTIONS": {
            "sslmode": config("POSTGRES_SSLMODE", default="require"),
            # transaction-mode pgbouncer can't hold prepared statements across requests
            "options": "-c default_transaction_isolation=read_committed",
        },
    }
}

# Disable Django's prepared-statement cache for pgbouncer transaction mode
# (see "Caveats" below)
```

##### Pool modes — choose `transaction`

| Mode | Behavior | Suitable? |
|---|---|---|
| `session` | One backend connection per client connection until the client disconnects | No — defeats pooling at scale |
| `transaction` | Backend connection assigned per transaction, returned to pool on commit/rollback | ✅ Yes |
| `statement` | Backend connection per statement | No — breaks multi-statement transactions |

##### Caveats with `pool_mode = transaction`

Transaction-mode pgbouncer hands out a different backend connection per transaction, so anything that depends on session-level state across statements stops working:

- **Prepared statements** — break across requests. Disable Django's prepared-statement cache: set `DISABLE_SERVER_SIDE_CURSORS = True` and avoid `psycopg`'s prepared-statement caching. With Django 4.2+ on psycopg 3, prepared statements are off by default — but verify with `EXPLAIN` that you're not seeing `_pgbouncer_*` prepared statement names in your slow query logs.
- **`SET LOCAL` / `SET ROLE`** — only persist for the transaction. `SET` (without `LOCAL`) leaks across transactions and breaks isolation. Avoid both unless you know what you're doing.
- **`LISTEN` / `NOTIFY`** — don't work. Use Celery instead.
- **Server-side cursors** — broken. Setting `DISABLE_SERVER_SIDE_CURSORS = True` is required.
- **Advisory locks** — only safe inside a transaction. Don't try to hold one across requests.
- **`CONN_MAX_AGE > 0`** — pointless. Django would hold a connection open across requests, but the pgbouncer side doesn't preserve session state, so the optimization doesn't help and can mask issues.

##### Why this is worth the constraints

A web fleet of 4 hosts × 9 workers = 36 worker processes. With direct Postgres each process holds a connection (36 connections, often idle). With pgbouncer in transaction mode and `DEFAULT_POOL_SIZE: 20`, Postgres sees at most 20 active backends regardless of worker count. Connections become a flow concept, not a static allocation.

The bookkeeping (no LISTEN/NOTIFY, careful prepared statements) is easy compared to running out of `max_connections` in production.

### 8c. Redis — the `redis_broker` and `redis_cache` hosts

Two Redis instances with different durability profiles. Same role, different vars.

`deploy/roles/redis/templates/compose.redis.yml.j2`:

```yaml
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: >
      redis-server
      {{ '--appendonly yes' if redis_persist else '--appendonly no' }}
      {{ '--maxmemory ' + redis_maxmemory if redis_maxmemory else '' }}
      {{ '--maxmemory-policy ' + redis_maxmemory_policy if redis_maxmemory_policy else '' }}
      --requirepass {{ redis_password }}
    volumes:
      - /srv/redis/data:/data
    ports:
      - "{{ private_ip }}:6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "{{ redis_password }}", "ping"]
      interval: 10s
```

`deploy/group_vars/redis_broker.yml`:

```yaml
redis_persist: true             # AOF on — in-flight tasks survive a crash
redis_maxmemory: ""             # let it grow
redis_maxmemory_policy: ""
```

`deploy/group_vars/redis_cache.yml`:

```yaml
redis_persist: false            # cache is rebuildable; disk writes are wasted I/O
redis_maxmemory: "1gb"
redis_maxmemory_policy: "allkeys-lru"
```

Both share a Vault-encrypted `redis_password`. The Django side connects via `rediss://:{password}@redis-broker.internal:6379/0` (TLS) or `redis://...` if the private network is trusted.

### 8d. GlitchTip — the `glitchtip` host

GlitchTip publishes an official docker-compose. The role drops it on the host and brings it up with vault-supplied secrets.

`deploy/roles/glitchtip/templates/compose.glitchtip.yml.j2` (boilerplate based on their docs — keep the upstream as the source of truth):

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_HOST_AUTH_METHOD: "trust"   # private network only
    volumes:
      - /srv/glitchtip/postgres:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine

  web:
    image: glitchtip/glitchtip:latest
    depends_on: [postgres, redis]
    ports:
      - "{{ private_ip }}:8000:8000"
    environment:
      DATABASE_URL: postgres://postgres:postgres@postgres:5432/postgres
      SECRET_KEY: "{{ glitchtip_secret_key }}"
      EMAIL_URL: "{{ glitchtip_email_url }}"
      GLITCHTIP_DOMAIN: "https://glitchtip.{{ root_domain }}"
      DEFAULT_FROM_EMAIL: "{{ default_from_email }}"
      CELERY_WORKER_AUTOSCALE: "1,3"

  worker:
    image: glitchtip/glitchtip:latest
    command: celery -A glitchtip worker
    depends_on: [postgres, redis]
    environment:  # same as web

  migrate:
    image: glitchtip/glitchtip:latest
    command: ./bin/run-migrate.sh
    depends_on: [postgres]
    environment: # same as web
    restart: "no"
```

HAProxy fronts the glitchtip host on `glitchtip.<domain>` with its own cert. The Django side sets `GLITCHTIP_DSN=https://<key>@glitchtip.<domain>/<project_id>` — sentry-sdk talks to it as if it were Sentry.

GlitchTip's upstream docs are the source of truth for the compose file — pin a version, don't track `:latest` in production.

## Step 9: Ansible structure

The deploy lives in a `deploy/` directory at the repo root.

```
deploy/
  ansible.cfg
  inventory.yml
  group_vars/
    all/
      vars.yml          # plain config (image registry, internal hostnames, etc.)
      vault.yml         # encrypted secrets (Vault)
    redis_broker.yml
    redis_cache.yml
  playbooks/
    provision.yml       # one-time + on infra changes — sets up infra hosts
    deploy.yml          # per-release — updates app hosts
    rollback.yml        # rolls back to a prior image tag
  roles/
    docker/             # install Docker (or use geerlingguy.docker)
    haproxy/
    postgres/
    redis/              # parameterized — used for both broker and cache
    glitchtip/
    write_env/          # render .env.production from group_vars + vault
    pull_image/
    migrate/
    collectstatic/
    restart_web/
    restart_worker/
  templates/
    env.production.j2
```

`deploy/inventory.yml`:

```yaml
all:
  children:
    lb:
      hosts:
        lb-01.internal:
    web:
      hosts:
        web-01.internal:
        web-02.internal:
    worker_beat:
      hosts:
        worker-beat-01.internal:
    worker:
      hosts:
        worker-02.internal:
        worker-03.internal:
    db:
      hosts:
        db-01.internal:
    redis_broker:
      hosts:
        redis-broker-01.internal:
    redis_cache:
      hosts:
        redis-cache-01.internal:
    glitchtip:
      hosts:
        glitchtip-01.internal:
  vars:
    ansible_user: deploy
    ansible_python_interpreter: /usr/bin/python3
```

Scaling Celery is a one-line inventory change: add a host under `worker:`, run `make deploy`. Scaling web is the same with one extra step — add to `web:` and re-render the HAProxy config (`make provision --tags=haproxy`).

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

## Step 10: Ansible Vault for secrets

```bash
# Generate a vault password and store it OUTSIDE the repo
mkdir -p ~/.config/django-deploy
openssl rand -base64 32 > ~/.config/django-deploy/vault_pass
chmod 400 ~/.config/django-deploy/vault_pass

# Create the vault file
ansible-vault create deploy/group_vars/all/vault.yml \
  --vault-password-file ~/.config/django-deploy/vault_pass
```

`deploy/group_vars/all/vault.yml` (decrypted view):

```yaml
vault_secret_key: "django-secret-key-here"
vault_postgres_password: "..."
vault_redis_password: "..."
vault_glitchtip_secret_key: "..."
vault_glitchtip_dsn: "https://abc@glitchtip.example.com/1"
vault_aws_access_key_id: "AKIA..."
vault_aws_secret_access_key: "..."
vault_email_host_user: "AKIA-SES..."
vault_email_host_password: "..."
vault_registry_user: "..."
vault_registry_password: "..."
```

`deploy/group_vars/all/vars.yml` (committed, plain — references vault values):

```yaml
image_registry: "registry.example.com"
image_name: "myapp"
root_domain: "example.com"
lb_domain: "app.example.com"
allowed_hosts: "app.example.com,api.example.com"
csrf_trusted_origins: "https://app.example.com"

postgres_db: "app"
postgres_user: "app"
postgres_host: "db-01.internal"
postgres_password: "{{ vault_postgres_password }}"

redis_password: "{{ vault_redis_password }}"
celery_broker_url: "redis://:{{ vault_redis_password }}@redis-broker-01.internal:6379/0"
redis_cache_url: "redis://:{{ vault_redis_password }}@redis-cache-01.internal:6379/0"

aws_storage_bucket_name: "myapp-static-prod"
aws_s3_region_name: "us-east-1"
aws_s3_custom_domain: "cdn.example.com"
aws_access_key_id: "{{ vault_aws_access_key_id }}"
aws_secret_access_key: "{{ vault_aws_secret_access_key }}"

email_host: "email-smtp.us-east-1.amazonaws.com"
email_host_user: "{{ vault_email_host_user }}"
email_host_password: "{{ vault_email_host_password }}"
default_from_email: "noreply@example.com"

glitchtip_dsn: "{{ vault_glitchtip_dsn }}"
glitchtip_secret_key: "{{ vault_glitchtip_secret_key }}"

secret_key: "{{ vault_secret_key }}"
```

Edit later with `ansible-vault edit`. Rotate keys with `ansible-vault rekey`. **Never** commit the vault password file — `.gitignore` it explicitly.

## Step 11: Provisioning playbook

Provisioning is the one-time bootstrap (and the path for adding/replacing infrastructure hosts). It runs the role for each infra host group.

`deploy/playbooks/provision.yml`:

```yaml
- name: Install Docker on every host
  hosts: all
  become: true
  roles:
    - docker

- name: Configure HAProxy + Let's Encrypt
  hosts: lb
  become: true
  roles:
    - haproxy

- name: Configure Postgres
  hosts: db
  become: true
  roles:
    - postgres

- name: Configure Redis (broker)
  hosts: redis_broker
  become: true
  roles:
    - role: redis
      vars:
        redis_persist: true

- name: Configure Redis (cache)
  hosts: redis_cache
  become: true
  roles:
    - role: redis
      vars:
        redis_persist: false
        redis_maxmemory: "1gb"
        redis_maxmemory_policy: "allkeys-lru"

- name: Configure GlitchTip
  hosts: glitchtip
  become: true
  roles:
    - glitchtip
```

Run with `make provision`. Idempotent — subsequent runs only change what's drifted or new.

## Step 12: Deploy playbook

```yaml
- name: Resolve image tag
  hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.set_fact:
        image_tag: "{{ lookup('env', 'IMAGE_TAG') | default(lookup('pipe', 'git rev-parse --short HEAD'), true) }}"

- name: Push the env file to every app host
  hosts: web:worker_beat:worker
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

- name: Run migrations on one host (any web host)
  hosts: web[0]
  become: true
  vars:
    image_tag: "{{ hostvars['localhost'].image_tag }}"
  tasks:
    - name: migrate
      ansible.builtin.command:
        cmd: >
          docker compose -f /opt/app/compose.prod.yml run --rm
          -e IMAGE_TAG={{ image_tag }} -e IMAGE_REGISTRY={{ image_registry }} -e IMAGE_NAME={{ image_name }}
          web python manage.py migrate --noinput
        chdir: /opt/app

    - name: collectstatic
      ansible.builtin.command:
        cmd: >
          docker compose -f /opt/app/compose.prod.yml run --rm
          -e IMAGE_TAG={{ image_tag }} -e IMAGE_REGISTRY={{ image_registry }} -e IMAGE_NAME={{ image_name }}
          web python manage.py collectstatic --noinput
        chdir: /opt/app

- name: Rolling restart of web hosts (zero-downtime)
  hosts: web
  serial: 1
  become: true
  vars:
    image_tag: "{{ hostvars['localhost'].image_tag }}"
    short_name: "{{ inventory_hostname.split('.')[0] }}"
  tasks:
    - name: Drain — tell HAProxy to stop sending new traffic to this host
      ansible.builtin.shell:
        cmd: |
          echo "set server django_web/{{ short_name }} state drain" \
            | socat stdio unix-connect:/var/run/haproxy/admin.sock
      delegate_to: "{{ groups['lb'][0] }}"
      become: true

    - name: Wait for in-flight requests to complete
      ansible.builtin.pause:
        seconds: 30

    - name: Restart web with new image
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

    - name: Resume — tell HAProxy to send traffic to this host again
      ansible.builtin.shell:
        cmd: |
          echo "set server django_web/{{ short_name }} state ready" \
            | socat stdio unix-connect:/var/run/haproxy/admin.sock
      delegate_to: "{{ groups['lb'][0] }}"
      become: true

  rescue:
    # Always resume routing if anything failed mid-deploy — better to take
    # traffic on the old container than leave the host marked drain forever.
    - name: Resume HAProxy routing on failure
      ansible.builtin.shell:
        cmd: |
          echo "set server django_web/{{ short_name }} state ready" \
            | socat stdio unix-connect:/var/run/haproxy/admin.sock
      delegate_to: "{{ groups['lb'][0] }}"
      become: true

    - ansible.builtin.fail:
        msg: "Rolling restart failed on {{ inventory_hostname }}"

- name: Restart the beat host (worker + beat — singleton)
  hosts: worker_beat
  become: true
  vars:
    image_tag: "{{ hostvars['localhost'].image_tag }}"
  tasks:
    - ansible.builtin.command:
        cmd: docker compose -f /opt/app/compose.prod.yml up -d celery celery-beat
        chdir: /opt/app
      environment:
        IMAGE_TAG: "{{ image_tag }}"
        IMAGE_REGISTRY: "{{ image_registry }}"
        IMAGE_NAME: "{{ image_name }}"

- name: Restart additional worker hosts (worker only)
  hosts: worker
  become: true
  vars:
    image_tag: "{{ hostvars['localhost'].image_tag }}"
  tasks:
    - ansible.builtin.command:
        cmd: docker compose -f /opt/app/compose.prod.yml up -d celery
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
DEPLOY_ENVIRONMENT=production

POSTGRES_DB={{ postgres_db }}
POSTGRES_USER={{ postgres_user }}
POSTGRES_PASSWORD={{ postgres_password }}
POSTGRES_HOST={{ postgres_host }}
POSTGRES_PORT=5432

CELERY_BROKER_URL={{ celery_broker_url }}
REDIS_CACHE_URL={{ redis_cache_url }}

AWS_STORAGE_BUCKET_NAME={{ aws_storage_bucket_name }}
AWS_S3_REGION_NAME={{ aws_s3_region_name }}
AWS_S3_CUSTOM_DOMAIN={{ aws_s3_custom_domain }}
AWS_ACCESS_KEY_ID={{ aws_access_key_id }}
AWS_SECRET_ACCESS_KEY={{ aws_secret_access_key }}

EMAIL_HOST={{ email_host }}
EMAIL_PORT=587
EMAIL_HOST_USER={{ email_host_user }}
EMAIL_HOST_PASSWORD={{ email_host_password }}
DEFAULT_FROM_EMAIL={{ default_from_email }}

GLITCHTIP_DSN={{ glitchtip_dsn }}
```

Rolling deploy semantics:

1. Migrations run **before** any web container restarts. New code starts on a schema that's already migrated.
2. `serial: 1` on the web play restarts one host at a time, with explicit drain via the HAProxy admin socket: mark drain → wait 30s for in-flight requests to complete → restart container → wait for `/readyz` → mark ready. No requests dropped.
3. `worker_beat` restarts before the additional `worker` hosts so beat is briefly the only Celery process during cutover.
4. Worker hosts pick up new code without losing in-flight tasks (graceful shutdown drains the queue).
5. The `rescue` block on the web play guarantees the host returns to "ready" in HAProxy even if anything mid-restart fails — better to serve traffic on the old container than leave a host stuck in drain forever.

## Zero-downtime — what makes it actually zero

The rolling deploy is zero-downtime in the strict sense (no dropped requests) because three pieces coordinate:

**1. HAProxy drain via admin socket.** Without this, HAProxy keeps routing to the host until `/healthz` fails — those failed checks ARE dropped requests. The drain command tells HAProxy "finish current connections, send no new ones," and the deploy waits 30s before touching the container. After the new container is healthy, the play marks the host `ready` again.

**2. gunicorn graceful shutdown.** Docker sends SIGTERM when the container is stopped; gunicorn (with `graceful_timeout = 30` from Step 3) stops accepting new requests, lets in-flight ones complete, then exits. Compose's `stop_grace_period: 35s` from Step 2 gives gunicorn enough time before SIGKILL. Without this, in-flight requests get killed mid-response.

**3. Backwards-compatible migrations.** The migration runs at the start of the deploy, so during the rolling restart, OLD code is running against the NEW schema. Only some migration types are safe in this window:

| Migration | Safe during rolling deploy? |
|---|---|
| Add a nullable column or one with a default | ✅ |
| Add a table | ✅ |
| Add a non-unique index | ✅ (use `CREATE INDEX CONCURRENTLY` for large tables) |
| Add a unique constraint | ⚠️ may reject inserts that old code allowed |
| Drop a column | ❌ old code may still reference it |
| Rename a column | ❌ old code references the old name |
| Change a column type | ❌ old code may not handle the new type |
| Add a NOT NULL column without a default | ❌ old code's INSERTs lack the column |

For unsafe migrations, two options:

- **Expand-contract** — covered in the future `django-migrations-scale` skill. Multi-deploy dance: add new column alongside old, ship code that writes both, backfill, ship code that reads new, drop old.
- **Maintenance window** — accept downtime. Take the LB out of rotation (mark all backends `maint`), run the migration, restart everything, mark `ready`. Use only when expand-contract isn't worth it (low-stakes admin tools, internal apps with off-hours windows).

Most migrations are additive and fall into the safe column. The discipline is to recognize which is which before deploying.

## Step 13: Backups

A backup that's never restored isn't a backup. The Postgres role installs:

1. **`pg_dump` cron** — daily compressed dump to `/srv/postgres/backups/`.
2. **`rclone` push** to off-host storage (S3, B2, etc.) — uses the same AWS credentials as the app.
3. **Retention policy** — keep daily for 14 days, weekly for 8 weeks.
4. **Periodic restore drill** — a documented runbook (not in this skill body) that pulls a backup, restores it to a staging host, and runs `python manage.py check` against it. Aim quarterly.

`deploy/roles/postgres/templates/backup.sh.j2`:

```bash
#!/usr/bin/env bash
set -euo pipefail

DATE=$(date +%F)
DUMP=/srv/postgres/backups/${DATE}.sql.gz

docker exec postgres pg_dump -U {{ postgres_user }} -d {{ postgres_db }} \
  | gzip -9 > "${DUMP}"

# Push to off-host storage
rclone copy "${DUMP}" "s3:{{ backup_bucket }}/postgres/" --quiet

# Local retention: 14 days
find /srv/postgres/backups/ -type f -mtime +14 -delete
```

Cron: `0 3 * * *` (daily, 3 AM in the host's timezone).

Restore is the inverse: `pg_restore` from a dump pulled via `rclone`. Document and rehearse it.

## Step 14: Makefile

```makefile
.PHONY: provision deploy rollback

provision: ## Bootstrap or update infrastructure hosts (idempotent)
	cd deploy && ansible-playbook playbooks/provision.yml \
		--vault-password-file ~/.config/django-deploy/vault_pass

deploy: ## Deploy the current commit to production
	cd deploy && ansible-playbook playbooks/deploy.yml \
		--vault-password-file ~/.config/django-deploy/vault_pass

rollback: ## Roll back to a prior image tag — pass TAG=<sha>
	@test -n "$(TAG)" || (echo "TAG=<sha> required" && exit 1)
	cd deploy && IMAGE_TAG=$(TAG) ansible-playbook playbooks/deploy.yml \
		--vault-password-file ~/.config/django-deploy/vault_pass
```

Run from the repo root.

## Common Mistakes

- **Running `runserver` in production.** Use gunicorn. `runserver` is single-threaded, prints debug stack traces, and is explicitly not for production.
- **Bind-mounting source code in `compose.prod.yml`.** Defeats the immutable-image guarantee.
- **Missing `SECURE_PROXY_SSL_HEADER`.** Django thinks every request is HTTP, secure cookies fail, redirects loop.
- **Pinning to `:latest`.** Reproducibility goes out the window. Always deploy a specific git SHA.
- **Forgetting `collectstatic`.** Static files 404. The deploy play handles it.
- **Running migrations from every web host.** Race conditions. The play runs migrate exactly once on `web[0]`.
- **Running `celery-beat` on more than one host.** Every periodic task gets enqueued multiple times. The `worker_beat` / `worker` split exists for this reason.
- **Letting Postgres or Redis face the internet.** They listen on the host's private IP only and the firewall blocks the public interface.
- **`.env.production` committed to git.** Use Ansible Vault. The on-host file is generated by Ansible, never tracked.
- **Vault password file inside the repo.** It belongs in `~/.config/django-deploy/`, never tracked.
- **Backups never restored.** A backup is theoretical until you've done a restore drill. Schedule one quarterly minimum.
- **Skipping the HAProxy drain step on rolling restart.** Without it, the LB keeps routing to a host while you swap its container — dropped requests on every deploy. The `socat` drain dance is what makes the deploy actually zero-downtime.
- **`stop_grace_period` shorter than gunicorn's `graceful_timeout`.** Docker SIGKILLs in-flight requests before gunicorn finishes draining. Always set compose's `stop_grace_period` slightly higher than `graceful_timeout`.
- **Shipping a destructive migration with a rolling deploy.** During the rolling restart, OLD code runs against the NEW schema. Drop-column, rename-column, change-type — these crash old code mid-deploy. Use expand-contract or a maintenance window.
- **`POSTGRES_HOST_AUTH_METHOD: trust` on the app database.** That's only safe in GlitchTip's *internal* compose where the DB isn't exposed. The app's Postgres needs `scram-sha-256` and a strong `POSTGRES_PASSWORD`.
- **Using `:latest` on GlitchTip.** Pin a version. The upstream project ships breaking changes.
- **Skipping the SES domain verification step.** Email lands in spam folders or doesn't deliver at all.

## Verify

```bash
# Build the prod image locally
docker build --target prod -t myapp:test .

# Smoke-test it
docker run --rm --env-file .env.staging myapp:test python manage.py check --deploy

# Ansible dry runs
cd deploy && ansible-playbook playbooks/provision.yml --check \
  --vault-password-file ~/.config/django-deploy/vault_pass

cd deploy && ansible-playbook playbooks/deploy.yml --check \
  --vault-password-file ~/.config/django-deploy/vault_pass

# After a real deploy
curl -sf https://app.example.com/healthz
curl -sf https://app.example.com/readyz

# Trigger a test error and confirm GlitchTip received it
docker compose -f compose.prod.yml exec web python -c "raise Exception('glitchtip-test')"

# Verify a backup
ls -la /srv/postgres/backups/   # on db-01
rclone ls s3:{{ backup_bucket }}/postgres/

# Restore drill (on a staging host)
gunzip -c /srv/postgres/backups/$(date +%F).sql.gz | docker exec -i postgres-staging psql -U app -d app
```

`python manage.py check --deploy` is Django's built-in audit for production settings — flags missing `SECURE_*`, weak `SECRET_KEY`, etc. Run it in CI too.

## Checklist

### Image and settings
- [ ] `Dockerfile` has `prod` and `dev` targets; CI builds `--target prod`
- [ ] `compose.prod.yml` defines web / celery / celery-beat with the prod image and `restart: unless-stopped`
- [ ] `production.py` has `DEBUG=False`, `SECURE_*` settings, `STORAGES` for S3, `LOGGING` for JSON, GlitchTip init via sentry-sdk, separate `CACHES` and `CELERY_BROKER_URL`, SES email config
- [ ] `entrypoint.sh` waits for postgres but does NOT auto-migrate (no `RUN_MIGRATIONS`)
- [ ] `gunicorn_config.py` exists with workers/threads/timeouts
- [ ] `/healthz` and `/readyz` endpoints wired in `config/urls.py`

### Inventory and infrastructure
- [ ] `deploy/inventory.yml` has groups: `lb`, `web`, `worker_beat`, `worker`, `db`, `redis_broker`, `redis_cache`, `glitchtip`
- [ ] **Exactly one** host in `worker_beat`
- [ ] HAProxy installed and configured on `lb`, with Let's Encrypt cert and auto-renew hook
- [ ] HAProxy admin socket enabled at `/var/run/haproxy/admin.sock`; `socat` installed on the LB host
- [ ] `compose.prod.yml`'s `web` service has `stop_grace_period` ≥ gunicorn's `graceful_timeout`
- [ ] Postgres running on `db`, listening on the private IP only, firewall blocks public
- [ ] pgbouncer container alongside Postgres on `db`, mode `transaction`, app connects to port 6432
- [ ] `CONN_MAX_AGE = 0` in production settings (pgbouncer pools, not Django)
- [ ] Two Redis instances: broker (AOF on), cache (LRU eviction, no AOF)
- [ ] GlitchTip running on `glitchtip`, fronted by HAProxy on its own subdomain
- [ ] All host-to-host traffic over the private network

### Secrets and config
- [ ] `deploy/group_vars/all/vault.yml` encrypted with Ansible Vault
- [ ] Vault password file at `~/.config/django-deploy/vault_pass`, NOT in the repo
- [ ] `deploy/group_vars/all/vars.yml` references vault values, no plaintext secrets
- [ ] `.env.production` is generated by Ansible on each app host, NOT committed

### Deploy and operations
- [ ] `deploy/playbooks/provision.yml` configures every infra host group
- [ ] `deploy/playbooks/deploy.yml` runs migrations once, restarts web hosts serially with HAProxy drain → swap → `/readyz` → resume, restarts `worker_beat` then additional `worker` hosts
- [ ] Web play has a `rescue` block that resumes HAProxy routing on failure (no host stuck in drain)
- [ ] Migration is verified backwards-compatible before rolling deploy; destructive changes go through expand-contract or a maintenance window
- [ ] `make provision`, `make deploy`, `make rollback TAG=…` all in the project Makefile
- [ ] `python manage.py check --deploy` passes
- [ ] First deploy logged + verified before automating subsequent deploys
- [ ] Test error appears in GlitchTip
- [ ] Postgres backup cron running and pushing to off-host storage
- [ ] Restore drill documented and scheduled (quarterly minimum)
- [ ] AWS SES domain verified (DKIM/SPF DNS records in place); test email delivers to inbox, not spam
