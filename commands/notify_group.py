"""The single top-level `/notify` command group every slash command
attaches to (e.g. `/notify toggle`, `/notify status`).

One shared module-level Group instance — commands/admin/status.py,
commands/admin/toggle.py, and commands/accounts/cog.py each decorate their
command against THIS object rather than defining their own top-level
`@app_commands.command()`. bot/client.py registers it to the tree exactly
once (`tree.add_command(notify_group)`), after every extension that
contributes a subcommand has been loaded.

Most commands are direct subcommands (`/notify account-add`). Discord
also allows subcommand groups alongside them (verified in Discord's docs
2026-10-13 — an earlier note here wrongly said mixing wasn't allowed), and
`/notify global status` uses one: commands/admin/status.py's
`global_group`. Only one level of grouping is possible:
/notify <group> <subcommand>.
"""

from __future__ import annotations

from discord import app_commands

notify_group = app_commands.Group(
    name="notify",
    description="DiscoNotify — monitor social/streaming accounts and post alerts to this server.",
    guild_only=True,
)

# `/notify global …` — bot-owner commands that span every server
# (global status, whitelist). Defined here, next to the parent, so every
# module that adds to it imports one shared object.
global_group = app_commands.Group(
    name="global", description="Bot owner only: DiscoNotify across every server.", parent=notify_group
)
