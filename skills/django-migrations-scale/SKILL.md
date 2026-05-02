---
name: django-migrations-scale
description: Apply schema changes to a production database under rolling deploys without downtime — the expand-contract pattern for destructive changes (drops, renames, type changes), lock-aware DDL with statement_timeout and CREATE INDEX CONCURRENTLY, deploy ordering for multi-step migrations, and the checklist of which Django migration operations are safe vs unsafe at scale. Use when planning a schema change on a production table, modeling a column rename, dropping a field, changing a column type, or whenever the user mentions migrations under load, zero-downtime DDL, expand-contract, or schema evolution.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Migrations Under Rolling Deploys

The deploy story from **django-deploy** is rolling: migrations run once before any web container restarts, and N web hosts then swap to the new image one at a time. This means **old code runs against the new schema for the duration of the rollout window** — minutes, sometimes longer. Most migrations are safe in that window because they're additive. The destructive ones are not.

This skill is about the destructive ones: how to ship a column drop, rename, or type change to a live system without downtime.

## Step 1: The migration safety table

| Operation | Rolling-deploy safe? | Why |
|---|---|---|
| `AddField` (nullable, or with default at DB level on PG 11+) | ✅ | Old code ignores the new column. |
| `AddField` (NOT NULL, no default) | ❌ | Old code's INSERTs lack the column → 500s during rollout. Use expand-contract. |
| `CreateModel` | ✅ | Old code doesn't reference the new model. |
| `DeleteModel` | ⚠️ | Only safe if no code path still references it. Verify with `grep` first. |
| `AddIndex` (small table) | ✅ | Brief table-level lock acceptable. |
| `AddIndex` (large table) | ⚠️ | Default `CREATE INDEX` blocks writes. Use `CREATE INDEX CONCURRENTLY` (Step 4). |
| `RemoveIndex` | ✅ | Fast metadata change. |
| `AddConstraint` (`UniqueConstraint`) | ⚠️ | Scans the table; may fail if duplicates exist; new code can't insert duplicates after. Phase it. |
| `AddConstraint` (`CheckConstraint`, NOT NULL) | ❌ | Full table scan with an exclusive lock. Apply `NOT VALID` first, validate later (Step 4). |
| `RemoveField` | ❌ | Old code still SELECTs / writes the column. Use expand-contract. |
| `RenameField` | ❌ | Old code uses the old name. Use expand-contract. |
| `AlterField` (column type change) | ❌ | Often requires a full table rewrite + may break old code's data parsing. Use expand-contract. |
| `AlterField` (changing `default`, `verbose_name`, `help_text`) | ✅ | Pure metadata. |
| `AlterField` (`max_length` increase) | ✅ on PG ≥ 9.2 | Metadata-only since PG 9.2. Decreasing is destructive. |
| `RunPython` (data migration on a small table) | ✅ | Fast. |
| `RunPython` (data migration on a large table) | ❌ | Holds a long transaction; locks build up. Use a management command instead (Step 5). |
| `RunSQL` (anything DDL-ish) | depends | Each statement evaluated against the rules above. |

**Heuristic:** if the operation either (a) adds something old code can ignore, or (b) is metadata-only, it's safe. Anything else needs sequencing across deploys.

## Step 2: Expand-contract — the universal pattern

Any destructive change becomes 3-6 deploys. The pattern:

```
deploy A:  EXPAND   — add the new structure alongside the old
deploy B:  WRITE    — code writes to both, reads from old
deploy C:  BACKFILL — copy historical data from old to new
deploy D:  READ     — code reads from new, still writes to both
deploy E:  STOP-OLD — code stops touching the old structure
deploy F:  CONTRACT — drop the old structure
```

Skip steps that don't apply (e.g., a pure rename has no backfill if you copy in deploy A). The point is no single deploy puts the system in a state where old + new code can't both work against the schema.

### Worked example: rename a column

Goal: rename `User.username` to `User.handle`.

**Deploy A — EXPAND.** Migration adds `handle` as a nullable column. Code unchanged.

```python
operations = [
    migrations.AddField(
        model_name="user",
        name="handle",
        field=models.CharField(max_length=150, null=True),
    ),
]
```

After this deploys, both old and new servers run; nothing writes `handle` yet.

**Deploy B — WRITE.** Code writes to both columns; reads still come from `username`.

```python
# In the service
def update_item(self, pk: int, **fields):
    if "username" in fields:
        fields["handle"] = fields["username"]
    return self.repo.update(pk, **fields)
```

**Deploy C — BACKFILL.** A management command (NOT a `RunPython` migration) copies `username` → `handle` for existing rows in batches.

```python
# apps/users/management/commands/backfill_handle.py
from django.core.management.base import BaseCommand
from apps.users.models import User

class Command(BaseCommand):
    def handle(self, *args, **opts):
        batch_size = 1000
        last_id = 0
        while True:
            qs = User.objects.filter(id__gt=last_id, handle__isnull=True).order_by("id")[:batch_size]
            ids = list(qs.values_list("id", flat=True))
            if not ids:
                break
            User.objects.filter(id__in=ids).update(handle=models.F("username"))
            last_id = ids[-1]
            self.stdout.write(f"Backfilled up to id {last_id}")
```

Run via `docker compose exec web uv run python manage.py backfill_handle`. Idempotent (the `handle__isnull=True` filter), restartable, doesn't hold a single long transaction.

**Deploy D — READ.** Code now reads `handle` (with a fallback to `username` for safety). Still writes both.

**Deploy E — STOP-OLD.** Code stops referencing `username` entirely. Still in the schema, just unused.

**Deploy F — CONTRACT.** Migration drops `username`. Now safe because no live code reads it.

```python
operations = [
    migrations.RemoveField(model_name="user", name="username"),
]
```

This is six deploys for one rename. That's the cost of zero-downtime. The discipline pays for itself the first time you would have had a Saturday-night downtime to run an `ALTER TABLE`.

### Shortcut: drop without rename

A pure drop (no replacement) is shorter — three deploys:

1. **Deploy A — STOP-OLD.** Code stops writing/reading the column.
2. **Deploy B — VERIFY.** Confirm no usage in production logs / metrics for one full deploy cycle. Skip if confidence is high.
3. **Deploy C — CONTRACT.** Migration drops the column.

## Step 3: NOT NULL columns — the special case

Adding `NOT NULL` requires four steps:

1. **Deploy A.** Add the column nullable, with a Django-level default for new writes.
2. **Deploy B.** Backfill existing rows (management command).
3. **Deploy C.** Add a `CheckConstraint` (`is_null=False`) with `NOT VALID`:

   ```python
   migrations.RunSQL(
       sql="ALTER TABLE myapp_user ADD CONSTRAINT user_handle_not_null CHECK (handle IS NOT NULL) NOT VALID;",
       reverse_sql="ALTER TABLE myapp_user DROP CONSTRAINT user_handle_not_null;",
   )
   ```

   `NOT VALID` adds the constraint without scanning the table — applies to new rows immediately, leaves old rows alone.

4. **Deploy D.** Validate the constraint, then convert the column to `NOT NULL`:

   ```python
   migrations.RunSQL(
       sql="""
           ALTER TABLE myapp_user VALIDATE CONSTRAINT user_handle_not_null;
           ALTER TABLE myapp_user ALTER COLUMN handle SET NOT NULL;
           ALTER TABLE myapp_user DROP CONSTRAINT user_handle_not_null;
       """,
       reverse_sql="ALTER TABLE myapp_user ALTER COLUMN handle DROP NOT NULL;",
   )
   ```

   `VALIDATE CONSTRAINT` scans the table without an exclusive lock; safe under load.

The Django ORM doesn't generate this directly — you write it as `RunSQL`.

## Step 4: Lock-aware DDL

Some operations Postgres can do online (concurrently) with the right syntax. Always use the online variant in production.

### `CREATE INDEX CONCURRENTLY`

The default `CREATE INDEX` takes an `ACCESS EXCLUSIVE` lock — blocks reads AND writes for the duration. On a 50M-row table this is minutes of downtime. The `CONCURRENTLY` variant lets reads and writes proceed during the build:

```python
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False  # CONCURRENTLY can't run in a transaction

    operations = [
        migrations.RunSQL(
            sql="CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_order_status_created ON orders_order (status, created_at DESC);",
            reverse_sql="DROP INDEX IF EXISTS idx_order_status_created;",
        ),
    ]
```

`atomic = False` is required — `CONCURRENTLY` can't run inside a transaction.

If the index build fails (e.g., a unique constraint violated), Postgres leaves an `INVALID` index behind. Drop it manually (`DROP INDEX idx_xxx`) before retrying — Django doesn't notice the invalid state.

### `statement_timeout` for DDL

Set a `statement_timeout` for DDL migrations so a stuck migration doesn't hold a lock forever:

```python
operations = [
    migrations.RunSQL(
        sql="""
            SET statement_timeout = '5s';
            ALTER TABLE myapp_thing ADD COLUMN ...;
        """,
        # ...
    ),
]
```

If the lock can't be acquired in 5 seconds, the migration fails fast — operator can investigate why locks are blocked and retry, instead of finding production frozen for an hour.

### Avoid `ALTER TABLE ... ADD COLUMN ... DEFAULT` on PG < 11

On Postgres ≥ 11, adding a column with a default is a metadata-only operation (`pg_attribute` update, no rewrite). On older versions, it rewrites the entire table while holding `ACCESS EXCLUSIVE`. If you're on PG ≥ 11 (and the **django-deploy** Postgres role pins 16-alpine, so you are), this is safe. On older versions, do it as: add nullable, backfill, NOT NULL.

## Step 5: Data migrations — management commands, not `RunPython`

Django lets you put data migrations inside a migration file via `RunPython`. **Don't, for any non-trivial data.** Reasons:

- The whole migration runs in one transaction. A backfill of millions of rows holds locks for hours.
- A failure mid-run leaves the migration half-done; restart starts over.
- The migration runner is single-threaded; no parallelism.
- Migrations are part of the deploy critical path; a slow data migration blocks every following migration.

Instead: write a **management command** that's idempotent and batched (see Step 2's `backfill_handle` example). Run it after the schema migration deploys, before the deploy that reads from the new structure.

`RunPython` is fine for tiny operations — populating a lookup table with 50 rows, normalizing a few enum values that exist on a small table. Anything that takes more than a few seconds belongs in a management command.

## Step 6: Multi-deploy migration plan template

When you propose a destructive migration, the PR description should include the deploy sequence:

```
EXPAND    PR #423 — Adds `handle` column (nullable). Schema-only, no code changes.
WRITE     PR #424 — Service writes both `username` and `handle`. Reads `username`.
BACKFILL  Run     — `manage.py backfill_handle` after PR #424 deploys. Verify count.
READ      PR #425 — Service reads `handle`, writes both.
STOP-OLD  PR #426 — Service stops referencing `username`. Verify in logs for one deploy cycle.
CONTRACT  PR #427 — Migration drops `username`.
```

Each row is a separate PR / deploy. The plan goes in the EXPAND PR's description so reviewers and future-you understand the multi-deploy contract.

## Common Mistakes

- **Shipping a destructive migration in one PR with the code that uses the new shape.** During the rolling deploy, old web hosts crash on requests that touch the old shape. Either downtime or expand-contract — pick one.
- **Skipping `CREATE INDEX CONCURRENTLY` on large tables.** A regular `CREATE INDEX` on a 50M-row table is minutes of write outage.
- **`RunPython` for million-row backfills.** One transaction, no batching, no recovery. Use a management command.
- **Forgetting `atomic = False` on a `CONCURRENTLY` migration.** Migration runs inside a transaction by default; Postgres rejects the statement.
- **Adding `NOT NULL` directly after backfill, in the same migration.** The `ALTER TABLE ... SET NOT NULL` takes an exclusive lock that scans the table. Use the `NOT VALID` constraint dance.
- **No `statement_timeout` on DDL.** A blocked lock holds production hostage. 5-30s ceiling, fail fast, investigate.
- **Running migrations during peak traffic.** Even safe migrations contend for locks under load. Schedule them in low-traffic windows when possible.
- **Trusting Django's auto-detected `RenameField`.** Django will generate it cleanly, but the resulting migration is destructive (it actually issues `ALTER TABLE RENAME COLUMN`). Treat any auto-generated `RenameField` as if it were a multi-deploy expand-contract.
- **Not auditing `grep` for usages before contracting.** "We stopped using it last sprint" is not a substitute for actually grepping the codebase + searching production logs for the column name.

## Verify

```bash
# Spot-check migrations for destructive operations before merge
docker compose exec web uv run python manage.py sqlmigrate myapp 0042 | less

# Long-running migration check — show what locks are being held in prod
docker compose exec postgres psql -U $POSTGRES_USER $POSTGRES_DB -c "
  SELECT pid, mode, locktype, relation::regclass, granted, query_start, query
  FROM pg_locks JOIN pg_stat_activity USING (pid)
  WHERE NOT granted OR mode LIKE '%Exclusive%';
"

# After a CONCURRENTLY index build, confirm it's VALID (not stuck in INVALID)
docker compose exec postgres psql -U $POSTGRES_USER $POSTGRES_DB -c "
  SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE NOT indisvalid;
"
# Empty result = all indexes valid.
```

## Checklist

- [ ] Migration plan documented in the PR description: which deploys, in what order
- [ ] Destructive operations (RemoveField, RenameField, type changes, NOT NULL) split into expand-contract steps
- [ ] Backfills are management commands, batched, idempotent — never `RunPython` for large tables
- [ ] Index creation on large tables uses `CREATE INDEX CONCURRENTLY` with `atomic = False`
- [ ] `NOT NULL` added via `CHECK ... NOT VALID` → backfill → `VALIDATE CONSTRAINT` → `SET NOT NULL`
- [ ] DDL migrations have `SET statement_timeout` so stuck locks fail fast
- [ ] No `RunPython` data migration takes more than a few seconds on the largest table it touches
- [ ] Before contract: grep the codebase for the dropped name, confirm production logs don't reference it
- [ ] Migration ran in staging against a copy of production data before going to prod
