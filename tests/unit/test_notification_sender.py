"""NotificationSender: alert-config gating, embed-vs-plain-text, mention
scoping (section 36), and marking a channel invalid rather than retrying
forever when Discord says it's gone (section 23)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import discord
import pytest

from database.repositories.alert_configs import AlertConfigurationRepository
from database.repositories.channels import NotificationChannelRepository
from events.models import EventType, NormalizedEvent
from notifications.sender import NotificationSender
from tests.unit.fakes import FakeDatabase


def _fake_http_exception(cls, status: int = 404, reason: str = "Not Found") -> discord.HTTPException:
    response = type("_FakeResponse", (), {"status": status, "reason": reason})()
    return cls(response, "")


class FakeWebhook:
    """Sends land in the owning channel's `.sent` list, same as a direct
    channel.send() — from the test's perspective, "what actually reached
    the channel" is what matters, regardless of which path delivered it."""

    def __init__(self, url: str, channel: "FakeDiscordChannel") -> None:
        self.url = url
        self._channel = channel

    async def send(self, *, content=None, embed=None, allowed_mentions=None, suppress_embeds=False, username=None):
        self._channel.webhook_send_calls += 1
        if self.url in self._channel.deleted_webhook_urls:
            raise _fake_http_exception(discord.NotFound, status=404)
        if self._channel.raise_on_webhook_send:
            raise self._channel.raise_on_webhook_send
        self._channel.sent.append(
            {
                "content": content,
                "embed": embed,
                "allowed_mentions": allowed_mentions,
                "suppress_embeds": suppress_embeds,
                "username": username,
            }
        )

    async def fetch(self):
        self._channel.webhook_fetch_calls += 1
        if self.url in self._channel.deleted_webhook_urls:
            raise _fake_http_exception(discord.NotFound, status=404)
        if self._channel.raise_on_webhook_fetch:
            raise self._channel.raise_on_webhook_fetch
        return self


class FakeDiscordChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        raise_on_send: Optional[BaseException] = None,
        raise_on_create_webhook: Optional[BaseException] = None,
        raise_on_webhook_send: Optional[BaseException] = None,
        raise_on_webhook_fetch: Optional[BaseException] = None,
    ) -> None:
        self.id = channel_id
        self.raise_on_send = raise_on_send
        self.raise_on_create_webhook = raise_on_create_webhook
        self.raise_on_webhook_send = raise_on_webhook_send
        self.raise_on_webhook_fetch = raise_on_webhook_fetch
        self.sent: List[Dict[str, Any]] = []
        self.create_webhook_calls = 0
        self.webhook_fetch_calls = 0
        self.webhook_send_calls = 0
        self.deleted_webhook_urls: set = set()
        self._created_webhook_urls: List[str] = []

    def delete_webhooks(self) -> None:
        """Simulates a server admin deleting every existing webhook."""
        self.deleted_webhook_urls.update(self._created_webhook_urls)

    async def send(self, *, content=None, embed=None, allowed_mentions=None, suppress_embeds=False):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent.append(
            {
                "content": content,
                "embed": embed,
                "allowed_mentions": allowed_mentions,
                "suppress_embeds": suppress_embeds,
                "username": None,
            }
        )

    async def create_webhook(self, *, name: str):
        self.create_webhook_calls += 1
        if self.raise_on_create_webhook:
            raise self.raise_on_create_webhook
        suffix = "" if self.create_webhook_calls == 1 else str(self.create_webhook_calls)
        url = f"https://discord.com/api/webhooks/{self.id}/faketoken{suffix}"
        self._created_webhook_urls.append(url)
        return FakeWebhook(url, self)


class FakeBot:
    def __init__(self) -> None:
        self._cached: Dict[int, FakeDiscordChannel] = {}
        self._fetchable: Dict[int, FakeDiscordChannel] = {}

    def cache(self, channel: FakeDiscordChannel) -> None:
        self._cached[channel.id] = channel

    def make_fetch_only(self, channel: FakeDiscordChannel) -> None:
        """Simulates a channel not in the gateway cache, requiring fetch_channel()."""
        self._fetchable[channel.id] = channel

    def get_channel(self, channel_id: int):
        return self._cached.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        if channel_id in self._fetchable:
            return self._fetchable[channel_id]
        raise _fake_http_exception(discord.NotFound)


class Harness:
    def __init__(self) -> None:
        self.db = FakeDatabase()
        self.alert_repo = AlertConfigurationRepository(self.db)  # type: ignore[arg-type]
        self.channel_repo = NotificationChannelRepository(self.db)  # type: ignore[arg-type]
        self.bot = FakeBot()
        self.sender = NotificationSender(  # type: ignore[arg-type]
            self.bot, self.alert_repo, self.channel_repo, webhook_from_url=self._fake_webhook_from_url
        )

    def _fake_webhook_from_url(self, url: str, *, client=None) -> FakeWebhook:
        # The fake URL format is ".../webhooks/{channel_id}/faketoken" —
        # parse the channel id back out to find which fake channel this
        # (persisted, then re-loaded) webhook belongs to.
        channel_id = int(url.rsplit("/", 2)[-2])
        channel = self.bot._cached.get(channel_id) or self.bot._fetchable.get(channel_id)
        assert channel is not None, f"no fake channel registered for webhook url {url}"
        return FakeWebhook(url, channel)  # type: ignore[return-value]

    async def add_channel(self, guild_id: int, account_id: str, discord_channel_id: int, **channel_kwargs: Any) -> FakeDiscordChannel:
        row = await self.channel_repo.get_or_create(guild_id, discord_channel_id)
        await self.channel_repo.link_account(guild_id, account_id, row["id"])
        channel = FakeDiscordChannel(discord_channel_id, **channel_kwargs)
        self.bot.cache(channel)
        return channel


def _event(**overrides) -> NormalizedEvent:
    fields = dict(
        guild_id=1,
        account_id="acc-1",
        platform="youtube",
        platform_account_id="UCabc",
        account_username="Example",
        event_type=EventType.VIDEO_PUBLISHED,
        platform_event_id="vid-1",
        title="A video",
        url="https://youtube.com/watch?v=vid-1",
    )
    fields.update(overrides)
    return NormalizedEvent(**fields)


@pytest.fixture
def harness() -> Harness:
    return Harness()


async def test_youtube_alert_gets_a_rich_embed_by_default(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)

    await harness.sender.handle(_event(description="Watch this", author="Example Channel"))

    sent = channel.sent[0]
    assert sent["embed"] is not None
    assert sent["embed"].title == "A video"
    assert sent["embed"].fields[0].value == "Watch this"
    # The link is wrapped so Discord doesn't add its own preview under the embed.
    assert "<https://youtube.com/watch?v=vid-1>" in sent["content"]
    assert "Example Channel" in sent["content"]
    assert sent["suppress_embeds"] is False


async def test_embed_disabled_sends_plain_text_with_no_preview(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["embed_enabled"] = False

    await harness.sender.handle(_event())

    sent = channel.sent[0]
    assert sent["embed"] is None
    assert "https://youtube.com/watch?v=vid-1" in sent["content"]
    assert sent["suppress_embeds"] is True


async def test_platform_without_a_rich_embed_sends_plain_text_with_link_preview(harness: Harness) -> None:
    """Platforms whose events lack the data for a full embed rely on
    Discord's own link preview instead (see notifications/embeds.py)."""
    channel = await harness.add_channel(1, "acc-1", 555)

    await harness.sender.handle(
        _event(platform="kick", event_type=EventType.STREAM_STARTED, url="https://kick.com/example", title="A video")
    )

    assert len(channel.sent) == 1
    sent = channel.sent[0]
    assert sent["embed"] is None
    assert "A video" in sent["content"]
    assert "https://kick.com/example" in sent["content"]
    assert "<https://kick.com/example>" not in sent["content"]
    assert sent["suppress_embeds"] is False  # embed_enabled=True -> allow the native preview


async def test_disabled_event_type_sends_nothing(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    # Flip it off directly in the fake DB (no /alerts command exists yet).
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["enabled"] = False

    await harness.sender.handle(_event())

    assert channel.sent == []


async def test_embed_disabled_suppresses_the_native_link_preview(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["embed_enabled"] = False

    await harness.sender.handle(_event())

    sent = channel.sent[0]
    assert sent["embed"] is None
    assert "A video" in sent["content"]
    assert sent["suppress_embeds"] is True


async def test_mention_role_is_included_and_scoped_to_that_role_only(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["mention_role_id"] = 999

    await harness.sender.handle(_event())

    sent = channel.sent[0]
    assert "<@&999>" in sent["content"]
    allowed = sent["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.users is False


async def test_everyone_role_is_sent_as_a_real_everyone_ping(harness: Harness) -> None:
    """The @everyone role's ID is the guild's ID; `<@&guild_id>` renders as
    a dead "@@everyone", so it must go out as literal @everyone instead."""
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["mention_role_id"] = 1

    await harness.sender.handle(_event())

    sent = channel.sent[0]
    assert sent["content"].startswith("@everyone ")
    assert "<@&1>" not in sent["content"]
    assert sent["allowed_mentions"].everyone is True
    assert sent["allowed_mentions"].roles is False


# ── custom_message (section 35) ────────────────────────────────────────


async def test_custom_message_replaces_the_default_content(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["custom_message"] = (
        "Hey @everyone, {creator} just posted a new post! Go check it out!\n{url}"
    )

    await harness.sender.handle(_event())

    sent = channel.sent[0]
    assert sent["content"] == (
        "Hey @everyone, Example just posted a new post! Go check it out!\n"
        "<https://youtube.com/watch?v=vid-1>"
    )


async def test_custom_message_keeps_the_rich_embed_without_a_duplicate_preview(harness: Harness) -> None:
    """The template's {url} is wrapped in <...> when the message carries
    its own embed, so Discord doesn't add a second preview under it."""
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["custom_message"] = "Check this out!\n{url}"
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["embed_enabled"] = True

    await harness.sender.handle(_event())

    sent = channel.sent[0]
    assert sent["content"] == "Check this out!\n<https://youtube.com/watch?v=vid-1>"
    assert sent["embed"] is not None
    assert sent["suppress_embeds"] is False


async def test_custom_message_with_embed_disabled_suppresses_the_native_link_preview(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["custom_message"] = "Check this out!\n{url}"
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["embed_enabled"] = False

    await harness.sender.handle(_event())

    sent = channel.sent[0]
    assert sent["embed"] is None
    assert sent["suppress_embeds"] is True


async def test_custom_messages_literal_everyone_actually_allows_the_mention(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["custom_message"] = "Hey @everyone!"

    await harness.sender.handle(_event())

    assert channel.sent[0]["allowed_mentions"].everyone is True


async def test_custom_message_without_everyone_does_not_allow_the_mention(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["custom_message"] = "New post from {creator}"

    await harness.sender.handle(_event())

    assert channel.sent[0]["allowed_mentions"].everyone is False


async def test_custom_message_title_cannot_inject_a_real_everyone_mention(harness: Harness) -> None:
    """A malicious/weird video title containing literal "@everyone" text
    must not be able to trigger a real mention just by being substituted
    into the template — only the admin's own typed text can do that."""
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["custom_message"] = "New post: {title}"

    await harness.sender.handle(_event(title="please @everyone watch this"))

    sent = channel.sent[0]
    assert "@everyone" not in sent["content"]
    assert sent["allowed_mentions"].everyone is False


async def test_custom_message_mention_placeholder_uses_the_configured_role(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.alert_repo.seed_defaults(1, "acc-1", ["video_published"])
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["custom_message"] = "{mention} new post!"
    harness.db.tables["pulsenotify_alert_configurations"].rows[0]["mention_role_id"] = 999999999999999999

    await harness.sender.handle(_event())

    sent = channel.sent[0]
    assert "<@&999999999999999999>" in sent["content"]
    assert sent["allowed_mentions"].roles == [discord.Object(id=999999999999999999)]


async def test_no_channels_configured_sends_nothing_and_does_not_raise(harness: Harness) -> None:
    await harness.sender.handle(_event())  # no add_channel() call at all


async def test_sends_to_every_linked_channel(harness: Harness) -> None:
    channel_a = await harness.add_channel(1, "acc-1", 111)
    channel_b = await harness.add_channel(1, "acc-1", 222)

    await harness.sender.handle(_event())

    assert len(channel_a.sent) == 1
    assert len(channel_b.sent) == 1


async def test_forbidden_on_send_marks_the_channel_invalid(harness: Harness) -> None:
    """Exercises the direct-send fallback path (webhook creation itself
    failing here for an unrelated reason) — the channel-invalid handling
    must still work the same regardless of which path actually sent."""
    row = await harness.channel_repo.get_or_create(1, 555)
    await harness.channel_repo.link_account(1, "acc-1", row["id"])
    channel = FakeDiscordChannel(
        555,
        raise_on_create_webhook=_fake_http_exception(discord.HTTPException, status=500),
        raise_on_send=_fake_http_exception(discord.Forbidden, status=403),
    )
    harness.bot.cache(channel)

    await harness.sender.handle(_event())  # must not raise

    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None
    assert updated["is_valid"] is False


async def test_not_found_on_send_marks_the_channel_invalid(harness: Harness) -> None:
    row = await harness.channel_repo.get_or_create(1, 555)
    await harness.channel_repo.link_account(1, "acc-1", row["id"])
    channel = FakeDiscordChannel(
        555,
        raise_on_create_webhook=_fake_http_exception(discord.HTTPException, status=500),
        raise_on_send=_fake_http_exception(discord.NotFound, status=404),
    )
    harness.bot.cache(channel)

    await harness.sender.handle(_event())  # must not raise

    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None
    assert updated["is_valid"] is False


async def test_generic_http_exception_on_send_does_not_mark_invalid(harness: Harness) -> None:
    row = await harness.channel_repo.get_or_create(1, 555)
    await harness.channel_repo.link_account(1, "acc-1", row["id"])
    channel = FakeDiscordChannel(
        555,
        raise_on_create_webhook=_fake_http_exception(discord.HTTPException, status=500),
        raise_on_send=_fake_http_exception(discord.HTTPException, status=500),
    )
    harness.bot.cache(channel)

    await harness.sender.handle(_event())  # must not raise

    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None
    assert updated["is_valid"] is True  # a transient server error isn't "gone"


# ── webhook delivery (section: notifications appear as the posting account) ──


async def test_first_notification_creates_and_persists_a_webhook(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)

    await harness.sender.handle(_event())

    assert channel.create_webhook_calls == 1
    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None
    assert updated["webhook_url"] == f"https://discord.com/api/webhooks/555/faketoken"


async def test_second_notification_reuses_the_persisted_webhook(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)

    await harness.sender.handle(_event())
    await harness.sender.handle(_event(platform_event_id="vid-2"))

    assert channel.create_webhook_calls == 1  # not once per notification
    assert len(channel.sent) == 2


async def test_webhook_send_never_overrides_the_webhooks_own_name_or_avatar(harness: Harness) -> None:
    """Regression test: sending used to pass username=account_username on
    every webhook.send() call, which silently stomped any name/avatar an
    admin had customized on the webhook itself in Discord's own UI (e.g. to
    match their server's branding) back to the raw platform account name."""
    channel = await harness.add_channel(1, "acc-1", 555)

    await harness.sender.handle(_event(account_username="SomeCreator"))

    assert channel.sent[0]["username"] is None


async def test_missing_manage_webhooks_permission_falls_back_to_sending_as_the_bot(harness: Harness) -> None:
    row = await harness.channel_repo.get_or_create(1, 555)
    await harness.channel_repo.link_account(1, "acc-1", row["id"])
    channel = FakeDiscordChannel(
        555, raise_on_create_webhook=_fake_http_exception(discord.Forbidden, status=403)
    )
    harness.bot.cache(channel)

    await harness.sender.handle(_event())

    assert len(channel.sent) == 1
    assert channel.sent[0]["username"] is None  # delivered as the bot, not a webhook identity
    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None
    assert updated["webhook_url"] is None  # never persisted since creation failed


async def test_webhook_deleted_externally_before_any_send_is_recreated(harness: Harness) -> None:
    """Regression test: if a webhook is deleted from Discord's side (by a
    server admin, or some unrelated automation/integration) before
    PulseNotify ever tries to actually send through it — e.g. between
    get_or_create_webhook() being called proactively from account-add and
    the first real notification — the stale stored URL must not be handed
    back as if it were still good. Previously get_or_create_webhook()
    trusted a stored URL unconditionally, so nothing new ever got created
    once the original webhook was gone."""
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.sender.get_or_create_webhook(
        await harness.channel_repo.get_by_discord_channel(1, 555), channel
    )
    assert channel.create_webhook_calls == 1

    # Simulate external deletion: the stored URL now points at nothing.
    channel.delete_webhooks()

    await harness.sender.handle(_event())

    assert channel.create_webhook_calls == 2  # a fresh webhook was actually created
    assert len(channel.sent) == 1  # and the notification still went out through it
    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None and updated["webhook_url"].endswith("faketoken2")


async def test_account_add_style_lookup_verifies_a_stored_webhook(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    row = await harness.channel_repo.get_by_discord_channel(1, 555)
    await harness.sender.get_or_create_webhook(row, channel)
    channel.delete_webhooks()

    await harness.sender.get_or_create_webhook(await harness.channel_repo.get_by_discord_channel(1, 555), channel)

    assert channel.create_webhook_calls == 2


async def test_sending_does_not_spend_a_round_trip_verifying_the_webhook(harness: Harness) -> None:
    """Every alert used to call webhook.fetch() before sending — an extra
    Discord API round trip on the delivery path, for something the send
    itself already reports."""
    channel = await harness.add_channel(1, "acc-1", 555)

    await harness.sender.handle(_event())
    await harness.sender.handle(_event(platform_event_id="vid-2"))

    assert channel.webhook_fetch_calls == 0
    assert len(channel.sent) == 2


async def test_deleted_webhook_is_replaced_and_the_alert_goes_through_the_new_one(harness: Harness) -> None:
    """Simulates a server admin deleting the webhook from Discord's side
    sometime after PulseNotify created and persisted it."""
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.sender.handle(_event())  # creates + persists the webhook normally
    assert channel.create_webhook_calls == 1

    channel.delete_webhooks()
    await harness.sender.handle(_event(platform_event_id="vid-2"))  # must not raise or drop the notification

    assert len(channel.sent) == 2
    assert channel.create_webhook_calls == 2
    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None and updated["webhook_url"].endswith("faketoken2")


async def test_webhook_not_found_twice_falls_back_to_sending_as_the_bot(harness: Harness) -> None:
    channel = await harness.add_channel(1, "acc-1", 555)
    await harness.sender.handle(_event())

    channel.raise_on_webhook_send = _fake_http_exception(discord.NotFound, status=404)
    await harness.sender.handle(_event(platform_event_id="vid-2"))

    assert len(channel.sent) == 2  # second one via the direct-send fallback
    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None and updated["webhook_url"] is None  # the next alert recreates one


async def test_channel_not_in_cache_falls_back_to_fetch(harness: Harness) -> None:
    row = await harness.channel_repo.get_or_create(1, 555)
    await harness.channel_repo.link_account(1, "acc-1", row["id"])
    channel = FakeDiscordChannel(555)
    harness.bot.make_fetch_only(channel)  # not cached — forces fetch_channel()

    await harness.sender.handle(_event())

    assert len(channel.sent) == 1


async def test_channel_that_cannot_be_fetched_is_marked_invalid(harness: Harness) -> None:
    row = await harness.channel_repo.get_or_create(1, 555)
    await harness.channel_repo.link_account(1, "acc-1", row["id"])
    # Neither cached nor fetchable -> FakeBot.fetch_channel raises NotFound.

    await harness.sender.handle(_event())  # must not raise

    updated = await harness.channel_repo.get_by_discord_channel(1, 555)
    assert updated is not None
    assert updated["is_valid"] is False
