---
name: django-ci
description: Set up GitHub Actions CI for the Django project — lint, format check, type check, and tests against a real Postgres + Redis service container, plus a build-and-push job that publishes the production Docker image to GHCR on main. No auto-deploy (Ansible runs from a developer's machine via `make deploy`). Use when adding CI to a new project, switching CI providers, or whenever the user mentions GitHub Actions, CI, pipeline, or build.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Continuous Integration

CI runs on every pull request and every push to `master`. Two jobs:

1. **`check`** — lint, format check, type check, and tests. Tests run against a real Postgres and a real Redis (per **django-pytest** — no internal mocks).
2. **`build`** — only on `master`. Builds the production Docker image (multi-stage `prod` target from **django-deploy**) and pushes to a registry with the git SHA as the tag. Deployment is NOT automated — Ansible runs from a developer's machine via `make deploy IMAGE_TAG=<sha>`.

The split keeps PR feedback fast (no image build for PRs that don't merge) and keeps the release artifact reproducible (a SHA-tagged image, no rolling `:latest` in production).

## Step 1: `.github/workflows/ci.yml`

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [master]

jobs:
  check:
    name: Lint, type-check, and test
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: app
          POSTGRES_USER: app
          POSTGRES_PASSWORD: app
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U app"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 5

      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 5

    env:
      DJANGO_SETTINGS_MODULE: config.settings.local
      SECRET_KEY: ci-secret-not-used-in-prod
      POSTGRES_HOST: localhost
      POSTGRES_DB: app
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      CELERY_BROKER_URL: redis://localhost:6379/1
      REDIS_CACHE_URL: redis://localhost:6379/0

    steps:
      - uses: actions/checkout@v4

      - name: Set up uv
        uses: astral-sh/setup-uv@v4
        with:
          enable-cache: true
          cache-dependency-glob: uv.lock

      - name: Install Python
        run: uv python install

      - name: Install dependencies
        run: uv sync --frozen

      - name: Lint (ruff check)
        run: uv run ruff check src

      - name: Format check (ruff format --check)
        run: uv run ruff format --check src

      - name: Type check (pyrefly)
        run: uv run pyrefly check src

      - name: Run tests
        run: uv run pytest

      - name: OpenAPI schema validates
        run: uv run python src/manage.py spectacular --validate

  build:
    name: Build and push production image
    runs-on: ubuntu-latest
    needs: check
    if: github.event_name == 'push' && github.ref == 'refs/heads/master'

    permissions:
      contents: read
      packages: write

    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          target: prod
          push: true
          tags: |
            ghcr.io/${{ github.repository }}:${{ github.sha }}
            ghcr.io/${{ github.repository }}:sha-${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          provenance: false
```

Notes:

- **Why services and not the dev compose stack?** GitHub Actions services are the Postgres/Redis equivalents the workflow connects to directly via `localhost:5432` / `localhost:6379`. Faster than building the dev image just to run tests, and identical in behavior (the tests still hit a real Postgres).
- **`uv sync --frozen`** mirrors what production does; the lock file is the single source of truth for what's installed.
- **`--target prod`** builds the production stage of the multi-stage `Dockerfile` — slim image with no dev deps. The dev target stays out of CI.
- **Dual tag** — both `${{ github.sha }}` and `sha-${{ github.sha }}` (the latter avoids collisions with branch tags if you ever add semver later). Never tag `latest` from CI; production deploys reference an explicit SHA.
- **`provenance: false`** is for now — GHCR provenance attestations are still in flux. Re-enable when stable.
- **Cache** uses GHA's built-in cache backend for buildx; the second build of an unchanged dependency layer is near-instant.

## Step 2: How `make deploy` consumes the SHA

The deploy is decoupled from CI on purpose. Once CI builds and pushes `ghcr.io/<repo>:<sha>`, you deploy that image manually:

```bash
make deploy IMAGE_TAG=<sha>
```

If `IMAGE_TAG` is unset, the deploy play falls back to `git rev-parse --short HEAD` — useful for ad-hoc deploys from a clean working tree, but explicit SHA is the production path. See **django-deploy** for the full play.

## Step 3: Branch protection (one-time setup)

In the GitHub repo settings:

1. Settings → Branches → Add branch protection rule for `master`.
2. Require status checks to pass: select `check` (CI required to merge).
3. Require linear history (rebase or squash, no merge commits).
4. Require pull request reviews if your team policy needs them.

This ensures the green tick on `master` actually means "tests passed" — without it, someone can push directly and CI runs after the fact.

## Optional: Release tags

If you also tag releases (`v1.2.3`), add a release workflow that builds and pushes a versioned image:

```yaml
name: Release

on:
  push:
    tags: ["v*.*.*"]

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          target: prod
          push: true
          tags: |
            ghcr.io/${{ github.repository }}:${{ github.ref_name }}
            ghcr.io/${{ github.repository }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

Then `make deploy IMAGE_TAG=v1.2.3` ships that release. Skip this if you only deploy from `master`.

## What's intentionally NOT in CI

- **Auto-deploy** — Ansible runs from a developer's machine. Vault password and SSH keys live there, not in CI secrets.
- **Cosign / SBOM signing** — supply-chain security is a separate concern. Add when you have a clear consumer.
- **Multi-arch builds** (`linux/amd64,linux/arm64`) — adds significant build time. Add if you actually deploy to ARM hosts.
- **Python version matrix** — single Python pin from `pyproject.toml`. The project doesn't support multiple Python versions.
- **Coverage reports** — opinionated debate; if you want them, run `pytest --cov` and upload to Codecov as a separate step. Not required.

## Common Mistakes

- **Using the dev compose stack in CI.** GitHub Actions services are simpler, faster, and behave the same.
- **Tagging the image `:latest` from CI.** Production deploys are SHA-pinned; `:latest` is reproducibility's enemy.
- **Auto-deploying on green CI.** This skill set keeps deploy explicit and human-triggered. If you need auto-deploy, that's a different workflow with explicit guardrails (manual approval gate, deploy windows, etc.).
- **Skipping `--frozen`** in `uv sync`. CI must install exactly what `uv.lock` says — drift breaks reproducibility.
- **Not requiring `check` as a branch protection rule.** A green checkmark on `master` is meaningless if CI didn't gate the merge.
- **Storing real secrets in CI for tests.** Use throwaway values (`SECRET_KEY: ci-secret-not-used-in-prod`). Real secrets only exist in Ansible Vault on a developer's machine.

## Verify

```bash
# Push to a branch and open a PR
git push -u origin feature-branch
# Open PR in GitHub UI; CI should run and pass

# Push to master and confirm the build job ran
git push origin master
# Inspect the workflow run; image should appear in GHCR
gh run list --workflow=ci.yml
gh run view <run-id>

# Deploy the built image
make deploy IMAGE_TAG=<sha-from-ci>
```

## Checklist

- [ ] `.github/workflows/ci.yml` runs `check` on PR + push to master, and `build` only on push to master
- [ ] `check` job runs lint, format check, type check, tests, and OpenAPI schema validation
- [ ] Tests run against real Postgres + Redis service containers (not mocked)
- [ ] `build` job uses `--target prod` from the multi-stage Dockerfile
- [ ] Image tagged with `${{ github.sha }}` — never `:latest`
- [ ] Image pushed to GHCR (or your registry) with `${{ secrets.GITHUB_TOKEN }}`
- [ ] GHA buildx cache enabled (`cache-from: type=gha` / `cache-to: type=gha,mode=max`)
- [ ] Branch protection on `master` requires `check` to pass
- [ ] Deploy stays manual: `make deploy IMAGE_TAG=<sha>` after CI publishes
- [ ] No real secrets in CI (only throwaway values for tests)
