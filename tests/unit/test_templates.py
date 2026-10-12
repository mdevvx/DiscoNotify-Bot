"""render_custom_message: placeholder substitution that never crashes on a
malformed template, and never lets platform-sourced content (title,
description, ...) inject a mention the admin didn't type themselves."""

from __future__ import annotations

from events.models import EventType, NormalizedEvent
from notifications.templates import render_custom_message


def _event(**overrides) -> NormalizedEvent:
    fields = dict(
        platform="instagram",
        platform_account_id="123",
        account_username="pajamabillionaire",
        event_type=EventType.POST_CREATED,
        platform_event_id="post-1",
        title="a new post",
        url="https://www.instagram.com/p/DcuX5pojWf7/",
    )
    fields.update(overrides)
    return NormalizedEvent(**fields)


def test_renders_the_users_example_message() -> None:
    template = "Hey @everyone, {creator} just posted a new post! Go check it out!\n{url}"
    rendered = render_custom_message(template, _event())

    assert rendered == (
        "Hey @everyone, pajamabillionaire just posted a new post! Go check it out!\n"
        "https://www.instagram.com/p/DcuX5pojWf7/"
    )


def test_admins_literal_everyone_is_preserved() -> None:
    rendered = render_custom_message("Hey @everyone!", _event())
    assert "@everyone" in rendered


def test_unknown_placeholder_is_left_as_literal_text_not_an_error() -> None:
    rendered = render_custom_message("Hello {nonexistent} world", _event())
    assert rendered == "Hello {nonexistent} world"


def test_malformed_brace_does_not_crash() -> None:
    rendered = render_custom_message("Unclosed { brace and } stray", _event())
    assert isinstance(rendered, str)


def test_title_containing_mention_syntax_is_escaped_not_a_real_mention() -> None:
    event = _event(title="please @everyone read this")
    rendered = render_custom_message("New post: {title}", event)

    assert "@everyone" not in rendered  # escaped, not the literal substring anymore
    assert "everyone" in rendered  # the words are still there, just neutralized


def test_description_and_category_are_also_escaped() -> None:
    # Discord snowflake IDs are always 17-20 digits — escape_mentions only
    # recognizes mention syntax with a real-length ID, so the test must
    # use one to actually exercise that path.
    event = _event(description="ping <@&999999999999999999> now", metadata={"category": "<@123456789012345678> stuff"})
    rendered = render_custom_message("{description} / {category}", event)

    assert "<@&999999999999999999>" not in rendered
    assert "<@123456789012345678>" not in rendered


def test_mention_placeholder_resolves_to_the_configured_role() -> None:
    rendered = render_custom_message("Ping: {mention}", _event(), mention_role_id=555)
    assert rendered == "Ping: <@&555>"


def test_mention_placeholder_for_the_everyone_role_is_literal_everyone() -> None:
    rendered = render_custom_message("Ping: {mention}", _event(guild_id=42), mention_role_id=42)
    assert rendered == "Ping: @everyone"


def test_mention_placeholder_is_empty_when_no_role_configured() -> None:
    rendered = render_custom_message("Ping: {mention}", _event(), mention_role_id=None)
    assert rendered == "Ping: "


def test_url_is_not_mention_escaped() -> None:
    rendered = render_custom_message("{url}", _event(url="https://example.com/@everyone-page"))
    assert rendered == "https://example.com/@everyone-page"


def test_missing_optional_fields_render_as_empty_string() -> None:
    event = _event(title=None, description=None)
    rendered = render_custom_message("[{title}][{description}][{category}][{viewers}]", event)
    assert rendered == "[][][][]"


def test_rendered_output_is_truncated_to_discord_message_limit() -> None:
    event = _event(title="x" * 3000)
    rendered = render_custom_message("{title}", event)
    assert len(rendered) <= 2000
    assert rendered.endswith("…")


def test_all_placeholders_constant_matches_what_template_values_produces() -> None:
    from notifications.templates import PLACEHOLDERS, _template_values

    values = _template_values(_event(), mention_role_id=1)
    assert set(PLACEHOLDERS) == set(values.keys())
