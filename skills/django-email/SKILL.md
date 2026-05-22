---
name: django-email
description: Transactional email via AWS SES SMTP. EmailService idempotent via sha256(template,to,context) or explicit idempotency_key. Templates: src/templates/emails/<name>/{subject.txt,body.txt,body.html}. Triggered via reliable signal (commits with transaction). Celery retry on SMTPException with exponential backoff. Suppression list from SNS bounce/complaint webhook (verify signature). AWS-side: DKIM, SPF, DMARC, production access.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Transactional Email

Email is delivered via AWS SES over SMTP, with the SMTP credentials and configuration set up in **django-deploy**. This skill covers the application side: how email gets sent, how delivery is made reliable, and how bounces are handled.

The shape:

```
Service triggers a reliable signal  →  Receiver enqueues a Celery task  →  Task renders + sends via SES SMTP  →  SNS posts back bounces/complaints  →  Webhook updates suppression list
```

Reliable signals (see **django-signals**) ensure the email task is enqueued only if the originating transaction commits. Celery handles retries on transient SMTP failures. Idempotency prevents duplicate sends when retries fire.

## Step 1: Templates

Email templates live alongside other templates: `src/templates/emails/<name>/`. Each email has a directory with three files:

```
src/templates/emails/welcome/
  subject.txt        # one-line subject, rendered as a template
  body.txt           # plain-text body
  body.html          # HTML body (optional but recommended)
```

Plain text is mandatory; HTML is optional but improves rendering in modern clients. Always send both when HTML is provided — multipart MIME with text fallback.

Example `welcome/body.txt`:

```
Hi {{ user.first_name }},

Thanks for joining. Your account is ready at {{ site_url }}.

— The team
```

Example `welcome/subject.txt`:

```
Welcome to {{ site_name }}, {{ user.first_name }}
```

## Step 2: `EmailService`

A single service in `src/apps/email/services.py` (or wherever your project's email app lives) handles all outgoing mail. The shape:

```python
import hashlib
import json
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .dtos import EmailDTO
from .repositories import EmailRepository


class EmailService:
    def __init__(self, repo: EmailRepository):
        self.repo = repo

    def send(
        self,
        *,
        template: str,
        to: str,
        context: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> EmailDTO:
        """Render a template + send via SMTP. Idempotent on `idempotency_key`."""
        key = idempotency_key or self._derive_key(template, to, context)

        if self.repo.exists_by_idempotency_key(key):
            return self.repo.get_by_idempotency_key(key)

        if self.repo.is_suppressed(to):
            return self.repo.record(
                template=template, to=to, key=key,
                status="suppressed", sent_at=None,
            )

        subject = render_to_string(f"emails/{template}/subject.txt", context).strip()
        body_txt = render_to_string(f"emails/{template}/body.txt", context)
        body_html = self._maybe_render_html(template, context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=body_txt,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to],
        )
        if body_html:
            msg.attach_alternative(body_html, "text/html")
        msg.send(fail_silently=False)

        return self.repo.record(
            template=template, to=to, key=key,
            status="sent", sent_at=timezone.now(),
        )

    def _derive_key(self, template: str, to: str, context: dict[str, Any]) -> str:
        """Deterministic key for idempotency. Override `idempotency_key` if context isn't stable."""
        payload = json.dumps({"template": template, "to": to, "context": context}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _maybe_render_html(self, template: str, context: dict[str, Any]) -> str | None:
        try:
            return render_to_string(f"emails/{template}/body.html", context)
        except Exception:
            return None
```

Notes:

- **Idempotency by hash**. The default `_derive_key` produces a stable hash of `(template, to, context)`. If the caller passes `idempotency_key` explicitly (e.g., `f"order_confirmed:{order_id}"`), that wins — useful when context contains volatile data (timestamps) that shouldn't break dedup.
- **Suppression check** before sending. If a recipient has bounced or complained, the call records the attempt as `suppressed` and returns without dialing SMTP.
- **Returns a DTO**, not a boolean. The DTO carries the email's identity, status, and timestamp — useful for tracking and tests.

## Step 3: Repository

`src/apps/email/repositories.py` follows the repository conventions from **django-repositories**:

```python
from django.db import transaction

from .dtos import EmailDTO
from .models import EmailLog, EmailSuppression


class EmailRepository:
    def exists_by_idempotency_key(self, key: str) -> bool:
        return EmailLog.objects.filter(idempotency_key=key).exists()

    def get_by_idempotency_key(self, key: str) -> EmailDTO:
        try:
            obj = EmailLog.objects.get(idempotency_key=key)
        except EmailLog.DoesNotExist as e:
            raise LookupError(f"EmailLog with key {key!r} not found") from e
        return EmailDTO.model_validate(obj)

    def is_suppressed(self, address: str) -> bool:
        return EmailSuppression.objects.filter(address=address).exists()

    @transaction.atomic
    def record(self, *, template: str, to: str, key: str, status: str, sent_at) -> EmailDTO:
        obj = EmailLog.objects.create(
            idempotency_key=key, template=template, to=to,
            status=status, sent_at=sent_at,
        )
        return EmailDTO.model_validate(obj)

    @transaction.atomic
    def suppress(self, *, address: str, reason: str) -> None:
        EmailSuppression.objects.update_or_create(
            address=address, defaults={"reason": reason},
        )
```

The `EmailLog` table acts as both the audit trail and the idempotency record. The unique constraint on `idempotency_key` is what makes "called twice, sent once" work.

## Step 4: Triggering from a service via reliable signals

Email is a side-effect of a business operation, so it rides reliable signals (see **django-signals**). Inside the originating service's `transaction.atomic()`, fire a signal; a receiver enqueues a Celery task that calls `EmailService.send`.

```python
# apps/users/signals.py
from config.signals import ReliableSignal
user_registered = ReliableSignal()

# apps/users/services.py
from django.db import transaction
from .signals import user_registered

class UserService:
    def create_item(self, *, email: str, name: str) -> UserDTO:
        with transaction.atomic():
            user = self.repo.create(email=email, name=name)
            user_registered.send_reliable(sender=None, user_id=user.id)
        return user

# apps/users/receivers.py
from django.dispatch import receiver
from config.services import get
from apps.email.services import EmailService
from apps.users.repositories import UserRepository
from .signals import user_registered

@receiver(user_registered)
def on_user_registered(user_id: int, **kwargs):
    user = get(UserRepository).get_by_id(user_id)
    get(EmailService).send(
        template="welcome",
        to=user.email,
        context={"user": user.model_dump(), "site_name": "Acme", "site_url": "https://acme.example.com"},
        idempotency_key=f"welcome:user:{user_id}",
    )
```

The receiver's `idempotency_key` is explicit (`welcome:user:{user_id}`) so a Celery retry doesn't double-send even if the context dict were to change between attempts.

## Step 5: Celery retry policy

Email-sending is wrapped by Celery's retry mechanism — `send_reliable` already runs receivers as tasks (see **django-signals**). For the SMTP call inside the receiver, retries are needed when SES returns transient errors (`ThrottlingException`, network blips). The `_dispatch_reliable_receiver` task accepts retries; configure them at the wrapper level or wrap the SMTP call:

```python
import smtplib
from celery import shared_task
from config.services import get
from apps.email.services import EmailService


@shared_task(
    bind=True,
    autoretry_for=(smtplib.SMTPException, ConnectionError),
    retry_backoff=True,           # exponential backoff: 1s, 2s, 4s, 8s
    retry_backoff_max=600,         # cap at 10 minutes
    retry_jitter=True,
    max_retries=5,
)
def send_email_task(self, **kwargs):
    get(EmailService).send(**kwargs)
```

Use this task directly when you don't need a signal indirection (e.g., admin-triggered emails). The reliable-signal path uses the same machinery via `_dispatch_reliable_receiver`.

If a permanent failure (`SMTPRecipientsRefused`) occurs, don't retry — the address is bad. Catch it inside the service and record the failure in `EmailLog`:

```python
try:
    msg.send(fail_silently=False)
except smtplib.SMTPRecipientsRefused:
    return self.repo.record(template=template, to=to, key=key, status="bounced", sent_at=None)
```

## Step 6: Bounces and complaints via SNS

AWS SES posts bounce and complaint notifications to an SNS topic; SNS posts to a webhook. The webhook receives a JSON envelope; you parse it, mark the address as suppressed, and never email it again.

Add an unauthenticated endpoint (it's authenticated via the SNS message signature, not Django auth):

```python
# apps/email/views.py
import json

from rest_framework import status, viewsets
from rest_framework.decorators import action, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from config.services import get

from .services import EmailService


@permission_classes([AllowAny])
class SESWebhookViewSet(viewsets.ViewSet):
    @action(detail=False, methods=["post"])
    def sns(self, request):
        envelope = json.loads(request.body)
        # Verify SNS signature here — boto3 has helpers, or use sns-message-validator.
        # If verification fails, return 403.
        msg_type = envelope.get("Type")

        if msg_type == "SubscriptionConfirmation":
            # Confirm by GET-ing the SubscribeURL
            ...
            return Response(status=status.HTTP_200_OK)

        if msg_type == "Notification":
            payload = json.loads(envelope["Message"])
            event = payload.get("eventType") or payload.get("notificationType")

            if event == "Bounce":
                for r in payload["bounce"]["bouncedRecipients"]:
                    get(EmailService).suppress_address(
                        address=r["emailAddress"], reason="bounce"
                    )
            elif event == "Complaint":
                for r in payload["complaint"]["complainedRecipients"]:
                    get(EmailService).suppress_address(
                        address=r["emailAddress"], reason="complaint"
                    )

        return Response(status=status.HTTP_200_OK)
```

Add `suppress_address` to the service:

```python
class EmailService:
    def suppress_address(self, *, address: str, reason: str) -> None:
        self.repo.suppress(address=address, reason=reason)
```

The `is_suppressed` check at the top of `send()` keeps suppressed addresses from getting more mail.

**Verifying the SNS signature** is non-negotiable — without it, anyone can POST a fake bounce to your webhook and mark arbitrary addresses as suppressed. Use `sns-message-validator` or follow the AWS pattern: fetch the cert URL, verify the signature, check the message age.

## Step 7: Domain setup (one-time, AWS-side)

The skill assumes the AWS-side setup is done:

- **Domain verified** in SES (TXT record).
- **DKIM** enabled (three CNAMEs).
- **SPF** record exists (TXT including `include:amazonses.com`).
- **DMARC** policy exists (TXT, at least `v=DMARC1; p=none; rua=mailto:postmaster@example.com`).
- **Production access** requested (sandbox mode only sends to verified addresses).
- **SNS topic** created, subscribed via HTTPS to your webhook URL.
- **Configuration set** in SES forwarding bounces/complaints to that SNS topic.

These are GUI/CLI steps documented in AWS — out of scope for the skill body. They are a once-per-domain setup and must complete before email reaches inboxes reliably.

## Common Mistakes

- **Calling `send_mail` directly from a view or service that's mid-transaction.** If the transaction rolls back, the email already left. Use a reliable signal so the email is only enqueued on commit.
- **No idempotency key.** Celery retries are at-least-once. Without dedup, a transient SMTP error at the wrong moment sends two welcome emails.
- **Sending to suppressed addresses.** SES will reject them and your sender reputation tanks. Always check `is_suppressed` before dialing.
- **Skipping the SNS signature verification.** Trivial for an attacker to forge bounce notifications and DoS your address list.
- **No plain-text body.** Spam filters penalize HTML-only mail. Always send multipart.
- **Templates with `{{ user.password }}` or any sensitive context.** Templates are debug-friendly; sensitive data leaks easily. Audit context dicts.
- **Logging full email bodies.** PII goes to logs that go to monitoring. Log only metadata (template name, recipient, status).
- **Sending from `noreply@`** without monitoring it. Bounces and replies pile up. Use a real address you actually read OR explicitly forward `noreply@` to a monitored alias.

## Verify

```bash
# In a test env (with SES sandbox + a verified test address)
docker compose exec web uv run python manage.py shell
>>> from config.services import get
>>> from apps.email.services import EmailService
>>> get(EmailService).send(template="welcome", to="verified@example.com", context={...})

# Check the EmailLog
>>> from apps.email.models import EmailLog
>>> EmailLog.objects.last().__dict__

# Trigger a soft-bounce (in SES sandbox, send to bounce@simulator.amazonses.com)
>>> get(EmailService).send(template="welcome", to="bounce@simulator.amazonses.com", ...)
# After ~1 minute, the SNS webhook should fire and EmailSuppression should have an entry.
```

`make check` and `make test` cover the service-layer paths via real-DB tests (mock the SMTP send only — see **django-pytest** for the rule about mocking external boundaries).

## Checklist

- [ ] Templates live at `src/templates/emails/<name>/{subject.txt,body.txt,body.html}`
- [ ] `EmailService.send` is idempotent — checks `idempotency_key` before sending, records every attempt
- [ ] `EmailLog.idempotency_key` has a unique constraint
- [ ] `EmailSuppression` table exists; `is_suppressed` is checked before every send
- [ ] Email is triggered via reliable signal (see **django-signals**), never directly from inside an open transaction
- [ ] Receiver passes an explicit `idempotency_key` so retries are safe even with volatile context
- [ ] Celery task has `autoretry_for=(SMTPException, ConnectionError)`, `retry_backoff=True`, `max_retries=5`
- [ ] Permanent failures (`SMTPRecipientsRefused`) are caught and recorded as `bounced`, not retried
- [ ] SNS webhook endpoint at `/email/sns/` (or similar) verifies the message signature before processing
- [ ] AWS-side: domain verified, DKIM enabled, SPF + DMARC records published, production access granted
- [ ] No PII in logs — log template name + recipient + status, never bodies
- [ ] Test deliverability with `mail-tester.com` or similar; aim for 9+/10
