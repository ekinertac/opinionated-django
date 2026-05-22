---
name: django-signals
description: Reliable signals via Celery. ReliableSignal from config.signals (never standard Django signals for cross-service). send_reliable(sender=None, foo_id=1) inside transaction.atomic(). Args JSON-serializable (IDs not models). Receivers run as Celery tasks on commit. Receivers MUST be idempotent (at-least-once delivery). Use for notifications, cache invalidation, analytics, async side effects.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Add a Reliable Signal

You are adding a reliable signal to an opinionated Django project. Standard Django signals are synchronous and unreliable — receiver failures propagate to the sender, there's no delivery guarantee if the process crashes after commit, and there's no retry. This project uses the reliable signals pattern with Celery instead.

## How It Works

Signal receiver tasks are enqueued **inside the same database transaction** as the business operation. If the transaction rolls back, the tasks roll back too. If it commits, the tasks are guaranteed to be in the queue. Celery processes them asynchronously with at-least-once delivery.

## BEFORE WRITING CODE

1. Read `ARCHITECTURE.md` if present for the full reliable signals reference
2. Find existing signals with `Grep` for `ReliableSignal` under `src/`
3. Find existing receivers with `Glob` for `src/apps/**/receivers.py`
4. Identify which service method should emit the signal and what data needs to travel with it

---

## Step 1: Define the Signal

File: `src/apps/<app>/signals.py`

```python
from config.signals import ReliableSignal

my_event = ReliableSignal()
```

## Step 2: Send from the Service Layer

Call `send_reliable()` **inside** a `transaction.atomic()` block. Arguments MUST be JSON-serializable — pass entity IDs, never model instances:

```python
from django.db import transaction

def create_entity(self, name: str) -> MyEntityDTO:
    with transaction.atomic():
        entity = self.repo.create(name=name)
        my_event.send_reliable(sender=None, entity_id=entity.id)
    return entity
```

## Step 3: Write the Receiver

File: `src/apps/<app>/receivers.py`

```python
from django.dispatch import receiver
from .signals import my_event

@receiver(my_event)
def on_my_event(obj_id: int, **kwargs):
    if already_processed(obj_id):
        return
    do_work(obj_id)
```

**CRITICAL: Every receiver MUST be idempotent.** The system guarantees at-least-once delivery, not exactly-once. A receiver may run more than once for the same event. Design accordingly:

- Check if the action was already performed before performing it
- Use database constraints or flags to prevent duplicate effects
- Never assume a receiver runs exactly once

## Step 4: Load Receivers in `apps.py`

```python
class MyAppConfig(AppConfig):
    def ready(self):
        from . import receivers  # noqa: F401
```

## Step 5: Test

Test receivers in isolation. Mock external dependencies. Verify idempotency by calling the receiver twice with the same arguments:

```python
def test_receiver_is_idempotent():
    on_my_event(obj_id=1)
    on_my_event(obj_id=1)  # second call must be safe
    # assert side-effect happened exactly once
```

---

## Rules

- NEVER use standard Django `send()` for post-commit side-effects — use `send_reliable()`
- Arguments MUST be JSON-serializable (strings, numbers, booleans) — never model instances
- Receivers MUST be idempotent — this is non-negotiable
- Receivers MUST NOT import or touch ORM models directly — use a repository if DB access is needed
- Receivers MUST NOT call other services that emit signals (no cascading) without careful consideration of idempotency across the chain

---

## VERIFY

```bash
make check    # lint + format-check + typecheck
make test
```

If anything fails, fix it and re-run.
