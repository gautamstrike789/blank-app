"""Concrete notification channels.

Each is a thin adapter.  Nothing here knows what an alert is - that decision
was already made by the routing layer - which is what keeps adding WhatsApp or
any other channel a matter of writing one class.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from typing import Sequence

from qmis.notify.base import Digest, Notifier


class ConsoleNotifier(Notifier):
    """Prints digests. The default, and what ``--dry-run`` uses."""

    kind = "console"

    def __init__(self, stream=None):
        self.stream = stream
        self.sent: list[Digest] = []

    def send(self, digest: Digest) -> None:
        self.sent.append(digest)
        text = f"\n--- to {digest.recipient} ---\n{digest.subject}\n\n{digest.body}\n"
        if self.stream is not None:
            self.stream.write(text)
        else:
            print(text)


class EmailNotifier(Notifier):
    """SMTP delivery. The password is read from the environment, never config."""

    kind = "email"

    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str | None = None,
        password_env: str = "QMIS_SMTP_PASSWORD",
        sender: str | None = None,
        use_tls: bool = True,
        timeout: int = 30,
    ):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = os.environ.get(password_env)
        self.sender = sender or username or "qmis@localhost"
        self.use_tls = bool(use_tls)
        self.timeout = timeout

    def send(self, digest: Digest) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = digest.recipient
        message["Subject"] = digest.subject
        message.set_content(digest.body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
            if self.use_tls:
                server.starttls(context=ssl.create_default_context())
            if self.username and self.password:
                server.login(self.username, self.password)
            server.send_message(message)


class WebhookNotifier(Notifier):
    """Slack or Microsoft Teams incoming webhook.

    Both accept a JSON POST; only the payload key differs, so one adapter with
    a ``format`` switch covers both rather than two near-identical classes.
    """

    kind = "webhook"

    def __init__(self, url_env: str = "QMIS_WEBHOOK_URL", url: str | None = None,
                 format: str = "slack", timeout: int = 20):
        self.url = url or os.environ.get(url_env, "")
        self.format = format
        self.timeout = timeout

    def send(self, digest: Digest) -> None:
        if not self.url:
            raise RuntimeError(
                "webhook notifier has no URL; set the configured url_env variable"
            )
        text = f"*{digest.subject}*\n```{digest.body}```"
        if self.format == "teams":
            payload = {"title": digest.subject, "text": digest.body}
        elif self.format == "raw":
            payload = {"subject": digest.subject, "body": digest.body, "to": digest.recipient}
        else:
            payload = {"text": text}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if response.status >= 300:
                raise RuntimeError(f"webhook returned HTTP {response.status}")


def build_notifiers(configs: Sequence[dict]) -> list[Notifier]:
    """Instantiate channels from the ``notifications.channels`` settings block."""
    out: list[Notifier] = []
    for config in configs or []:
        kind = str(config.get("kind", "")).lower()
        options = {k: v for k, v in config.items() if k != "kind"}
        if kind == "console":
            out.append(ConsoleNotifier())
        elif kind == "email":
            out.append(EmailNotifier(**options))
        elif kind == "webhook":
            out.append(WebhookNotifier(**options))
        else:
            raise ValueError(f"unknown notification channel {kind!r}")
    return out
