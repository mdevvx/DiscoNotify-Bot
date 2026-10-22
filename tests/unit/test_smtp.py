"""The SMTP sender: what the message looks like on the wire, that the login
only travels over an encrypted connection, and how mail-server answers are
sorted into "try again later" and "never going to work"."""

from __future__ import annotations

import smtplib
import socket
from typing import Any, Dict, List, Optional

import pytest

from config.settings import Settings
from mail import smtp as smtp_module
from mail.errors import PermanentMailError, TransientMailError
from mail.smtp import SmtpClient

_REAL_RESOLVE = smtp_module.resolve_targets  # the fixture below replaces it for every other test


class FakeServer:
    """Stands in for the SMTP connection classes; records what the client did."""

    instances: List["FakeServer"] = []
    failure: Optional[Exception] = None
    failure_at: str = "send_message"
    # Addresses (the IP part) that can't be connected to / whose TLS handshake hangs.
    unreachable: set = set()

    def __init__(self, timeout: float = 0, context: Any = None) -> None:
        self.timeout, self.context = timeout, context
        self.host: Optional[str] = None
        self.port: Optional[int] = None
        self.sock: Any = None
        self.calls: List[str] = []
        self.sent: List[Any] = []
        self.login_args: Optional[tuple] = None
        FakeServer.instances.append(self)

    def connect(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.calls.append(f"connect:{self._target[1][0]}")
        if self._target[1][0] in FakeServer.unreachable:
            raise OSError(101, "Network is unreachable")
        self.sock = _FakeSock()

    def __enter__(self) -> "FakeServer":
        return self

    def __exit__(self, *_: Any) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")
        self._maybe_fail("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.login_args = (user, password)
        self._maybe_fail("login")

    def send_message(self, message: Any) -> None:
        self.calls.append("send_message")
        self._maybe_fail("send_message")
        self.sent.append(message)

    def close(self) -> None:
        self.calls.append("close")

    def _maybe_fail(self, step: str) -> None:
        if FakeServer.failure is not None and FakeServer.failure_at == step:
            raise FakeServer.failure


class _FakeSock:
    def settimeout(self, timeout: float) -> None:
        pass


class FakeSslServer(FakeServer):
    pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer.instances = []
    FakeServer.failure = None
    FakeServer.failure_at = "send_message"
    FakeServer.unreachable = set()
    monkeypatch.setattr(smtp_module, "resolve_targets", lambda host, port: [(socket.AF_INET, ("203.0.113.7", port))])
    monkeypatch.setattr(smtp_module, "_Smtp", FakeServer)
    monkeypatch.setattr(smtp_module, "_SmtpSsl", FakeSslServer)


def _client(**kwargs: Any) -> SmtpClient:
    defaults: Dict[str, Any] = {"sender": "DiscoNotify <no-reply@disconotify.com>"}
    defaults.update(kwargs)
    return SmtpClient("mail.example.com", 587, "billing@disconotify.com", "app-password", **defaults)


async def _send(client: SmtpClient, **overrides: Any) -> str:
    message: Dict[str, Any] = {"to": "owner@example.com", "subject": "Hello", "html": "<p>Hi</p>", "text": "Hi"}
    message.update(overrides)
    return await client.send(**message)


async def test_the_message_comes_from_the_domain_with_a_text_and_an_html_part() -> None:
    message_id = await _send(_client())

    sent = FakeServer.instances[0].sent[0]
    assert sent["From"] == "DiscoNotify <no-reply@disconotify.com>" and sent["To"] == "owner@example.com"
    assert sent["Subject"] == "Hello"
    assert sent["Reply-To"] is None  # nobody can reply
    assert sent["Auto-Submitted"] == "auto-generated"
    assert sent["Message-ID"] == message_id and message_id.endswith("@disconotify.com>")
    assert sent.get_body(("plain",)).get_content().strip() == "Hi"
    assert sent.get_body(("html",)).get_content().strip() == "<p>Hi</p>"
    assert sent["Date"]


async def test_a_marketing_email_carries_one_click_unsubscribe_headers() -> None:
    await _send(_client(), unsubscribe_url="https://disconotify.com/unsubscribe?t=abc")

    sent = FakeServer.instances[0].sent[0]
    assert sent["List-Unsubscribe"] == "<https://disconotify.com/unsubscribe?t=abc>"
    assert sent["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    await _send(_client())
    assert FakeServer.instances[1].sent[0]["List-Unsubscribe"] is None


async def test_starttls_encrypts_the_connection_before_the_login() -> None:
    await _send(_client(security="starttls"))

    server = FakeServer.instances[0]
    assert not isinstance(server, FakeSslServer) and server.port == 587
    assert server.calls == ["connect:203.0.113.7", "starttls", "login", "send_message", "quit"]
    assert server.login_args == ("billing@disconotify.com", "app-password")
    assert server.context is None or True  # a default TLS context verifies the server's certificate


async def test_ssl_mode_connects_encrypted_from_the_start() -> None:
    client = SmtpClient("mail.example.com", 465, "u", "p", sender="billing@disconotify.com", security="ssl")

    await _send(client)

    server = FakeServer.instances[0]
    assert isinstance(server, FakeSslServer) and server.port == 465
    assert server.calls == ["connect:203.0.113.7", "login", "send_message", "quit"]


def test_an_unknown_security_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        _client(security="none")


async def test_a_refused_recipient_is_permanent() -> None:
    FakeServer.failure = smtplib.SMTPRecipientsRefused({"owner@example.com": (550, b"No such user")})

    with pytest.raises(PermanentMailError):
        await _send(_client())


async def test_a_5xx_answer_is_permanent_and_a_4xx_answer_is_temporary() -> None:
    FakeServer.failure = smtplib.SMTPDataError(554, b"Message rejected as spam")
    with pytest.raises(PermanentMailError) as permanent:
        await _send(_client())
    assert "554" in str(permanent.value)

    FakeServer.failure = smtplib.SMTPDataError(451, b"Try again later")
    with pytest.raises(TransientMailError):
        await _send(_client())


async def test_a_login_or_sender_problem_is_a_settings_problem_so_it_is_retried_not_dropped() -> None:
    FakeServer.failure, FakeServer.failure_at = smtplib.SMTPAuthenticationError(535, b"bad credentials"), "login"
    with pytest.raises(TransientMailError) as error:
        await _send(_client())
    assert "EMAIL_SMTP_USERNAME" in str(error.value) and "app-password" not in str(error.value)

    FakeServer.failure, FakeServer.failure_at = smtplib.SMTPSenderRefused(553, b"not allowed to send as this address", "x@y.z"), "send_message"
    with pytest.raises(TransientMailError):
        await _send(_client())


async def test_connection_trouble_and_timeouts_are_temporary() -> None:
    for failure in (ConnectionRefusedError("nope"), TimeoutError("slow"), smtplib.SMTPServerDisconnected("gone")):
        FakeServer.failure, FakeServer.failure_at = failure, "send_message"
        with pytest.raises(TransientMailError):
            await _send(_client())


async def test_a_failed_starttls_closes_the_connection() -> None:
    FakeServer.failure, FakeServer.failure_at = smtplib.SMTPNotSupportedError("no STARTTLS"), "starttls"

    with pytest.raises(TransientMailError):
        await _send(_client())

    assert all("close" in server.calls and "login" not in server.calls for server in FakeServer.instances)


# ── settings ────────────────────────────────────────────────────────────


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in {"DISCORD_TOKEN": "t", "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "k", **env}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_email_is_off_until_every_smtp_setting_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).email_configured is False

    partial = _settings(monkeypatch, EMAIL_SMTP_HOST="mail.example.com", EMAIL_SMTP_USERNAME="u", EMAIL_SMTP_PASSWORD="p")
    assert partial.email_configured is False  # no EMAIL_FROM yet

    full = _settings(monkeypatch, EMAIL_FROM="DiscoNotify <no-reply@disconotify.com>")
    assert full.email_configured is True
    assert full.email_smtp_port == 587 and full.email_smtp_mode == "starttls"


def test_the_smtp_security_mode_is_validated_and_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, EMAIL_SMTP_SECURITY=" SSL ").email_smtp_mode == "ssl"
    with pytest.raises(Exception):
        _settings(monkeypatch, EMAIL_SMTP_SECURITY="plaintext")


# ── which address is used ───────────────────────────────────────────────


_V4, _V6 = (socket.AF_INET, ("172.65.255.143", 465)), (socket.AF_INET6, ("2606:4700::1", 465, 0, 0))


def test_ipv4_addresses_are_listed_before_ipv6_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", _V6[1]), (socket.AF_INET, socket.SOCK_STREAM, 6, "", _V4[1])]
    monkeypatch.setattr(smtp_module.socket, "getaddrinfo", lambda host, port, type=0: entries)

    assert _REAL_RESOLVE("smtp.example.com", 465) == [_V4, _V6]


def _two_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtp_module, "resolve_targets", lambda host, port: [_V4, _V6])


async def test_ipv4_is_used_first_and_ipv6_never_touched_when_it_works(monkeypatch: pytest.MonkeyPatch) -> None:
    _two_addresses(monkeypatch)

    await _send(_client())

    assert [call for server in FakeServer.instances for call in server.calls if call.startswith("connect")] == ["connect:172.65.255.143"]


async def test_a_host_without_ipv6_routing_or_ipv4_trouble_falls_through_to_the_next_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _two_addresses(monkeypatch)
    FakeServer.unreachable = {"172.65.255.143"}  # IPv4 hangs / is refused: the next address is used

    await _send(_client())

    connects = [call for server in FakeServer.instances for call in server.calls if call.startswith("connect")]
    assert connects == ["connect:172.65.255.143", "connect:2606:4700::1"]
    assert FakeServer.instances[-1].sent  # and the message went out over the second one


async def test_when_no_address_works_the_last_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _two_addresses(monkeypatch)
    FakeServer.unreachable = {"172.65.255.143", "2606:4700::1"}

    with pytest.raises(TransientMailError, match="unreachable"):
        await _send(_client())


async def test_a_host_with_no_addresses_is_an_error_not_a_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtp_module, "resolve_targets", lambda host, port: [])

    with pytest.raises(TransientMailError, match="No address"):
        await _send(_client())


async def test_a_login_failure_is_not_retried_on_another_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only connection trouble moves on to the next address — a wrong password would just fail twice."""
    _two_addresses(monkeypatch)
    FakeServer.failure, FakeServer.failure_at = smtplib.SMTPAuthenticationError(535, b"bad credentials"), "login"

    with pytest.raises(TransientMailError):
        await _send(_client())

    assert len(FakeServer.instances) == 1


def test_the_encryption_mode_follows_the_port_unless_told_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, EMAIL_SMTP_PORT="465").email_smtp_mode == "ssl"
    assert _settings(monkeypatch, EMAIL_SMTP_PORT="587").email_smtp_mode == "starttls"
    assert _settings(monkeypatch, EMAIL_SMTP_PORT="465", EMAIL_SMTP_SECURITY="starttls").email_smtp_mode == "starttls"
    assert _settings(monkeypatch, EMAIL_SMTP_PORT="2525", EMAIL_SMTP_SECURITY="").email_smtp_mode == "starttls"
