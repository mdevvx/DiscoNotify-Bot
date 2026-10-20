"""_format_usage: the parameter-hint string shown in /notify help,
derived from a command's real parameters so it can't drift out of sync
with what a subcommand actually accepts.

Uses lightweight duck-typed stand-ins rather than real app_commands.Command
instances — _format_usage only ever reads `.name` and
`.parameters[].name`/`.required`, and constructing a genuine Command
correctly involves discord.py internals unrelated to what's under test
here (end-to-end registration against the real command objects is already
covered by the manual verification in bot/client.py's wiring)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from commands.admin.help import _format_usage


@dataclass
class _FakeParam:
    name: str
    required: bool


@dataclass
class _FakeCommand:
    name: str
    parameters: List[_FakeParam] = field(default_factory=list)


def test_no_parameters_returns_empty_usage() -> None:
    command = _FakeCommand(name="account-list")
    assert _format_usage(command) == ""  # type: ignore[arg-type]


def test_required_parameter_shown_without_brackets() -> None:
    command = _FakeCommand(name="account-remove", parameters=[_FakeParam("account", required=True)])
    assert _format_usage(command) == "/notify account-remove account"  # type: ignore[arg-type]


def test_optional_parameter_shown_in_brackets() -> None:
    command = _FakeCommand(
        name="account-set-message",
        parameters=[
            _FakeParam("account", required=True),
            _FakeParam("message", required=True),
            _FakeParam("event_type", required=False),
        ],
    )
    assert (
        _format_usage(command)  # type: ignore[arg-type]
        == "/notify account-set-message account message [event_type]"
    )
