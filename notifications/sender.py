"""The real EventProcessor notification sink: looks up who wants to know
about an event and how, then sends it — respecting AllowedMentions
(section 36) and marking a channel invalid rather than retrying forever if
Discord says it's gone (section 23).

Alerts are sent through a per-channel Discord webhook rather than the bot
posting as itself. The webhook is created once per channel (lazily, on the
first notification sent there, named after this bot by default) and its
URL is persisted on the channel row (migration 0004) so it's reused after
that instead of accumulating a new webhook per message. Deliberately never
overridden with a per-message `username`/`avatar_url` after that: an admin
can rename it and give it a custom avatar in Discord's own channel
integration settings (e.g. to match their server's branding), and every
future notification must keep respecting that instead of stomping it back
to whatever the raw platform account's name is on every send. If the bot
lacks "Manage Webhooks" in a channel, or webhook delivery fails for any
reason, this falls back to sending as the bot itself rather than dropping
the notification — a missing permission in one channel must not silently
swallow alerts.

Images that aren't ready yet: alerts now go out within seconds of an
upload, often before YouTube has generated the video's thumbnail — its URL
already exists but serves a 404 placeholder, which Discord shows as a
broken image (seen 2026-10-12). So the thumbnail URL is checked first; if
it isn't serving an image yet, the alert is sent without it right away and
a background task edits the image in once it appears (an edit never
re-pings). The alert is never held back waiting for it.

Discord-side rate limiting is handled by discord.py's own HTTP layer;
EventProcessor's single-consumer queue already serializes delivery (one
event handled at a time), which is what keeps this from ever firing a
burst of simultaneous sends — see events/processor.py's docstring for why
no separate notification queue was built on top of that.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import discord
import httpx

from config import constants
from database.repositories.alert_configs import AlertConfigurationRepository
from database.repositories.channels import NotificationChannelRepository
from events.models import NormalizedEvent
from notifications.cards import build_alert, build_headline, build_plain_text
from notifications.templates import format_role_mention, render_custom_message
from utils.logger import get_logger

logger = get_logger(__name__)

WebhookFromURL = Callable[..., discord.Webhook]
ImageReady = Callable[[str], Awaitable[bool]]

_HEAD_UNSUPPORTED_STATUSES = frozenset({400, 403, 405, 501})

# TikTok's signed cover links don't render when Discord fetches them itself
# (the card shows no image), so the cover is downloaded and uploaded with the
# alert instead, and the card points at the attachment.
_ATTACHED_COVER_PLATFORMS = frozenset({"tiktok"})
_COVER_FILENAME = "cover.jpg"
_COVER_MAX_BYTES = 8 * 1024 * 1024

# How often, and for how long, to re-check a thumbnail that wasn't ready
# when its alert went out. YouTube can take several minutes to generate
# one for a fresh upload.
_IMAGE_RETRY_INTERVAL_SECONDS = 30.0
_IMAGE_RETRY_MAX_WAIT_SECONDS = 30 * 60.0
_IMAGE_CHECK_TIMEOUT_SECONDS = 5.0


def webhook_notice(channel: Any) -> str:
    return (
        f"🪝 I created a webhook in {channel.mention}. Open **Channel settings → Integrations → Webhooks** "
        f"and change its name and logo to match your server's branding — alerts there will use them."
    )


class NotificationSender:
    def __init__(
        self,
        bot: discord.Client,
        alert_config_repo: AlertConfigurationRepository,
        channel_repo: NotificationChannelRepository,
        *,
        webhook_from_url: WebhookFromURL = discord.Webhook.from_url,
        image_ready: Optional[ImageReady] = None,
        image_retry_interval_seconds: float = _IMAGE_RETRY_INTERVAL_SECONDS,
        image_retry_max_wait_seconds: float = _IMAGE_RETRY_MAX_WAIT_SECONDS,
    ) -> None:
        self._bot = bot
        self._alert_config_repo = alert_config_repo
        self._channel_repo = channel_repo
        # Overridable purely for tests (a fake that doesn't need a real
        # Discord webhook URL to construct a usable object) — production
        # code never passes this.
        self._webhook_from_url = webhook_from_url
        self._http: Optional[httpx.AsyncClient] = None
        self._image_ready: ImageReady = image_ready or self._check_image
        self._image_retry_interval = image_retry_interval_seconds
        self._image_retry_max_wait = image_retry_max_wait_seconds
        # Strong references, so pending image follow-ups aren't garbage
        # collected mid-wait; cancelled on aclose().
        self._image_tasks: Set["asyncio.Task[None]"] = set()

    async def aclose(self) -> None:
        for task in list(self._image_tasks):
            task.cancel()
        await asyncio.gather(*self._image_tasks, return_exceptions=True)
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def handle(self, event: NormalizedEvent) -> None:
        assert event.guild_id is not None and event.account_id is not None

        config = await self._alert_config_repo.get(event.account_id, event.event_type.value)
        if config is not None and not config["enabled"]:
            return

        channels = await self._channel_repo.list_valid_for_account(event.account_id)
        if not channels:
            logger.debug(
                "No notification channels configured for account %s (guild %s); dropping %s event.",
                event.account_id,
                event.guild_id,
                event.event_type.value,
            )
            return

        embed_enabled = config["embed_enabled"] if config else True
        mention_role_id = config["mention_role_id"] if config else None
        custom_message = config["custom_message"] if config else None
        top_line = self._top_line(
            event, embed_enabled=embed_enabled, mention_role_id=mention_role_id, custom_message=custom_message
        )
        # Computed from the top line only: that's the one place a mention
        # is meant to be; the card's platform text is mention-escaped.
        allowed_mentions = self._allowed_mentions_for(top_line, mention_role_id, event.guild_id)

        image_pending = bool(embed_enabled and event.thumbnail_url) and not await self._image_ready(
            event.thumbnail_url  # type: ignore[arg-type]
        )
        shown = dataclasses.replace(event, thumbnail_url=None) if image_pending else event
        cover: Optional[bytes] = None
        if shown.thumbnail_url and embed_enabled and event.platform in _ATTACHED_COVER_PLATFORMS:
            cover = await self._download_image(shown.thumbnail_url)
            shown = dataclasses.replace(shown, thumbnail_url=f"attachment://{_COVER_FILENAME}" if cover else None)
        layout = build_alert(shown, top_line=top_line, rich=embed_enabled)

        sent = []
        for channel_row in channels:
            message = await self._send_to_channel(
                channel_row, layout=layout, allowed_mentions=allowed_mentions, cover=cover
            )
            if message is not None:
                sent.append(message)

        if image_pending and sent:
            task = asyncio.create_task(self._add_image_when_ready(event, top_line, sent))
            self._image_tasks.add(task)
            task.add_done_callback(self._image_tasks.discard)

    async def _add_image_when_ready(self, event: NormalizedEvent, top_line: str, messages: List[Any]) -> None:
        url = event.thumbnail_url
        assert url is not None
        waited = 0.0
        while waited < self._image_retry_max_wait:
            await asyncio.sleep(self._image_retry_interval)
            waited += self._image_retry_interval
            if not await self._image_ready(url):
                continue
            layout = build_alert(event, top_line=top_line, rich=True)
            for message in messages:
                try:
                    await message.edit(view=layout)
                except discord.HTTPException:
                    logger.warning("Couldn't add the thumbnail to alert message %s.", getattr(message, "id", "?"))
            return
        logger.info("Thumbnail %s never became available; the alert stays without it.", url)

    async def _check_image(self, url: str) -> bool:
        """Whether `url` serves an image right now. YouTube answers a not-yet-
        generated thumbnail with HTTP 404 (and a placeholder image body)."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_IMAGE_CHECK_TIMEOUT_SECONDS, follow_redirects=True)
        try:
            response = await self._http.head(url)
            if response.status_code in _HEAD_UNSUPPORTED_STATUSES:
                # Some CDNs (TikTok's) reject HEAD but serve the image on GET.
                async with self._http.stream("GET", url) as streamed:
                    response = streamed
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and response.headers.get("content-type", "").startswith("image/")

    @staticmethod
    def _cover_file(cover: bytes) -> discord.File:
        return discord.File(io.BytesIO(cover), filename=_COVER_FILENAME)

    async def _download_image(self, url: str) -> Optional[bytes]:
        """The image's bytes, or None if it can't be fetched (the alert then
        goes out without it)."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_IMAGE_CHECK_TIMEOUT_SECONDS, follow_redirects=True)
        try:
            response = await self._http.get(url)
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
            return None
        return response.content if len(response.content) <= _COVER_MAX_BYTES else None

    def _top_line(
        self,
        event: NormalizedEvent,
        *,
        embed_enabled: bool,
        mention_role_id: Optional[int],
        custom_message: Optional[str],
    ) -> str:
        """The text above the alert card (see notifications/cards.py) — the
        whole alert when embeds are off for this alert type."""
        if custom_message:
            return render_custom_message(custom_message, event, mention_role_id=mention_role_id)

        mention_text = format_role_mention(mention_role_id, event.guild_id) if mention_role_id else None
        text = build_headline(event) if embed_enabled else build_plain_text(event)
        if mention_text is None:
            return text
        return f"{mention_text} {text}" if embed_enabled else f"{mention_text}\n{text}"

    def _allowed_mentions_for(
        self, content: Optional[str], mention_role_id: Optional[int], guild_id: Optional[int]
    ) -> discord.AllowedMentions:
        text = content or ""
        # The @everyone role is sent as literal "@everyone" text (see
        # format_role_mention), which the `everyone` check below covers —
        # it isn't a real role to allow-list.
        is_real_role = mention_role_id is not None and mention_role_id != guild_id
        return discord.AllowedMentions(
            # @everyone/@here can only appear here if the admin picked the
            # @everyone role or typed it into their own template — platform
            # content substituted into placeholders is always
            # mention-escaped first (see notifications/templates.py).
            everyone="@everyone" in text or "@here" in text,
            users=False,
            roles=[discord.Object(id=mention_role_id)] if is_real_role else False,
        )

    async def _send_to_channel(
        self,
        channel_row: Dict[str, Any],
        *,
        layout: discord.ui.LayoutView,
        allowed_mentions: discord.AllowedMentions,
        cover: Optional[bytes] = None,
    ) -> Optional[Any]:
        """Returns the sent message (for a later edit), or None if nothing
        was delivered."""
        discord_channel_id = channel_row["channel_id"]
        discord_channel = self._bot.get_channel(discord_channel_id)

        if discord_channel is None:
            try:
                discord_channel = await self._bot.fetch_channel(discord_channel_id)
            except (discord.NotFound, discord.Forbidden):
                await self._mark_invalid(channel_row, "channel is no longer accessible")
                return None
            except discord.HTTPException:
                logger.exception("Failed to look up notification channel %s.", discord_channel_id)
                return None

        # Not verified up front (that would cost a Discord round trip on
        # every alert) — a deleted webhook surfaces as NotFound on send,
        # and is replaced right there.
        webhook = await self.get_or_create_webhook(channel_row, discord_channel, verify=False)
        # A discord.File is used up by one send, so each attempt gets its own.
        extra: Dict[str, Any] = {}
        for attempt in range(2):
            if webhook is None:
                break
            try:
                # No username/avatar_url override here on purpose — see this
                # module's docstring for why: it would stomp any branding an
                # admin has customized on this webhook in Discord's own UI.
                # wait=True returns the message, so it can be edited later.
                if cover:
                    extra = {"files": [self._cover_file(cover)]}
                return await webhook.send(view=layout, allowed_mentions=allowed_mentions, wait=True, **extra)
            except discord.NotFound:
                # The webhook itself was deleted from Discord's side (e.g.
                # a server admin removed it manually) — forget it and send
                # this alert through a fresh one; if that fails too, fall
                # through to sending as the bot instead of dropping it.
                await self._channel_repo.set_webhook_url(channel_row["id"], None)
                channel_row = {**channel_row, "webhook_url": None}
                webhook = await self.get_or_create_webhook(channel_row, discord_channel) if attempt == 0 else None
            except discord.HTTPException:
                logger.exception("Failed to send via webhook in channel %s; falling back.", discord_channel_id)
                break

        try:
            if cover:
                extra = {"files": [self._cover_file(cover)]}
            return await discord_channel.send(view=layout, allowed_mentions=allowed_mentions, **extra)
        except discord.Forbidden:
            await self._mark_invalid(channel_row, "missing permission to send messages")
        except discord.NotFound:
            await self._mark_invalid(channel_row, "channel was deleted")
        except discord.HTTPException:
            logger.exception("Failed to send notification to channel %s.", discord_channel_id)
        return None

    async def get_or_create_webhook(
        self, channel_row: Dict[str, Any], discord_channel: Any, *, verify: bool = True
    ) -> Optional[discord.Webhook]:
        """Public (not just an internal `_send_to_channel()` helper) since
        `/notify account-add` also calls this proactively — right when
        a channel is first linked, rather than waiting for the first
        notification — so the admin can go rename/re-avatar the webhook in
        Discord's own UI before any alert ever uses it.

        `verify` checks with Discord that a stored webhook still exists.
        The send path skips it: the send itself reports a deleted webhook."""
        webhook_url = channel_row.get("webhook_url")
        if webhook_url:
            webhook = self._webhook_from_url(webhook_url, client=self._bot)
            if not verify or await self._webhook_still_exists(webhook):
                return webhook
            # Deleted from Discord's side (manually, or by some other
            # automation/integration in the server) since we created it —
            # forget the stale URL and fall through to create a fresh one,
            # rather than silently handing back a reference to nothing.
            await self._channel_repo.set_webhook_url(channel_row["id"], None)

        try:
            webhook = await discord_channel.create_webhook(name=constants.BOT_NAME)
        except discord.Forbidden:
            logger.warning(
                "Missing 'Manage Webhooks' permission in channel %s — sending as the bot instead.",
                channel_row["channel_id"],
            )
            return None
        except discord.HTTPException:
            logger.exception("Failed to create a webhook in channel %s; sending as the bot instead.", channel_row["channel_id"])
            return None

        await self._channel_repo.set_webhook_url(channel_row["id"], webhook.url)
        return webhook

    async def ensure_webhook(self, channel_row: Dict[str, Any], discord_channel: Any) -> bool:
        """Makes sure the channel has its webhook (so an admin can brand it
        before the first alert). True only when one was newly created here —
        the caller then tells the admin to go and customize it."""
        before = channel_row.get("webhook_url")
        webhook = await self.get_or_create_webhook(channel_row, discord_channel)
        return webhook is not None and webhook.url != before

    async def delete_guild_webhooks(self, guild_id: int) -> int:
        """Deletes every webhook the bot made in a server it has left and
        forgets them. Works without being in the server: a webhook's own
        URL is enough to delete it. Returns how many were deleted."""
        deleted = 0
        for row in await self._channel_repo.list_with_webhooks(guild_id):
            try:
                await self._webhook_from_url(row["webhook_url"], client=self._bot).delete()
                deleted += 1
            except (discord.NotFound, discord.Forbidden):
                pass  # already gone
            except discord.HTTPException:
                logger.warning("Couldn't delete the webhook for channel %s.", row["channel_id"], exc_info=True)
                continue  # keep the URL so a later cleanup can retry
            await self._channel_repo.set_webhook_url(row["id"], None)
        return deleted

    async def _webhook_still_exists(self, webhook: discord.Webhook) -> bool:
        try:
            await webhook.fetch()
            return True
        except (discord.NotFound, discord.Forbidden):
            return False

    async def _mark_invalid(self, channel_row: Dict[str, Any], reason: str) -> None:
        await self._channel_repo.mark_invalid(channel_row["id"])
        logger.warning("Marking notification channel %s invalid: %s", channel_row["channel_id"], reason)
