from datetime import UTC, datetime, timedelta

import pytest

from app.campaigns.models import Button
from app.campaigns.scheduling import can_create_cycle, fit_rotation_schedule, scheduled_cycle_count, scheduled_cycle_time
from app.telegram.handlers_owner import (
    OwnerHandlers,
    campaign_keyboard,
    content_type_keyboard,
    cta_style_keyboard,
    parse_period_minutes,
    parse_repost_gaps_minutes,
    parse_repost_offsets_minutes,
    quick_duration_keyboard,
    quick_interval_keyboard,
    retention_keyboard,
)
from app.telegram.keyboards import audience_markup, auto_button_rows


def button(label: str, row: int = 0) -> Button:
    return Button(id=label, text=label, url="https://t.me/example", row=row)


def test_cycle_end_is_strictly_before_campaign_end() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)
    assert can_create_cycle(start, end, 0, 3600)
    assert can_create_cycle(start, end, 1, 3600)
    assert not can_create_cycle(start, end, 2, 3600)
    assert scheduled_cycle_time(start, 1, 3600) == start + timedelta(hours=1)


def test_horizontal_first_buttons_wrap_without_truncation() -> None:
    assert len(auto_button_rows([button("ONE"), button("TWO"), button("THREE")])) == 1
    rows = auto_button_rows([button("A deliberately long label"), button("Another long label")])
    assert [item.text for row in rows for item in row] == ["A deliberately long label", "Another long label"]
    assert len(rows) == 2


def test_custom_rows_are_respected() -> None:
    rows = auto_button_rows([button("A", 1), button("B", 0), button("C", 1)], "CUSTOM")
    assert [[item.text for item in row] for row in rows] == [["B"], ["A", "C"]]


def test_campaign_cta_colors_are_rendered_with_native_bot_api_styles() -> None:
    markup = audience_markup(
        [
            Button(id="neutral", text="Neutral", url="https://t.me/example", style="default"),
            Button(id="blue", text="Blue", url="https://t.me/example", style="primary"),
            Button(id="green", text="Green", url="https://t.me/example", style="success"),
            Button(id="red", text="Red", url="https://t.me/example", style="danger"),
        ],
        "1",
    )
    assert markup is not None
    assert [row[0].style for row in markup.inline_keyboard] == [None, "primary", "success", "danger"]


def test_cta_color_picker_exposes_neutral_blue_green_and_red_choices() -> None:
    buttons = [button for row in cta_style_keyboard("c:cmp:open").inline_keyboard for button in row]
    assert {"Neutral", "Blue • main action", "Green • positive", "Red • warning"} <= {button.text for button in buttons}
    assert {button.style for button in buttons} >= {None, "primary", "success", "danger"}


def test_guided_creator_uses_callback_controls_not_pipe_delimited_commands() -> None:
    content_controls = [item.text for row in content_type_keyboard("cmp").inline_keyboard for item in row]
    campaign_controls = [item.text for row in campaign_keyboard("cmp", "DRAFT", variant_count=2).inline_keyboard for item in row]
    archived_controls = [item.text for row in campaign_keyboard("cmp", "ARCHIVED", has_live_posts=True).inline_keyboard for item in row]
    assert {"Text", "Photo", "Photo + caption", "Video", "Video + caption", "Forward ready post"} <= set(content_controls)
    assert {
        "+ Add variant",
        "Manage variants (2)",
        "Rename",
        "CTA buttons",
        "Promoted links",
        "Audience",
        "Plan for later",
        "Send campaign",
        "Delete draft",
        "Home",
    } <= set(campaign_controls)
    assert {"Run again now", "Edit a copy", "Full report", "Delete retained posts", "Delete campaign history"} <= set(archived_controls)


def test_every_campaign_state_has_safe_navigation_and_short_callback_data() -> None:
    active = campaign_keyboard("cmp_123", "ACTIVE", variant_count=2)
    keyboards = [
        campaign_keyboard("cmp_123", "DRAFT", variant_count=2),
        campaign_keyboard("cmp_123", "SCHEDULED", variant_count=2),
        active,
        campaign_keyboard("cmp_123", "PAUSED", variant_count=2),
        campaign_keyboard("cmp_123", "ENDING", variant_count=2),
        campaign_keyboard("cmp_123", "ARCHIVED", variant_count=2, has_live_posts=True),
    ]
    for keyboard in keyboards:
        buttons = [item for row in keyboard.inline_keyboard for item in row]
        assert "Home" in {item.text for item in buttons}
        assert all(not item.callback_data or len(item.callback_data.encode()) <= 64 for item in buttons)
    assert "+3 days" in {item.text for row in active.inline_keyboard for item in row}
    assert "Variants (2)" in {item.text for row in active.inline_keyboard for item in row}
    ending_labels = {item.text for row in keyboards[4].inline_keyboard for item in row}
    assert {"View cleanup issues", "Retry cleanup"} <= ending_labels


def test_expired_network_controls_recover_to_network_and_home() -> None:
    controls = [item.text for row in OwnerHandlers._recovery_keyboard("net:refresh_attention").inline_keyboard for item in row]
    assert {"Network", "Home"} <= set(controls)


def test_retention_controls_distinguish_all_three_end_behaviors() -> None:
    labels = [item.text for row in retention_keyboard("cmp", False, True).inline_keyboard for item in row]
    assert "✓ Keep until a future campaign replaces it" in labels
    assert "Keep until I delete it" in labels
    assert "Delete final post at campaign end" in labels


def test_quick_send_controls_offer_custom_duration_and_compatible_intervals() -> None:
    duration_controls = [item.text for row in quick_duration_keyboard("cmp").inline_keyboard for item in row]
    short_intervals = [item.text for row in quick_interval_keyboard("cmp", 15, 6).inline_keyboard for item in row]
    thirty_minute_intervals = [item.text for row in quick_interval_keyboard("cmp", 30, 6).inline_keyboard for item in row]
    assert {"15 minutes", "1 day", "30 days", "Custom duration", "Back", "Home"} <= set(duration_controls)
    assert {"Post once only", "Every 5 minutes", "Custom interval", "Specific times after launch", "Set custom repost gaps"} <= set(short_intervals)
    assert "Every 1 hour" not in short_intervals
    assert {"Every 5 minutes", "Every 10 minutes", "Every 15 minutes"} <= set(thirty_minute_intervals)


def test_custom_periods_and_reposts_fit_the_campaign_window() -> None:
    assert parse_period_minutes("45m", field="duration") == 45
    assert parse_period_minutes("2h", field="duration") == 120
    assert parse_period_minutes("3d", field="duration") == 4_320
    assert parse_period_minutes("1mo", field="duration") == 43_200
    assert parse_repost_offsets_minutes("1d, 4d, 6d", duration_minutes=7 * 24 * 60) == [1_440, 5_760, 8_640]
    assert parse_repost_gaps_minutes("1d, 3d, 2d", duration_minutes=7 * 24 * 60) == [1_440, 5_760, 8_640]
    # A requested five-day next gap with only three days left is fitted to a
    # final post shortly before the campaign finishes instead of rejecting it.
    assert parse_repost_gaps_minutes("4d, 5d", duration_minutes=7 * 24 * 60) == [5_760, 10_020]
    OwnerHandlers._validate_repost_interval(30, 10)
    OwnerHandlers._validate_repost_interval(30, 7)
    with pytest.raises(ValueError, match="shorter"):
        OwnerHandlers._validate_repost_interval(15, 60)


def test_incomplete_rotation_interval_is_auto_fitted_to_every_variant() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    fit = fit_rotation_schedule(
        start_at=start,
        end_at=start + timedelta(minutes=15),
        interval_seconds=10 * 60,
        repost_offsets_seconds=None,
        mode="MIX_ROTATE",
        variant_count=3,
    )

    assert fit.interval_seconds == 5 * 60
    assert scheduled_cycle_count(start, fit.end_at, fit.interval_seconds) == 3
    assert fit.adjusted


def test_incomplete_uneven_rotation_plan_is_respaced_instead_of_silently_ending() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    fit = fit_rotation_schedule(
        start_at=start,
        end_at=start + timedelta(days=7),
        interval_seconds=None,
        repost_offsets_seconds=[24 * 3600],
        mode="ROTATE",
        variant_count=5,
    )

    assert fit.offsets_seconds is not None
    assert len(fit.offsets_seconds) == 4
    assert scheduled_cycle_count(start, fit.end_at, None, fit.offsets_seconds) == 5
    assert any("all 5 variants" in note for note in fit.notes)


def test_complete_safe_custom_rotation_plan_is_preserved_exactly() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    offsets = [24 * 3600, 4 * 24 * 3600, 6 * 24 * 3600]
    fit = fit_rotation_schedule(
        start_at=start,
        end_at=start + timedelta(days=7),
        interval_seconds=None,
        repost_offsets_seconds=offsets,
        mode="MIX_ROTATE",
        variant_count=3,
    )

    assert fit.offsets_seconds == offsets
    assert not fit.adjusted
