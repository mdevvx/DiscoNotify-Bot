"""Sends email through the SMTP server of a mailbox on your own domain (for
example no-reply@disconotify.com at Google Workspace, Zoho Mail, Fastmail,
Namecheap Private Email or your web host) — so every email comes from
@disconotify.com.

Uses Python's standard library (`smtplib`), run in a worker thread so the
bot's event loop never waits on the mail server. One connection per message:
the volume here is a handful of emails an hour, and it keeps a dropped
connection from ever affecting the next email.

Security: `ssl` (implicit TLS, usually port 465) or `starttls` (usually port
587) — never plain text. The login is only sent over the encrypted connection.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Optional

from mail.errors import PermanentMailError, TransientMailError

_TIMEOUT_SECONDS = 20.0
SECURITY_MODES = ("ssl", "starttls")


class SmtpClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        sender: str,
        reply_to: Optional[str] = None,
        security: str = "starttls",
    ) -> None:
        if security not in SECURITY_MODES:
            raise ValueError(f"security must be one of {SECURITY_MODES}")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._reply_to = reply_to
        self._security = security
        self._domain = parseaddr(sender)[1].rpartition("@")[2] or None

    async def aclose(self) -> None:
        """Nothing is kept open between messages."""

    async def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        unsubscribe_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """The message's Message-ID. (`idempotency_key` is accepted for the
        caller's convenience; SMTP has no use for it — the outbox's claim
        step is what keeps a message from being sent twice.)"""
        message = self._build(to, subject, html, text, unsubscribe_url)
        await asyncio.to_thread(self._deliver, message)
        return str(message["Message-ID"])

    # ── internals ───────────────────────────────────────────────────────

    def _build(self, to: str, subject: str, html: str, text: str, unsubscribe_url: Optional[str]) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain=self._domain)
        # Tells mail systems this is automatic, so they don't send out-of-office replies to it.
        message["Auto-Submitted"] = "auto-generated"
        if self._reply_to:
            message["Reply-To"] = self._reply_to
        if unsubscribe_url:
            message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
            message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        return message

    def _deliver(self, message: EmailMessage) -> None:
        try:
            with self._connect() as server:
                server.login(self._username, self._password)
                server.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentMailError(f"The address was refused: {_summary(exc)}") from exc
        except smtplib.SMTPSenderRefused as exc:
            # The mailbox/domain isn't allowed to send as this address: a settings problem.
            raise TransientMailError(f"The sender address was refused: {_summary(exc)}") from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise TransientMailError(f"The mail server refused the login (check EMAIL_SMTP_USERNAME/PASSWORD): {_summary(exc)}") from exc
        except smtplib.SMTPResponseException as exc:
            # 5xx = refused for good; 4xx = try again later.
            error = PermanentMailError if 500 <= exc.smtp_code < 600 else TransientMailError
            raise error(f"The mail server answered {exc.smtp_code}: {_summary(exc)}") from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError, TimeoutError) as exc:
            raise TransientMailError(f"Couldn't reach the mail server: {type(exc).__name__}: {exc}") from exc

    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if self._security == "ssl":
            return smtplib.SMTP_SSL(self._host, self._port, timeout=_TIMEOUT_SECONDS, context=context)
        server = smtplib.SMTP(self._host, self._port, timeout=_TIMEOUT_SECONDS)
        try:
            server.starttls(context=context)  # the login below only ever travels encrypted
        except Exception:
            server.close()
            raise
        return server


def _summary(exc: Exception) -> str:
    """A short, log-safe description (no addresses of other people, no credentials)."""
    if isinstance(exc, smtplib.SMTPResponseException):
        detail = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"{exc.smtp_code} {detail}"[:200]
    return str(exc)[:200]
