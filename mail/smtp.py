"""Sends email through the SMTP server of a mailbox on your own domain (for
example no-reply@disconotify.com at Google Workspace, Zoho Mail, Fastmail,
Namecheap Private Email or your web host) — so every email comes from
@disconotify.com.

Uses Python's standard library (`smtplib`), run in a worker thread so the
bot's event loop never waits on the mail server. One connection per message:
the volume here is a handful of emails an hour, and it keeps a dropped
connection from ever affecting the next email.

Every address the mail host's DNS lists is tried in turn, IPv4 first, each with
its whole connection and encryption handshake, and the next one is tried when
one fails. Some hosts (Railway among them) can't route IPv6 and fail with
"Network is unreachable"; some home networks can open an IPv4 connection but
never finish the TLS handshake over it. Either way a working address is found.

Security: `ssl` (implicit TLS, usually port 465) or `starttls` (usually port
587) — never plain text. The login is only sent over the encrypted connection.
"""

from __future__ import annotations

import asyncio
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any, List, Optional, Tuple

from mail.errors import PermanentMailError, TransientMailError

_TIMEOUT_SECONDS = 20.0
SECURITY_MODES = ("ssl", "starttls")


Target = Tuple[int, Any]  # (address family, socket address)

# Each address gets this long to connect and finish its encryption handshake
# (the last one gets _TIMEOUT_SECONDS), so a dead address doesn't hold up the next.
_ATTEMPT_TIMEOUT_SECONDS = 8.0


def resolve_targets(host: str, port: int) -> List[Target]:
    """The mail host's addresses, IPv4 first (DNS order otherwise)."""
    entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    entries.sort(key=lambda entry: 0 if entry[0] == socket.AF_INET else 1)
    return [(family, sockaddr) for family, _kind, _proto, _canonical, sockaddr in entries]


def _open_socket(target: Target, timeout: float) -> socket.socket:
    family, sockaddr = target
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        raise
    return sock


class _Smtp(smtplib.SMTP):
    """Connects to one chosen address (`_target`) rather than letting smtplib pick."""

    _target: Target

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return _open_socket(self._target, timeout)


class _SmtpSsl(smtplib.SMTP_SSL):
    _target: Target

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        # Wrapped with the host name, so the certificate is still checked against it.
        return self.context.wrap_socket(_open_socket(self._target, timeout), server_hostname=self._host)


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
        """An open, encrypted connection to the first address that works."""
        targets = resolve_targets(self._host, self._port)
        last_error: Exception = OSError(f"No address found for {self._host}")
        for index, target in enumerate(targets):
            last = index == len(targets) - 1
            try:
                return self._connect_to(target, _TIMEOUT_SECONDS if last else _ATTEMPT_TIMEOUT_SECONDS)
            except (OSError, ssl.SSLError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError) as exc:
                last_error = exc  # try the next address
        raise last_error

    def _connect_to(self, target: Target, timeout: float) -> smtplib.SMTP:
        context = ssl.create_default_context()
        server: smtplib.SMTP
        if self._security == "ssl":
            server = _SmtpSsl(timeout=timeout, context=context)
        else:
            server = _Smtp(timeout=timeout)
        server._target = target  # type: ignore[attr-defined]
        server._host = self._host  # the name the certificate is checked against
        try:
            server.connect(self._host, self._port)
            if self._security != "ssl":
                server.starttls(context=context)  # the login below only ever travels encrypted
        except Exception:
            server.close()
            raise
        if server.sock is not None:
            server.sock.settimeout(_TIMEOUT_SECONDS)
        return server


def _summary(exc: Exception) -> str:
    """A short, log-safe description (no addresses of other people, no credentials)."""
    if isinstance(exc, smtplib.SMTPResponseException):
        detail = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"{exc.smtp_code} {detail}"[:200]
    return str(exc)[:200]
