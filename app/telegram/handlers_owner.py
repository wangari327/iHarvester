"""Telegram-native owner control plane with short-lived, guided interaction state."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultsButton,
    Message,
    SwitchInlineQueryChosenChat,
)
from pydantic import ValidationError

from app.backups.export import make_backup
from app.backups.restore import parse_backup, restore_backup
from app.campaigns.models import Button, CampaignMode, CampaignStatus, ChannelStatus, Creative, Destination
from app.campaigns.service import CampaignService
from app.campaigns.sharing import creative_snapshot_hash, normalize_share_code
from app.db.repositories import Document, Repositories
from app.telegram.formatting import capture_creative
from app.telegram.handlers_admin_updates import refresh_channel, register_forwarded_channel
from app.telegram.inline_sharing import (
    UnsupportedInlineCreative,
    button_layout_manifest,
    inline_result_for_share,
    inline_support_error,
)
from app.telegram.keyboards import home_keyboard
from app.telegram.sender import TelegramSender
from app.utils.ids import opaque_id
from app.utils.time import as_utc

logger = logging.getLogger(__name__)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _navigation(back_callback: str = "home:back") -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(text="Back", callback_data=back_callback),
            InlineKeyboardButton(text="Home", callback_data="home:back"),
        ]
    ]


_PERIOD_UNITS = {
    "m": 1,
    "min": 1,
    "mins": 1,
    "minute": 1,
    "minutes": 1,
    "h": 60,
    "hr": 60,
    "hrs": 60,
    "hour": 60,
    "hours": 60,
    "d": 24 * 60,
    "day": 24 * 60,
    "days": 24 * 60,
    "mo": 30 * 24 * 60,
    "month": 30 * 24 * 60,
    "months": 30 * 24 * 60,
}
_INTERVAL_PRESET_MINUTES = (5, 10, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440)
_SAFE_REPOST_MAX_MINUTES = 47 * 60
_MAX_VARIANTS = 20
_MAX_DESTINATIONS = 20
_MAX_CTA_BUTTONS = 20
_CTA_STYLES = {
    "default": "Neutral",
    "primary": "Blue",
    "success": "Green",
    "danger": "Red",
}


def _cta_style_label(style: str) -> str:
    return _CTA_STYLES.get(style, "Neutral")


def cta_style_keyboard(back_callback: str) -> InlineKeyboardMarkup:
    """Choose a native Bot API button style without asking the owner to type."""

    return _markup(
        [
            [InlineKeyboardButton(text="Neutral", callback_data="tone:default")],
            [InlineKeyboardButton(text="Blue • main action", callback_data="tone:primary", style="primary")],
            [
                InlineKeyboardButton(text="Green • positive", callback_data="tone:success", style="success"),
                InlineKeyboardButton(text="Red • warning", callback_data="tone:danger", style="danger"),
            ],
            *_navigation(back_callback),
        ]
    )


async def refresh_attention_channels(bot: Bot, repositories: Repositories, *, concurrency: int = 6) -> dict[str, int]:
    """Re-verify the current attention queue without touching manual pauses."""
    chat_ids = await repositories.channel_ids_by_status(ChannelStatus.NEEDS_ATTENTION.value)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def verify(chat_id: int) -> str:
        async with semaphore:
            try:
                await refresh_channel(bot, repositories, chat_id)
                channel = await repositories.get_channel(chat_id)
                return str(channel.get("status", ChannelStatus.NEEDS_ATTENTION.value)) if channel else "MISSING"
            except Exception:
                logger.exception("Bulk channel refresh failed", extra={"telegram_chat_id": chat_id})
                return ChannelStatus.NEEDS_ATTENTION.value

    statuses = Counter(await asyncio.gather(*(verify(chat_id) for chat_id in chat_ids)))
    return {
        "checked": len(chat_ids),
        "active": statuses[ChannelStatus.ACTIVE.value],
        "needs_attention": statuses[ChannelStatus.NEEDS_ATTENTION.value],
        "unavailable": statuses[ChannelStatus.UNAVAILABLE.value],
        "other": sum(
            count
            for status, count in statuses.items()
            if status not in {ChannelStatus.ACTIVE.value, ChannelStatus.NEEDS_ATTENTION.value, ChannelStatus.UNAVAILABLE.value}
        ),
    }


def _period_label(minutes: int) -> str:
    if minutes % (30 * 24 * 60) == 0:
        value = minutes // (30 * 24 * 60)
        return f"{value} month{'s' if value != 1 else ''}"
    if minutes % (24 * 60) == 0:
        value = minutes // (24 * 60)
        return f"{value} day{'s' if value != 1 else ''}"
    if minutes % 60 == 0:
        value = minutes // 60
        return f"{value} hour{'s' if value != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _cleanup_guidance(category: str | None) -> str:
    """Human action for a cleanup failure; unknown categories stay honest."""
    guidance = {
        "ACCESS_OR_PERMISSION": "Restore the bot's channel admin/delete access, then use Retry cleanup.",
        "DELETE_NOT_ALLOWED": (
            "Telegram refused that deletion, usually because the post is over its 48-hour Bot API deletion limit. "
            "The tracked post remains; remove it manually if Telegram still permits it."
        ),
        "RETRY_EXHAUSTED": "A temporary error exhausted its first retry round. The bot will retry later; Retry cleanup runs it now.",
        "ALREADY_ABSENT": "The post was already removed manually; the bot will reconcile it as cleaned.",
    }
    return guidance.get(category or "", "Open the item after Refresh dashboard to see Telegram's stored error, then retry cleanup once the cause is resolved.")


def parse_period_minutes(raw: str, *, field: str) -> int:
    """Parse short owner-entered periods; a month is deliberately 30 days."""
    match = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]+)\s*", raw)
    if not match or match.group(2).lower() not in _PERIOD_UNITS:
        raise ValueError(f"send {field} as a whole number with m, h, d, or mo (for example 45m, 2h, 3d, or 1mo)")
    minutes = int(match.group(1)) * _PERIOD_UNITS[match.group(2).lower()]
    if minutes < 1:
        raise ValueError(f"{field} must be at least 1 minute")
    return minutes


def _last_repost_before_end(duration_minutes: int) -> int:
    """Leave a small, visible window before campaign end when fitting a plan."""
    buffer_minutes = min(60, max(1, duration_minutes // 10))
    return max(1, duration_minutes - buffer_minutes)


def _fit_repost_offsets_minutes(values: list[int], *, duration_minutes: int) -> tuple[list[int], bool]:
    """Fit owner intent inside the campaign instead of discarding an overrun.

    The exact order and number are retained whenever there is enough room. A
    final requested time past the end becomes a last repost shortly before the
    end, which is more useful than an unexplained rejected schedule.
    """
    if duration_minutes < 2:
        raise ValueError("campaign duration is too short for a repost")
    latest = _last_repost_before_end(duration_minutes)
    offsets: list[int] = []
    adjusted = False
    for requested in values:
        candidate = min(requested, latest)
        if candidate != requested:
            adjusted = True
        if offsets and candidate <= offsets[-1]:
            candidate = offsets[-1] + 1
            adjusted = True
        if candidate >= duration_minutes:
            raise ValueError("there is not enough time to fit that many distinct reposts; use fewer reposts or a longer campaign")
        offsets.append(candidate)
    return offsets, adjusted


def build_repost_offsets_minutes(raw: str, *, duration_minutes: int) -> tuple[list[int], bool]:
    values = [parse_period_minutes(item, field="repost time") for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("send one or more comma-separated repost times")
    if len(values) > 20:
        raise ValueError("choose at most 20 specific repost times")
    values = sorted(set(values))
    return _fit_repost_offsets_minutes(values, duration_minutes=duration_minutes)


def parse_repost_offsets_minutes(raw: str, *, duration_minutes: int) -> list[int]:
    """Compatibility wrapper used by callers that only need the final plan."""
    return build_repost_offsets_minutes(raw, duration_minutes=duration_minutes)[0]


def build_repost_gaps_minutes(raw: str, *, duration_minutes: int) -> tuple[list[int], bool]:
    """Convert 1-20 owner-entered gaps between posts into elapsed offsets."""
    gaps = [parse_period_minutes(item, field="repost gap") for item in raw.split(",") if item.strip()]
    if not gaps:
        raise ValueError("send one or more comma-separated repost gaps")
    if len(gaps) > 20:
        raise ValueError("choose at most 20 repost gaps")
    offsets: list[int] = []
    elapsed = 0
    for gap in gaps:
        elapsed += gap
        offsets.append(elapsed)
    return _fit_repost_offsets_minutes(offsets, duration_minutes=duration_minutes)


def parse_repost_gaps_minutes(raw: str, *, duration_minutes: int) -> list[int]:
    return build_repost_gaps_minutes(raw, duration_minutes=duration_minutes)[0]


def _valid_repost_minutes(duration_minutes: int) -> tuple[int, ...]:
    return tuple(
        interval
        for interval in _INTERVAL_PRESET_MINUTES
        if interval < duration_minutes
        and duration_minutes % interval == 0
        and (duration_minutes <= _SAFE_REPOST_MAX_MINUTES or interval <= _SAFE_REPOST_MAX_MINUTES)
    )


def _interval_keyboard(
    campaign_id: str,
    duration_minutes: int,
    *,
    prefix: str,
    back_callback: str,
    preferred_minutes: int | None = None,
) -> InlineKeyboardMarkup:
    """Offer tidy presets; custom plans can use any safe shorter gap."""
    rows: list[list[InlineKeyboardButton]] = []
    valid = list(_valid_repost_minutes(duration_minutes))
    if duration_minutes <= _SAFE_REPOST_MAX_MINUTES:
        rows.append([InlineKeyboardButton(text="Post once only", callback_data=f"{prefix}:{campaign_id}:0")])
    if preferred_minutes and preferred_minutes in valid:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Use my preference: every {_period_label(preferred_minutes)}",
                    callback_data=f"{prefix}:{campaign_id}:{preferred_minutes}",
                )
            ]
        )
        valid.remove(preferred_minutes)
    for offset in range(0, len(valid), 2):
        rows.append(
            [
                InlineKeyboardButton(text=f"Every {_period_label(interval)}", callback_data=f"{prefix}:{campaign_id}:{interval}")
                for interval in valid[offset : offset + 2]
            ]
        )
    rows.append([InlineKeyboardButton(text="Custom interval", callback_data=f"{prefix}:{campaign_id}:custom")])
    rows.append(
        [
            InlineKeyboardButton(text="Specific times after launch", callback_data=f"times:{prefix}:{campaign_id}"),
            InlineKeyboardButton(text="Set custom repost gaps", callback_data=f"gaps:{prefix}:{campaign_id}"),
        ]
    )
    rows.extend(_navigation(back_callback))
    return _markup(rows)


def campaign_keyboard(
    campaign_id: str,
    status: str,
    *,
    variant_count: int = 0,
    has_live_posts: bool = False,
    has_schedule: bool = False,
) -> InlineKeyboardMarkup:
    if status == CampaignStatus.ARCHIVED.value:
        rows = [
            [InlineKeyboardButton(text="Run again now", callback_data=f"c:{campaign_id}:rerun")],
            [InlineKeyboardButton(text="Edit a copy", callback_data=f"c:{campaign_id}:duplicate")],
            [InlineKeyboardButton(text="Full report", callback_data=f"c:{campaign_id}:progress")],
            [InlineKeyboardButton(text="Share client progress page", callback_data=f"c:{campaign_id}:client")],
        ]
        if has_live_posts:
            rows.append([InlineKeyboardButton(text="Delete retained posts", callback_data=f"c:{campaign_id}:cleanup")])
        rows.append([InlineKeyboardButton(text="Delete campaign history", callback_data=f"c:{campaign_id}:delete")])
        rows.append(
            [
                InlineKeyboardButton(text="Campaigns", callback_data="home:campaigns:0"),
                InlineKeyboardButton(text="Home", callback_data="home:back"),
            ]
        )
        return _markup(rows)
    if status in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value, CampaignStatus.ENDING.value, CampaignStatus.PAUSED.value}:
        rows = [
            [
                InlineKeyboardButton(text="Refresh dashboard", callback_data=f"c:{campaign_id}:open"),
                InlineKeyboardButton(text="View campaign report", callback_data=f"c:{campaign_id}:progress"),
            ]
        ]
        rows.append([InlineKeyboardButton(text="Share client progress page", callback_data=f"c:{campaign_id}:client")])
        rows.append([InlineKeyboardButton(text=f"Variants ({variant_count})", callback_data=f"c:{campaign_id}:variants")])
        if status in {CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}:
            rows.append(
                [
                    InlineKeyboardButton(text="View failures", callback_data=f"c:{campaign_id}:failures"),
                    InlineKeyboardButton(text="Retry failed", callback_data=f"c:{campaign_id}:retry"),
                ]
            )
        elif status == CampaignStatus.ENDING.value:
            rows.append(
                [
                    InlineKeyboardButton(text="View cleanup issues", callback_data=f"c:{campaign_id}:failures"),
                    InlineKeyboardButton(text="Retry cleanup", callback_data=f"c:{campaign_id}:retrycleanup"),
                ]
            )
        if status == CampaignStatus.PAUSED.value:
            rows.append(
                [
                    InlineKeyboardButton(text="Resume & extend window", callback_data=f"c:{campaign_id}:resume"),
                    InlineKeyboardButton(text="Stop & delete posts", callback_data=f"c:{campaign_id}:end"),
                ]
            )
        elif status != CampaignStatus.ENDING.value:
            if status == CampaignStatus.SCHEDULED.value:
                rows.append([InlineKeyboardButton(text="Edit before it starts", callback_data=f"c:{campaign_id}:todraft")])
            rows.append(
                [
                    InlineKeyboardButton(text="Pause / freeze", callback_data=f"c:{campaign_id}:pause"),
                    InlineKeyboardButton(text="Keep or delete final post", callback_data=f"c:{campaign_id}:retention"),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(text="+6 hours", callback_data=f"c:{campaign_id}:extend6"),
                    InlineKeyboardButton(text="+1 day", callback_data=f"c:{campaign_id}:extend24"),
                ]
            )
            rows.append([InlineKeyboardButton(text="+3 days", callback_data=f"c:{campaign_id}:extend72")])
            rows.append([InlineKeyboardButton(text="Custom extension", callback_data=f"c:{campaign_id}:extendcustom")])
            rows.append(
                [
                    InlineKeyboardButton(text="Stop & delete posts", callback_data=f"c:{campaign_id}:end"),
                    InlineKeyboardButton(text="Edit as new draft", callback_data=f"c:{campaign_id}:refactor"),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text="Campaigns", callback_data="home:campaigns:0"),
                InlineKeyboardButton(text="Home", callback_data="home:back"),
            ]
        )
        return _markup(rows)
    rows = [
        [
            InlineKeyboardButton(text="Add first variant" if not variant_count else "+ Add variant", callback_data=f"c:{campaign_id}:add"),
            InlineKeyboardButton(text="Rename", callback_data=f"c:{campaign_id}:rename"),
        ]
    ]
    if variant_count:
        rows.extend(
            [
                [
                    InlineKeyboardButton(text=f"Manage variants ({variant_count})", callback_data=f"c:{campaign_id}:variants"),
                    InlineKeyboardButton(text="CTA buttons", callback_data=f"c:{campaign_id}:buttons"),
                ],
                [
                    InlineKeyboardButton(text="Audience", callback_data=f"c:{campaign_id}:targets"),
                    InlineKeyboardButton(text="Promoted links", callback_data=f"c:{campaign_id}:destination"),
                ],
                [
                    InlineKeyboardButton(text="Preview all", callback_data=f"c:{campaign_id}:preview"),
                    InlineKeyboardButton(text="Test in a channel", callback_data=f"c:{campaign_id}:test"),
                ],
                [InlineKeyboardButton(text="Send campaign", callback_data=f"c:{campaign_id}:send")],
                [
                    InlineKeyboardButton(text="Plan for later", callback_data=f"c:{campaign_id}:schedule"),
                    InlineKeyboardButton(text="End behavior", callback_data=f"c:{campaign_id}:retention"),
                ],
            ]
        )
        if has_schedule:
            rows.append([InlineKeyboardButton(text="Review & schedule campaign", callback_data=f"c:{campaign_id}:launch")])
        if variant_count >= 2:
            rows.append([InlineKeyboardButton(text="Rotation mode", callback_data=f"c:{campaign_id}:mode")])
    rows.extend(
        [
            [InlineKeyboardButton(text="Delete draft", callback_data=f"c:{campaign_id}:delete")],
            [
                InlineKeyboardButton(text="Campaigns", callback_data="home:campaigns:0"),
                InlineKeyboardButton(text="Home", callback_data="home:back"),
            ],
        ]
    )
    return _markup(rows)


def content_type_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(text="Text", callback_data=f"ct:{campaign_id}:TEXT"),
                InlineKeyboardButton(text="Photo", callback_data=f"ct:{campaign_id}:PHOTO"),
            ],
            [
                InlineKeyboardButton(text="Photo + caption", callback_data=f"ct:{campaign_id}:PHOTO_TEXT"),
                InlineKeyboardButton(text="Video", callback_data=f"ct:{campaign_id}:VIDEO"),
            ],
            [
                InlineKeyboardButton(text="Video + caption", callback_data=f"ct:{campaign_id}:VIDEO_TEXT"),
                InlineKeyboardButton(text="More media", callback_data=f"ct:{campaign_id}:MEDIA"),
            ],
            [InlineKeyboardButton(text="Album", callback_data=f"ct:{campaign_id}:ALBUM")],
            [InlineKeyboardButton(text="Forward ready post", callback_data=f"ct:{campaign_id}:FORWARD")],
            *_navigation(f"c:{campaign_id}:open"),
        ]
    )


def mode_keyboard(campaign_id: str, variant_count: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Standard • Variant 1 only", callback_data=f"mode:{campaign_id}:STANDARD")]]
    if variant_count >= 2:
        rows.extend(
            [
                [InlineKeyboardButton(text="Rotate", callback_data=f"mode:{campaign_id}:ROTATE")],
                [InlineKeyboardButton(text="Mix + Rotate", callback_data=f"mode:{campaign_id}:MIX_ROTATE")],
            ]
        )
    rows.extend(_navigation(f"c:{campaign_id}:open"))
    return _markup(rows)


def retention_keyboard(campaign_id: str, delete_on_end: bool, delete_on_next_campaign: bool) -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text=f"{'✓ ' if delete_on_end else ''}Delete final post at campaign end", callback_data=f"ret:{campaign_id}:delete")],
            [
                InlineKeyboardButton(
                    text=f"{'✓ ' if delete_on_next_campaign else ''}Keep until a future campaign replaces it",
                    callback_data=f"ret:{campaign_id}:replace",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✓ ' if not delete_on_end and not delete_on_next_campaign else ''}Keep until I delete it",
                    callback_data=f"ret:{campaign_id}:keep",
                )
            ],
            *_navigation(f"c:{campaign_id}:open"),
        ]
    )


def schedule_interval_keyboard(campaign_id: str, duration_minutes: int) -> InlineKeyboardMarkup:
    return _interval_keyboard(campaign_id, duration_minutes, prefix="sint", back_callback=f"c:{campaign_id}:open")


def target_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="All active channels", callback_data=f"target:{campaign_id}:all")],
            [
                InlineKeyboardButton(text="Match tags", callback_data=f"target:{campaign_id}:tags"),
                InlineKeyboardButton(text="Audience size", callback_data=f"target:{campaign_id}:members"),
            ],
            [
                InlineKeyboardButton(text="Manually include IDs", callback_data=f"target:{campaign_id}:include"),
                InlineKeyboardButton(text="Exclude IDs", callback_data=f"target:{campaign_id}:exclude"),
            ],
            *_navigation(f"c:{campaign_id}:open"),
        ]
    )


def quick_duration_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(text="15 minutes", callback_data=f"dur:{campaign_id}:15"),
                InlineKeyboardButton(text="1 hour", callback_data=f"dur:{campaign_id}:60"),
            ],
            [
                InlineKeyboardButton(text="6 hours", callback_data=f"dur:{campaign_id}:360"),
                InlineKeyboardButton(text="1 day", callback_data=f"dur:{campaign_id}:1440"),
            ],
            [
                InlineKeyboardButton(text="3 days", callback_data=f"dur:{campaign_id}:4320"),
                InlineKeyboardButton(text="7 days", callback_data=f"dur:{campaign_id}:10080"),
            ],
            [InlineKeyboardButton(text="30 days", callback_data=f"dur:{campaign_id}:43200")],
            [InlineKeyboardButton(text="Custom duration", callback_data=f"dur:{campaign_id}:custom")],
            *_navigation(f"c:{campaign_id}:open"),
        ]
    )


def quick_interval_keyboard(campaign_id: str, duration_minutes: int, default_hours: float) -> InlineKeyboardMarkup:
    return _interval_keyboard(
        campaign_id,
        duration_minutes,
        prefix="qmin",
        back_callback=f"c:{campaign_id}:send",
        preferred_minutes=round(default_hours * 60) if default_hours else None,
    )


class OwnerHandlers:
    def __init__(
        self,
        *,
        owner_ids: frozenset[int],
        repositories: Repositories,
        campaigns: CampaignService,
        sender: TelegramSender,
        public_base_url: str | None = None,
    ) -> None:
        self.owner_ids = owner_ids
        self.repositories = repositories
        self.campaigns = campaigns
        self.sender = sender
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.router = Router(name="owner")
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.backup, Command("backup"))
        self.router.message.register(self.restore, Command("restore"))
        self.router.inline_query.register(self.inline_query)
        self.router.callback_query.register(self.callback, F.data)
        self.router.message.register(self.message)

    def _allowed(self, user_id: int | None, chat_type: str) -> bool:
        return user_id in self.owner_ids and chat_type == "private"

    def _allowed_inline(self, user_id: int | None) -> bool:
        return user_id in self.owner_ids

    @staticmethod
    def _date(value: Any, timezone: str = "UTC") -> str:
        if not value:
            return "Not set"
        try:
            local = as_utc(value).astimezone(ZoneInfo(timezone))
        except ZoneInfoNotFoundError:
            timezone = "UTC"
            local = as_utc(value)
        return f"{local.strftime('%d %b %Y, %H:%M')} {timezone}"

    @staticmethod
    def _bar(completed: int, total: int, width: int = 12) -> str:
        if total <= 0:
            return "waiting"
        filled = min(width, max(0, round(width * completed / total)))
        return "🟩" * filled + "⬜" * (width - filled)

    @staticmethod
    def _duration_label(seconds: int) -> str:
        seconds = max(0, seconds)
        if seconds < 60:
            return f"{seconds}s"
        days, remainder = divmod(seconds, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, _ = divmod(remainder, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours or parts:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    async def _retire(self, query: CallbackQuery) -> None:
        """Disable the just-used control so old UI messages cannot drive stale workflows."""
        if not query.message:
            return
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

    async def _render(self, message: Message, text: str, reply_markup: InlineKeyboardMarkup) -> None:
        """Reuse callback control messages to keep the owner's chat clean."""
        if message.from_user and message.from_user.is_bot and message.text:
            try:
                await message.edit_text(text, reply_markup=reply_markup)
                return
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
        await message.answer(text, reply_markup=reply_markup)

    async def _show_home(self, message: Message) -> None:
        channels = await self.repositories.channel_status_counts()
        campaigns = await self.repositories.campaign_status_counts()
        request_count = await self.repositories.pending_client_promotion_request_count()
        await self._render(
            message,
            "iHarvester control room\n\n"
            f"Active campaigns: {campaigns.get('ACTIVE', 0)}\n"
            f"Scheduled campaigns: {campaigns.get('SCHEDULED', 0)}\n"
            f"Active source channels: {channels.get('ACTIVE', 0)}\n"
            f"Need attention: {channels.get('NEEDS_ATTENTION', 0)}\n"
            f"Client requests: {request_count}",
            home_keyboard(),
        )

    async def _show_settings(self, message: Message) -> None:
        timezone = await self.repositories.get_setting("owner_timezone", "UTC")
        interval = float(await self.repositories.get_setting("quick_interval_hours", 6))
        interval_text = "Post once" if not interval else f"Every {_period_label(round(interval * 60))}"
        backup_enabled = bool(await self.repositories.get_setting("auto_backup_enabled", True))
        backup_channels = int(await self.repositories.get_setting("auto_backup_every_new_channels", 100))
        backup_hours = int(await self.repositories.get_setting("auto_backup_interval_hours", 168))
        await self._render(
            message,
            "Settings\n\n"
            f"Display timezone: {timezone}\n"
            f"Quick-send default interval: {interval_text}\n"
            f"Automatic backups: {'On' if backup_enabled else 'Off'}\n"
            f"Backup triggers: every {backup_channels} new channels or every {_period_label(backup_hours * 60)}\n\n"
            "These preferences apply to new campaign setup and automatic safety copies.",
            _markup(
                [
                    [
                        InlineKeyboardButton(text="UTC", callback_data="set:timezone:UTC"),
                        InlineKeyboardButton(text="Africa/Nairobi", callback_data="set:timezone:Africa/Nairobi"),
                    ],
                    [InlineKeyboardButton(text="Custom timezone", callback_data="set:timezone:custom")],
                    [
                        InlineKeyboardButton(text="Default: post once", callback_data="set:interval:0"),
                        InlineKeyboardButton(text="Default: every 6h", callback_data="set:interval:6"),
                    ],
                    [InlineKeyboardButton(text="Default: every 24h", callback_data="set:interval:24")],
                    [InlineKeyboardButton(text="Custom default interval", callback_data="set:interval:custom")],
                    [
                        InlineKeyboardButton(
                            text="Turn backups off" if backup_enabled else "Turn backups on",
                            callback_data=f"set:backup:{'off' if backup_enabled else 'on'}",
                        )
                    ],
                    [
                        InlineKeyboardButton(text="Backup channel trigger", callback_data="set:backup_channels:custom"),
                        InlineKeyboardButton(text="Backup time trigger", callback_data="set:backup_interval:custom"),
                    ],
                    *_navigation(),
                ]
            ),
        )

    async def _show_campaign(self, message: Message, campaign: Document) -> None:
        variants = campaign.get("variants", [])
        button_count = sum(len(item.get("buttons", [])) for item in variants)
        destinations = campaign.get("destinations", [])
        selector = campaign.get("target_selector") or {}
        snapshot = campaign.get("target_snapshot", [])
        protected_ids = {item["telegram_chat_id"] for item in destinations if item.get("telegram_chat_id") is not None}
        status = campaign["status"]
        # A launched campaign's audience is immutable history.  Only drafts
        # should be evaluated against the network as it exists today.
        planned_count = (
            await self.repositories.active_channel_count(selector, exclude_ids=protected_ids)
            if status == CampaignStatus.DRAFT.value
            else len(snapshot)
        )
        if status == CampaignStatus.DRAFT.value and len(variants) < 2 and campaign.get("mode") != CampaignMode.STANDARD.value:
            await self.repositories.update_campaign(
                campaign["campaign_id"],
                {
                    "mode": CampaignMode.STANDARD.value,
                    "mode_explicitly_selected": False,
                    "updated_at": datetime.now(UTC),
                },
            )
            campaign["mode"] = CampaignMode.STANDARD.value
            campaign["mode_explicitly_selected"] = False
        display_timezone = campaign.get("owner_timezone") or await self.repositories.get_setting("owner_timezone", "UTC")
        cleanup = (
            "Delete final post"
            if campaign.get("delete_on_end", True)
            else "Keep until a future campaign replaces it"
            if campaign.get("delete_on_next_campaign", False)
            else "Keep until manually deleted"
        )

        if status == CampaignStatus.DRAFT.value:
            has_schedule = bool(campaign.get("start_at_utc") and campaign.get("current_end_at_utc"))
            content_line = f"✓ Variants: {len(variants)}" if variants else "○ Variants: add the first post"
            preview_line = "✓ Preview: ready" if campaign.get("preview_sent") else "○ Preview: required before launch"
            schedule_line = (
                f"✓ Timing: {self._date(campaign['start_at_utc'], display_timezone)} → {self._date(campaign['current_end_at_utc'], display_timezone)}"
                if has_schedule
                else "○ Timing: choose Send campaign or Plan for later"
            )
            if not variants:
                next_step = "Next: add Variant 1, then add more variants if you want rotation."
            elif not campaign.get("preview_sent"):
                next_step = "Next: preview all variants, then choose Send campaign."
            elif len(variants) > 1 and campaign.get("mode") == CampaignMode.STANDARD.value:
                next_step = "Standard sends Variant 1 only. Choose Rotation mode to use every saved variant."
            elif not has_schedule:
                next_step = "Next: choose Send campaign for an immediate run, or Plan for later."
            else:
                next_step = "Ready. Review or launch this fully configured campaign."
            await self._render(
                message,
                f"{campaign['name']}\nDraft • {campaign.get('mode', 'STANDARD').replace('_', ' ').title()}\n\n"
                "Setup\n"
                f"{content_line}\n"
                f"{'✓' if button_count else '○'} CTA buttons: {button_count} (optional)\n"
                f"✓ Audience: {planned_count} eligible source channel{'s' if planned_count != 1 else ''}\n"
                f"{'✓' if destinations else '○'} Promoted links: {len(destinations)} (optional)\n"
                f"{schedule_line}\n"
                f"✓ End behavior: {cleanup.lower()}\n"
                f"{preview_line}\n"
                + (
                    "Timing auto-fit: " + "; ".join(campaign.get("rotation_adjustment_notes", [])) + "\n"
                    if campaign.get("rotation_adjustment_notes")
                    else ""
                )
                + f"\n{next_step}",
                campaign_keyboard(campaign["campaign_id"], status, variant_count=len(variants), has_schedule=has_schedule),
            )
            return

        totals = await self.repositories.campaign_delivery_totals(campaign["campaign_id"])
        cycle_stats = await self.repositories.campaign_cycle_stats(campaign["campaign_id"])
        metrics = await self.repositories.campaign_delivery_metrics(campaign["campaign_id"])
        live_count = await self.repositories.campaign_live_state_count(campaign["campaign_id"])
        cleanup_statuses = (
            await self.repositories.cleanup_status_summary(campaign["campaign_id"])
            if status == CampaignStatus.ENDING.value
            else {}
        )
        cleanup_failures_by_category = (
            await self.repositories.cleanup_failure_summary(campaign["campaign_id"])
            if status == CampaignStatus.ENDING.value
            else {}
        )
        join_count = await self.repositories.campaign_join_count(campaign["campaign_id"])
        latest_cycle = await self.repositories.latest_cycle_report(campaign["campaign_id"])
        delivery_statuses = (
            "PENDING",
            "PROCESSING",
            "RETRY_WAIT",
            "PAUSED",
            "SENT",
            "FAILED_PERMANENT",
            "UNKNOWN_SEND_STATE",
            "CLEANED",
            "CLEANUP_FAILED",
            "CANCELLED",
        )
        sent = totals.get("SENT", 0)
        permanent_failed = totals.get("FAILED_PERMANENT", 0)
        unknown = totals.get("UNKNOWN_SEND_STATE", 0)
        cleanup_failed = totals.get("CLEANUP_FAILED", 0)
        pending = sum(totals.get(item, 0) for item in ("PENDING", "PROCESSING", "RETRY_WAIT", "PAUSED"))
        cancelled = totals.get("CANCELLED", 0)
        total_jobs = sum(totals.get(status, 0) for status in delivery_statuses)
        complete_jobs = total_jobs - sum(totals.get(status, 0) for status in ("PENDING", "PROCESSING", "RETRY_WAIT", "PAUSED"))
        delivery_progress = (
            "Waiting for the first delivery cycle" if not total_jobs else f"{self._bar(complete_jobs, total_jobs, 8)}  {complete_jobs}/{total_jobs} jobs"
        )
        timeline = "Waiting to start"
        run_time = "Not started"
        if campaign.get("start_at_utc") and campaign.get("current_end_at_utc"):
            start = as_utc(campaign["start_at_utc"])
            end = as_utc(campaign["current_end_at_utc"])
            finish = as_utc(campaign["archived_at"]) if campaign.get("archived_at") else datetime.now(UTC)
            total_seconds = max(1, int((end - start).total_seconds()))
            elapsed = max(0, min(int((finish - start).total_seconds()), total_seconds))
            timeline = f"{self._bar(elapsed, total_seconds, 8)}  {round(100 * elapsed / total_seconds)}%"
            run_time = self._duration_label(max(0, int((finish - start).total_seconds())))
        phase = (
            "paused/frozen"
            if campaign["status"] == CampaignStatus.PAUSED.value
            else "running"
            if campaign["status"] == CampaignStatus.ACTIVE.value
            else campaign["status"].lower()
        )
        target_text = f"{len(snapshot)} frozen source channels"
        report_heading = "Final results" if status == CampaignStatus.ARCHIVED.value else "Live progress"
        offsets = campaign.get("repost_offsets_seconds")
        interval = campaign.get("repost_interval_seconds")
        if offsets is not None:
            expected_cycles = 1 + len(offsets)
            repost_plan = "Initial post, then at " + ", ".join(_period_label(int(value) // 60) for value in offsets)
        elif interval:
            duration_seconds = max(
                1,
                int((as_utc(campaign["current_end_at_utc"]) - as_utc(campaign["start_at_utc"])).total_seconds()),
            )
            expected_cycles = (duration_seconds + int(interval) - 1) // int(interval)
            repost_plan = f"Every {_period_label(int(interval) // 60)}"
        else:
            expected_cycles = 1
            repost_plan = "Post once"
        next_cycle_text = (
            "No more reposts; waiting for campaign end"
            if status == CampaignStatus.ACTIVE.value and int(campaign.get("next_cycle_number", 0)) >= expected_cycles
            else self._date(campaign.get("next_cycle_at"), display_timezone)
            if status in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value}
            else "None while paused/ending"
            if status in {CampaignStatus.PAUSED.value, CampaignStatus.ENDING.value}
            else "Complete"
        )
        coverage_text = ""
        if campaign.get("mode") != CampaignMode.STANDARD.value and variants:
            completed_cycles = int(cycle_stats.get("completed", 0))
            full_passes, current_pass_cycles = divmod(completed_cycles, len(variants))
            coverage_text = (
                f"Variant coverage: {self._bar(current_pass_cycles, len(variants), 8)}  "
                f"{current_pass_cycles}/{len(variants)} cycles in current pass; {full_passes} full pass"
                f"{'es' if full_passes != 1 else ''} complete\n"
            )
        adjustment_text = (
            "Timing auto-fit: " + "; ".join(campaign.get("rotation_adjustment_notes", [])) + "\n"
            if campaign.get("rotation_adjustment_notes")
            else ""
        )
        cleanup_text = ""
        if status == CampaignStatus.ENDING.value:
            cleanup_waiting = sum(cleanup_statuses.get(item, 0) for item in ("PENDING", "PROCESSING", "RETRY_WAIT"))
            blocked = cleanup_statuses.get("CLEANUP_FAILED", 0)
            cleanup_text = (
                f"Cleanup: {live_count} tracked posts live  |  {cleanup_waiting} queued/retrying  |  {blocked} blocked\n"
            )
            if cleanup_failures_by_category:
                labels = {
                    "ACCESS_OR_PERMISSION": "access",
                    "DELETE_NOT_ALLOWED": "Telegram restriction",
                    "RETRY_EXHAUSTED": "temporary retry exhausted",
                }
                reasons = ", ".join(
                    f"{labels.get(category, str(category).replace('_', ' ').lower())}: {count}"
                    for category, count in sorted(cleanup_failures_by_category.items())
                )
                cleanup_text += f"Blocked by: {reasons}. Open View cleanup issues for the affected channels.\n"
        latest_text = "Latest cycle: not created yet"
        if latest_cycle:
            latest = latest_cycle.get("delivery_counts", {})
            target_count = int(latest_cycle.get("target_count", 0))
            latest_sent = int(latest.get("SENT", 0))
            reachability = round(100 * latest_sent / target_count) if target_count else 0
            latest_text = (
                f"Latest cycle {int(latest_cycle['cycle_number']) + 1}: "
                f"{latest_sent} sent, {latest.get('PENDING', 0)} pending, {latest.get('PROCESSING', 0)} processing, "
                f"{latest.get('RETRY_WAIT', 0)} retrying, {latest.get('FAILED_PERMANENT', 0)} failed, "
                f"{latest.get('UNKNOWN_SEND_STATE', 0)} unknown\n"
                f"Reachability: {reachability}%  |  Started: {self._date(latest_cycle.get('started_at'), display_timezone)}  |  "
                f"Completed: {self._date(latest_cycle.get('completed_at'), display_timezone)}"
            )
        await self._render(
            message,
            f"{campaign['name']}\n"
            f"{status.title()} • {campaign.get('mode', 'STANDARD').replace('_', ' ').title()}\n\n"
            f"Variants: {len(variants)}  |  CTA buttons: {button_count}\n"
            f"Destinations: {len(destinations)}\n"
            f"Targets: {target_text}\n"
            f"Window: {self._date(campaign.get('start_at_utc'), display_timezone)} → "
            f"{self._date(campaign.get('current_end_at_utc'), display_timezone)}\n"
            f"End behavior: {cleanup}\n\n"
            f"Repost plan: {repost_plan}\n"
            f"Next cycle: {next_cycle_text}\n\n"
            f"{adjustment_text}"
            f"{coverage_text}"
            f"{report_heading}\n"
            f"Deliveries: {delivery_progress}\n"
            f"Sent: {sent}  |  Pending: {pending}  |  Failed: {permanent_failed}  |  Unknown: {unknown}\n"
            f"Deleted posts: {metrics.get('replaced_messages', 0) + metrics.get('cleaned_messages', 0)}"
            f"  |  Cleanup failed: {cleanup_failed}  |  Cancelled: {cancelled}\n"
            f"{cleanup_text}"
            f"Cycles: {cycle_stats.get('completed', 0)}/{expected_cycles} complete "
            f"({cycle_stats.get('planned', 0)} created)  |  Attempts: {metrics.get('attempts', 0)}\n"
            f"Posts currently live: {live_count}  |  Tracked joins: {join_count}\n"
            f"Excluded destinations: {len(campaign.get('protected_destination_ids', []))}\n"
            f"{latest_text}\n"
            f"Campaign time: {timeline}  |  Run time: {run_time}\n\n"
            f"State: {phase}.",
            campaign_keyboard(campaign["campaign_id"], status, variant_count=len(variants), has_live_posts=live_count > 0),
        )

    async def _show_network(self, message: Message, *, notice: str | None = None) -> None:
        counts = await self.repositories.channel_status_counts()
        text = (
            (f"{notice}\n\n" if notice else "") + "Network registry\n\n"
            f"Active: {counts.get('ACTIVE', 0)}\n"
            f"Needs attention: {counts.get('NEEDS_ATTENTION', 0)}\n"
            f"Unavailable: {counts.get('UNAVAILABLE', 0)}\n"
            f"Paused manually: {counts.get('INACTIVE_MANUAL', 0)}\n"
            f"Total discovered: {sum(counts.values())}\n\n"
            "Add me as a channel admin to register automatically, or forward a channel post here to repair/register it."
        )
        controls = [
            [
                InlineKeyboardButton(text=f"Active ({counts.get('ACTIVE', 0)})", callback_data="net:list:ACTIVE:0"),
                InlineKeyboardButton(text=f"Attention ({counts.get('NEEDS_ATTENTION', 0)})", callback_data="net:list:NEEDS_ATTENTION:0"),
            ],
            [
                InlineKeyboardButton(text=f"Unavailable ({counts.get('UNAVAILABLE', 0)})", callback_data="net:list:UNAVAILABLE:0"),
                InlineKeyboardButton(text=f"Paused ({counts.get('INACTIVE_MANUAL', 0)})", callback_data="net:list:INACTIVE_MANUAL:0"),
            ],
        ]
        if counts.get("NEEDS_ATTENTION", 0):
            controls.append(
                [
                    InlineKeyboardButton(
                        text=f"Refresh all attention ({counts.get('NEEDS_ATTENTION', 0)})",
                        callback_data="net:refresh_attention",
                    )
                ]
            )
        controls.extend(
            [
                [InlineKeyboardButton(text="Top channels by subscribers", callback_data="net:top:15")],
                [InlineKeyboardButton(text="Forward post to register", callback_data="net:forward")],
                [InlineKeyboardButton(text="Back", callback_data="home:back")],
            ]
        )
        await self._render(
            message,
            text,
            _markup(controls),
        )

    async def _show_network_list(self, message: Message, status: str, page: int) -> None:
        page_size = 8
        rows = await self.repositories.list_channels(status, skip=page * page_size, limit=page_size)
        if not rows:
            await self._render(
                message,
                "No channels in this group.",
                _markup(
                    [
                        [InlineKeyboardButton(text="Back to Network", callback_data="net:home")],
                    ]
                ),
            )
            return
        lines = [f"{status.replace('_', ' ').title()} channels (page {page + 1})", ""]
        controls: list[list[InlineKeyboardButton]] = []
        for index, channel in enumerate(rows, start=1):
            username = f"@{channel['username']}" if channel.get("username") else "private"
            lines.append(f"{index}. {channel.get('title', channel['telegram_chat_id'])}\n   {username} | {channel.get('member_count', '?')} members")
            controls.append([InlineKeyboardButton(text=f"Open channel {index}", callback_data=f"chan:{channel['telegram_chat_id']}:view")])
        if status == ChannelStatus.NEEDS_ATTENTION.value:
            controls.append([InlineKeyboardButton(text="Refresh all attention", callback_data="net:refresh_attention")])
        nav: list[InlineKeyboardButton] = []
        if page:
            nav.append(InlineKeyboardButton(text="Previous", callback_data=f"net:list:{status}:{page - 1}"))
        if len(rows) == page_size:
            nav.append(InlineKeyboardButton(text="Next", callback_data=f"net:list:{status}:{page + 1}"))
        if nav:
            controls.append(nav)
        controls.append([InlineKeyboardButton(text="Back to Network", callback_data="net:home")])
        await self._render(message, "\n".join(lines), _markup(controls))

    async def _show_top_channels(self, message: Message, limit: int) -> None:
        limit = 30 if limit >= 30 else 15
        channels = await self.repositories.top_channels_by_members(limit)
        known_count, unknown_count = await self.repositories.channel_member_count_coverage()
        if not channels:
            await self._render(
                message,
                "Top channels by subscribers\n\nNo registered channel has a verified subscriber count yet. "
                "Open a channel from Network and use Refresh access to collect it.",
                _markup(_navigation("net:home")),
            )
            return
        lines = [f"Top {limit} channels by subscribers", ""]
        for rank, channel in enumerate(channels, start=1):
            title = " ".join(str(channel.get("title") or channel["telegram_chat_id"]).split())[:36]
            username = f"@{channel['username']}" if channel.get("username") else "private"
            status = str(channel.get("status") or "UNKNOWN").replace("_", " ").title()
            lines.append(
                f"{rank}. {title} — {int(channel['member_count']):,} subscribers • {username} • {status}"
            )
        lines.extend(
            [
                "",
                f"Ranked from {known_count:,} channels with verified counts. "
                f"{unknown_count:,} channel{'s are' if unknown_count != 1 else ' is'} omitted because the count is unknown.",
                "Counts reflect the last successful channel refresh and are not live analytics.",
            ]
        )
        controls = [
            [
                InlineKeyboardButton(
                    text="Show top 15" if limit == 30 else "Show top 30",
                    callback_data=f"net:top:{15 if limit == 30 else 30}",
                )
            ],
            *_navigation("net:home"),
        ]
        await self._render(message, "\n".join(lines), _markup(controls))

    async def _show_channel(self, message: Message, chat_id: int) -> None:
        channel = await self.repositories.get_channel(chat_id)
        if not channel:
            await self._render(message, "That channel is no longer in the registry.", _markup(_navigation("net:home")))
            return
        permissions = channel.get("permissions", {})
        text = (
            f"{channel.get('title', chat_id)}\nStatus: {channel.get('status')}\nUsername: @{channel['username']}"
            if channel.get("username")
            else f"{channel.get('title', chat_id)}\nStatus: {channel.get('status')}\nUsername: private"
        )
        text += (
            f"\nMembers: {channel.get('member_count', 'unknown')}\n"
            f"Can post: {'yes' if permissions.get('can_post_messages') else 'no'}\n"
            f"Tags: {', '.join(channel.get('tags', [])) or 'none'}"
        )
        enabled = channel.get("status") != "INACTIVE_MANUAL"
        await self._render(
            message,
            text,
            _markup(
                [
                    [
                        InlineKeyboardButton(text="Refresh access", callback_data=f"chan:{chat_id}:refresh"),
                        InlineKeyboardButton(text="Tag", callback_data=f"chan:{chat_id}:tag"),
                    ],
                    [
                        InlineKeyboardButton(
                            text="Pause source" if enabled else "Enable source", callback_data=f"chan:{chat_id}:{'disable' if enabled else 'enable'}"
                        )
                    ],
                    [InlineKeyboardButton(text="Back to Network", callback_data="net:home")],
                ]
            ),
        )

    async def _show_button_editor(self, message: Message, campaign: Document, variant_index: int) -> None:
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        if not variants:
            await message.answer("Add Variant 1 first; CTA buttons attach to a saved variant.", reply_markup=content_type_keyboard(campaign["campaign_id"]))
            return
        creative = variants[variant_index]
        if creative.buttons:
            by_row: dict[int, list[str]] = {}
            for button in sorted(creative.buttons, key=lambda item: (item.row, item.position)):
                by_row.setdefault(button.row, []).append(f"{button.text} [{_cta_style_label(button.style)}]")
            canvas = "\n".join(f"Row {row + 1}: {' | '.join(labels)}" for row, labels in by_row.items())
        else:
            canvas = "No CTA buttons yet."
        cid = campaign["campaign_id"]
        rows: list[list[InlineKeyboardButton]] = []
        if not creative.buttons:
            rows.append([InlineKeyboardButton(text="+ Add first CTA", callback_data=f"btn:{cid}:{variant_index}:first")])
        else:
            rows.append(
                [
                    InlineKeyboardButton(text="+ Add beside last", callback_data=f"btn:{cid}:{variant_index}:right"),
                    InlineKeyboardButton(text="+ Add new row", callback_data=f"btn:{cid}:{variant_index}:below"),
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(text="Auto layout", callback_data=f"layout:{cid}:{variant_index}:AUTO"),
                    InlineKeyboardButton(text="1 per row", callback_data=f"layout:{cid}:{variant_index}:1"),
                ],
                [
                    InlineKeyboardButton(text="2 per row", callback_data=f"layout:{cid}:{variant_index}:2"),
                    InlineKeyboardButton(text="3 per row", callback_data=f"layout:{cid}:{variant_index}:3"),
                ],
            ]
        )
        for button in creative.buttons:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Color: {button.text} ({_cta_style_label(button.style)})",
                        callback_data=f"toneedit:{cid}:{variant_index}:{button.id}",
                    ),
                    InlineKeyboardButton(text="Remove", callback_data=f"rm:{cid}:{variant_index}:{button.id}"),
                ]
            )
        rows.append([InlineKeyboardButton(text="Send real preview", callback_data=f"preview:{cid}:{variant_index}")])
        rows.append([InlineKeyboardButton(text="Back to campaign", callback_data=f"c:{cid}:open")])
        await self._render(
            message,
            f"CTA button editor - variant {variant_index + 1}\n\n{canvas}\n\n"
            "Use beside-last for a horizontal button and new-row for a vertical button. "
            "Tap Color to choose Telegram's native blue, green, red, or neutral style.",
            _markup(rows),
        )

    async def start(self, message: Message) -> None:
        if self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            await self.repositories.clear_owner_session(message.from_user.id)
            await self._show_home(message)

    async def backup(self, message: Message) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        payload = await make_backup(self.repositories)
        await message.answer_document(
            BufferedInputFile(payload, filename="iharvester-core-backup.json.gz"),
            caption="Core backup: channels, campaign definitions, and settings.",
            reply_markup=home_keyboard(),
        )

    async def restore(self, message: Message) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        await self.repositories.set_owner_session(message.from_user.id, {"action": "await_restore_file"})
        await message.answer(
            "Attach an iHarvester .json.gz backup. I will validate it before restoring.",
            reply_markup=_markup(_navigation()),
        )

    async def inline_query(self, query: InlineQuery) -> None:
        """Resolve one owner-authorized, immutable variant snapshot by code."""
        if not self._allowed_inline(query.from_user.id):
            await query.answer(results=[], cache_time=0, is_personal=True)
            return
        raw_code = (query.query or "").strip().strip("\"'")
        code = normalize_share_code(raw_code.split(maxsplit=1)[0] if raw_code else "")
        share = await self.repositories.get_variant_share(code, query.from_user.id) if code else None
        if not share:
            await query.answer(
                results=[],
                cache_time=0,
                is_personal=True,
                button=InlineQueryResultsButton(text="Open iHarvester", start_parameter="shares"),
            )
            return
        try:
            result = inline_result_for_share(share)
        except UnsupportedInlineCreative:
            logger.warning(
                "Unsupported creative reached inline lookup",
                extra={"share_code": code, "creative_kind": share.get("creative", {}).get("kind")},
            )
            await query.answer(
                results=[],
                cache_time=0,
                is_personal=True,
                button=InlineQueryResultsButton(text="Open iHarvester", start_parameter="shares"),
            )
            return
        await query.answer(results=[result], cache_time=0, is_personal=True)
        try:
            await self.repositories.record_variant_share_query(code, query.from_user.id)
        except Exception:
            # Usage telemetry must never turn a successfully answered inline
            # query into a webhook retry of the same one-shot query ID.
            logger.exception("Could not record manual-share inline lookup", extra={"share_code": code})

    async def callback(self, query: CallbackQuery, bot: Bot) -> None:
        if not query.message or not self._allowed(query.from_user.id, query.message.chat.type):
            await query.answer("Owner-only private control.", show_alert=True)
            return
        data = query.data or ""
        await self._retire(query)
        try:
            if data.startswith("home:"):
                await self._home(query, data.split(":", 1)[1])
            elif data.startswith("c:"):
                _, campaign_id, action = data.split(":", 2)
                await self._campaign_action(query, campaign_id, action)
            elif data.startswith("req:"):
                _, request_id, action = data.split(":", 2)
                await self._client_request_action(query, request_id, action)
            elif data.startswith(("mode:", "m:")):
                _, campaign_id, mode = data.split(":", 2)
                await self._set_mode(query, campaign_id, mode)
            elif data.startswith("ct:"):
                _, campaign_id, kind = data.split(":", 2)
                prompts = {
                    "TEXT": "Send the formatted text post.",
                    "PHOTO": "Send the photo. A caption is optional.",
                    "PHOTO_TEXT": "Send the photo with its caption.",
                    "VIDEO": "Send the video. A caption is optional.",
                    "VIDEO_TEXT": "Send the video with its caption.",
                    "MEDIA": "Send a supported photo, video, GIF, document, audio, voice, sticker, or video note.",
                    "ALBUM": "Send 2-10 photos or videos. Tap Finish album after the last item.",
                    "FORWARD": "Forward the finished post. Its formatting and media will be captured for replay.",
                }
                if kind == "ALBUM":
                    await self.repositories.set_owner_session(
                        query.from_user.id,
                        {"action": "await_album", "campaign_id": campaign_id, "media": [], "caption": None, "caption_entities": []},
                    )
                else:
                    await self.repositories.set_owner_session(query.from_user.id, {"action": "await_creative", "campaign_id": campaign_id, "kind": kind})
                await query.message.answer(prompts[kind], reply_markup=_markup(_navigation(f"c:{campaign_id}:open")))
            elif data.startswith("edit:"):
                _, campaign_id, index = data.split(":", 2)
                campaign = await self.campaigns.editable_campaign(campaign_id)
                await self._show_button_editor(query.message, campaign, int(index))
            elif data.startswith("var:"):
                _, campaign_id, index, action = data.split(":", 3)
                await self._variant_action(query, campaign_id, int(index), action)
            elif data.startswith("share:"):
                _, campaign_id, index, action, share_code = data.split(":", 4)
                await self._share_action(query, campaign_id, int(index), action, share_code)
            elif data.startswith("btn:"):
                _, campaign_id, index, placement = data.split(":", 3)
                await self.repositories.set_owner_session(
                    query.from_user.id, {"action": "await_button_label", "campaign_id": campaign_id, "variant_index": int(index), "placement": placement}
                )
                await query.message.answer(
                    "Send the CTA button label exactly as you want viewers to see it.",
                    reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
                )
            elif data.startswith("toneedit:"):
                _, campaign_id, index, button_id = data.split(":", 3)
                await self._begin_button_style_edit(query, campaign_id, int(index), button_id)
            elif data.startswith("tone:"):
                _, style = data.split(":", 1)
                await self._apply_button_style(query, style)
            elif data.startswith("album:"):
                _, campaign_id, action = data.split(":", 2)
                if action != "finish":
                    raise ValueError("unknown album action")
                await self._finish_album(query, campaign_id)
            elif data.startswith("layout:"):
                _, campaign_id, index, layout = data.split(":", 3)
                await self._set_button_layout(query, campaign_id, int(index), layout)
            elif data.startswith("rm:"):
                _, campaign_id, index, button_id = data.split(":", 3)
                await self._remove_button(query, campaign_id, int(index), button_id)
            elif data.startswith("preview:"):
                _, campaign_id, index = data.split(":", 2)
                await self._preview_variant(query, campaign_id, int(index))
            elif data.startswith("net:"):
                if data == "net:refresh_attention":
                    try:
                        await query.answer("Refreshing channels…")
                    except Exception:
                        logger.warning("Could not acknowledge bulk refresh callback")
                await self._network_action(query, bot, data)
            elif data.startswith("chan:"):
                _, chat_id, action = data.split(":", 2)
                await self._channel_action(query, bot, int(chat_id), action)
            elif data.startswith("target:"):
                _, campaign_id, action = data.split(":", 2)
                await self._target_action(query, campaign_id, action)
            elif data.startswith("ret:"):
                _, campaign_id, action = data.split(":", 2)
                await self._set_retention(query, campaign_id, action)
            elif data.startswith("dest:"):
                _, campaign_id, action = data.split(":", 2)
                await self._destination_action(query, campaign_id, action)
            elif data.startswith("times:"):
                _, flow, campaign_id = data.split(":", 2)
                await self._specific_repost_times(query, campaign_id, flow)
            elif data.startswith("gaps:"):
                _, flow, campaign_id = data.split(":", 2)
                await self._custom_repost_gaps(query, campaign_id, flow)
            elif data.startswith("sint:"):
                _, campaign_id, value = data.split(":", 2)
                await self._schedule_interval(query, campaign_id, value)
            elif data.startswith("sch:"):
                # Compatibility with buttons emitted before intervals used
                # minute-based callbacks. Those values were whole hours.
                _, campaign_id, value = data.split(":", 2)
                await self._schedule_interval(query, campaign_id, value if value == "custom" else str(int(value) * 60))
            elif data.startswith("dur:"):
                _, campaign_id, value = data.split(":", 2)
                if value == "custom":
                    await self._custom_duration(query, campaign_id)
                else:
                    await self._quick_duration(query, campaign_id, int(value))
            elif data.startswith("qmin:"):
                _, campaign_id, value = data.split(":", 2)
                await self._quick_interval(query, campaign_id, value)
            elif data.startswith("qint:"):
                # Compatibility with old quick-send buttons, which encoded hours.
                _, campaign_id, value = data.split(":", 2)
                await self._quick_interval(query, campaign_id, value if value == "custom" else str(int(value) * 60))
            elif data.startswith("set:"):
                _, action, value = data.split(":", 2)
                await self._settings_action(query, action, value)
            elif data.startswith("confirm:"):
                _, campaign_id, action = data.split(":", 2)
                await self._confirm(query, campaign_id, action)
            elif data.startswith("reg:"):
                _, chat_id, action = data.split(":", 2)
                await self._channel_action(query, bot, int(chat_id), action)
            elif data.startswith("restore:"):
                _, restore_id, action = data.split(":", 2)
                await self._restore_confirm(query, restore_id, action)
            else:
                await query.message.answer(
                    "That control has expired. Choose where to continue.",
                    reply_markup=self._recovery_keyboard(data),
                )
        except (ValueError, KeyError, ValidationError) as error:
            await query.message.answer(f"Could not complete that action: {error}", reply_markup=self._recovery_keyboard(data))
        except Exception:
            logger.exception("Owner callback failed", extra={"callback_data": data, "owner_id": query.from_user.id})
            network_action = data.startswith(("net:", "chan:", "reg:"))
            await query.message.answer(
                "I could not complete that network action because of an internal error. Return to Network and retry."
                if network_action
                else "I could not confirm that action because of an internal error. "
                "Open the campaign to check its saved state; duplicate launches are blocked safely.",
                reply_markup=self._recovery_keyboard(data),
            )
        try:
            if data != "net:refresh_attention":
                await query.answer()
        except Exception:
            # Callback acknowledgements expire quickly. The state-changing
            # action above is already complete and must not be replayed merely
            # because Telegram no longer accepts the cosmetic acknowledgement.
            logger.warning("Could not acknowledge completed owner callback", extra={"callback_data": data})

    @staticmethod
    def _recovery_keyboard(callback_data: str) -> InlineKeyboardMarkup:
        parts = callback_data.split(":")
        rows: list[list[InlineKeyboardButton]] = []
        campaign_prefixes = {
            "c",
            "mode",
            "m",
            "ct",
            "edit",
            "btn",
            "album",
            "layout",
            "rm",
            "preview",
            "var",
            "share",
            "target",
            "ret",
            "dest",
            "times",
            "gaps",
            "sint",
            "sch",
            "dur",
            "qmin",
            "qint",
            "confirm",
        }
        campaign_part = 2 if parts and parts[0] in {"times", "gaps"} else 1
        if len(parts) > campaign_part and parts[0] in campaign_prefixes and parts[campaign_part]:
            rows.append([InlineKeyboardButton(text="Return to campaign", callback_data=f"c:{parts[campaign_part]}:open")])
        if parts and parts[0] in {"net", "chan", "reg"}:
            rows.append([InlineKeyboardButton(text="Network", callback_data="net:home")])
        rows.append(
            [
                InlineKeyboardButton(text="Campaigns", callback_data="home:campaigns:0"),
                InlineKeyboardButton(text="Home", callback_data="home:back"),
            ]
        )
        return _markup(rows)

    async def _home(self, query: CallbackQuery, action: str) -> None:
        # Moving between top-level areas is an explicit cancellation point for
        # a pending text-entry step.  Otherwise an old message sent after
        # navigating home could accidentally be consumed by that old step.
        action_name, _, page_value = action.partition(":")
        if action_name != "create":
            await self.repositories.clear_owner_session(query.from_user.id)
        if action_name == "create":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_campaign_name"})
            await query.message.answer("What should this campaign be called?", reply_markup=_markup(_navigation()))
        elif action_name == "network":
            await self._show_network(query.message)
        elif action_name == "campaigns":
            page = max(0, int(page_value or 0))
            page_size = 8
            rows = await self.repositories.list_campaigns(page_size, skip=page * page_size)
            if not rows:
                await self._render(query.message, "No campaigns on this page.", home_keyboard())
                return
            buttons: list[list[InlineKeyboardButton]] = []
            for row in rows:
                campaign_id = row["campaign_id"]
                status = row["status"]
                icon = {"DRAFT": "📝", "SCHEDULED": "🕒", "ACTIVE": "🟢", "PAUSED": "⏸", "ENDING": "⌛", "ARCHIVED": "✓"}.get(status, "•")
                buttons.append([InlineKeyboardButton(text=f"{icon} {row['name']}", callback_data=f"c:{campaign_id}:open")])
                if status == CampaignStatus.DRAFT.value:
                    buttons.append(
                        [
                            InlineKeyboardButton(text="Send", callback_data=f"c:{campaign_id}:send"),
                            InlineKeyboardButton(text="Delete", callback_data=f"c:{campaign_id}:delete"),
                        ]
                    )
                elif status in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value, CampaignStatus.ENDING.value, CampaignStatus.PAUSED.value}:
                    buttons.append(
                        [
                            InlineKeyboardButton(text="Progress", callback_data=f"c:{campaign_id}:progress"),
                            InlineKeyboardButton(
                                text="Resume" if status == CampaignStatus.PAUSED.value else "End",
                                callback_data=f"c:{campaign_id}:{'resume' if status == CampaignStatus.PAUSED.value else 'end'}",
                            ),
                        ]
                    )
                elif status == CampaignStatus.ARCHIVED.value:
                    buttons.append(
                        [
                            InlineKeyboardButton(text="Run again", callback_data=f"c:{campaign_id}:rerun"),
                            InlineKeyboardButton(text="Report", callback_data=f"c:{campaign_id}:progress"),
                        ]
                    )
                    buttons.append([InlineKeyboardButton(text="Delete", callback_data=f"c:{campaign_id}:delete")])
            nav: list[InlineKeyboardButton] = []
            if page:
                nav.append(InlineKeyboardButton(text="Previous", callback_data=f"home:campaigns:{page - 1}"))
            if len(rows) == page_size:
                nav.append(InlineKeyboardButton(text="Next", callback_data=f"home:campaigns:{page + 1}"))
            if nav:
                buttons.append(nav)
            buttons.append([InlineKeyboardButton(text="Home", callback_data="home:back")])
            await self._render(query.message, f"Campaigns • page {page + 1}\nTap a campaign for its full controls.", _markup(buttons))
        elif action_name == "requests":
            await self._show_client_requests(query.message, query.from_user.id)
        elif action_name == "back":
            await self._show_home(query.message)
        elif action_name == "backups":
            await self._render(
                query.message,
                "Backups\n\nExport channels, campaign definitions, and settings, or restore a validated iHarvester backup.",
                _markup(
                    [
                        [InlineKeyboardButton(text="Download core backup", callback_data="home:backup_download")],
                        [InlineKeyboardButton(text="Restore from file", callback_data="home:restore_upload")],
                        *_navigation(),
                    ]
                ),
            )
        elif action_name == "backup_download":
            payload = await make_backup(self.repositories)
            await query.message.answer_document(
                BufferedInputFile(payload, filename="iharvester-core-backup.json.gz"),
                caption="Core backup created.",
                reply_markup=_markup(_navigation("home:backups")),
            )
        elif action_name == "restore_upload":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_restore_file"})
            await query.message.answer(
                "Attach an iHarvester .json.gz backup. I will validate it before restoring.",
                reply_markup=_markup(_navigation()),
            )
        elif action_name == "settings":
            await self._show_settings(query.message)
        else:
            raise ValueError("that home control is no longer valid")

    async def _share_client_portal(self, query: CallbackQuery, campaign: Document) -> None:
        if not self.public_base_url:
            raise ValueError("client pages need PUBLIC_BASE_URL or KOYEB_PUBLIC_DOMAIN to be configured")
        _, token = await self.repositories.create_campaign_portal_share(query.from_user.id, campaign["campaign_id"])
        url = f"{self.public_base_url}/client/c/{token}"
        await query.message.answer(
            "Client progress page created. It updates its delivery and campaign-time bars automatically while the page is open. "
            "Anyone with this private link can see only this campaign's aggregate progress and submit an approval-gated request.",
            reply_markup=_markup(
                [
                    [InlineKeyboardButton(text="Copy client link", copy_text=CopyTextButton(text=url))],
                    [InlineKeyboardButton(text="Open campaign", callback_data=f"c:{campaign['campaign_id']}:open")],
                    [InlineKeyboardButton(text="Revoke all client links", callback_data=f"c:{campaign['campaign_id']}:revokeclient")],
                ]
            ),
        )

    async def _show_client_requests(self, message: Message, owner_id: int) -> None:
        requests = await self.repositories.list_client_promotion_requests(owner_id)
        if not requests:
            await self._render(
                message,
                "Client requests\n\nNothing is waiting for review. Shared campaign pages let clients request a repeat run or send new-promotion materials.",
                _markup(_navigation()),
            )
            return
        lines = ["Client requests", ""]
        rows: list[list[InlineKeyboardButton]] = []
        for index, request in enumerate(requests, start=1):
            state = str(request.get("status", "")).replace("_", " ").title()
            client = str(request.get("client_name") or "Unnamed client")
            kind = str(request.get("request_type") or "").title()
            materials = len(request.get("materials") or [])
            lines.append(f"{index}. {client} · {kind} · {state} · {materials} material{'s' if materials != 1 else ''}")
            rows.append([InlineKeyboardButton(text=f"Open request {index}", callback_data=f"req:{request['request_id']}:open")])
        rows.extend(_navigation())
        await self._render(message, "\n".join(lines), _markup(rows))

    async def _client_request_action(self, query: CallbackQuery, request_id: str, action: str) -> None:
        request = await self.repositories.get_client_promotion_request(request_id, query.from_user.id)
        if not request:
            raise ValueError("that client request is no longer available")
        if action == "open":
            await self._show_client_request(query.message, request)
            return
        if action == "materials":
            materials = request.get("materials") or []
            if not materials:
                await query.message.answer("No materials have been submitted yet.")
                return
            await query.message.answer(f"Showing {len(materials)} submitted material item{'s' if len(materials) != 1 else ''}.")
            for material in materials:
                await self.sender.send_variant(query.from_user.id, material)
            return
        if action == "reject":
            if not await self.repositories.resolve_client_promotion_request(request_id, query.from_user.id, status="REJECTED"):
                raise ValueError("that request was already resolved")
            await query.message.answer("Client request rejected.", reply_markup=home_keyboard())
            return
        if action != "approve":
            raise ValueError("that client request control is no longer valid")
        if request.get("status") != "PENDING_APPROVAL":
            raise ValueError("wait for the client to finish submitting materials before approving")
        if request.get("request_type") == "RERUN":
            activated = await self._approve_client_rerun(request, query.from_user.id)
            if not await self.repositories.resolve_client_promotion_request(
                request_id,
                query.from_user.id,
                status="APPROVED",
                approved_campaign_id=activated["campaign_id"],
            ):
                raise ValueError("that request was already resolved")
            notice = await query.message.answer(
                f"Approved. {activated['name']} is now {activated['status']}; its saved variants, audience rules, repost plan, and end behavior were retained."
            )
            await self._show_campaign(notice, activated)
            return
        draft = await self._approve_client_new_request(request, query.from_user.id)
        if not await self.repositories.resolve_client_promotion_request(
            request_id,
            query.from_user.id,
            status="APPROVED",
            approved_campaign_id=draft["campaign_id"],
        ):
            raise ValueError("that request was already resolved")
        notice = await query.message.answer(
            "Approved into a prepared draft. Review the submitted variants, choose timing, preview, then schedule or launch it."
        )
        await self._show_campaign(notice, draft)

    async def _show_client_request(self, message: Message, request: Document) -> None:
        start = request.get("desired_start_at_utc")
        timezone = str(request.get("client_timezone") or "UTC")
        lines = [
            "Client promotion request",
            "",
            f"Client: {request.get('client_name') or 'not supplied'}",
            f"Request: {str(request.get('request_type') or '').title()}",
            f"Status: {str(request.get('status') or '').replace('_', ' ').title()}",
            f"Preferred start: {self._date(start, timezone) if start else 'Manager chooses'}",
            f"Materials: {len(request.get('materials') or [])}",
            f"Notes: {request.get('details') or 'None'}",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        if request.get("materials"):
            rows.append([InlineKeyboardButton(text="View submitted materials", callback_data=f"req:{request['request_id']}:materials")])
        if request.get("status") == "PENDING_APPROVAL":
            approve_label = "Approve & schedule rerun" if request.get("request_type") == "RERUN" else "Approve into draft"
            rows.append([InlineKeyboardButton(text=approve_label, callback_data=f"req:{request['request_id']}:approve")])
        if request.get("status") in {"PENDING_APPROVAL", "AWAITING_MATERIALS"}:
            rows.append([InlineKeyboardButton(text="Reject request", callback_data=f"req:{request['request_id']}:reject")])
        rows.extend(_navigation("home:requests"))
        await self._render(message, "\n".join(lines), _markup(rows))

    async def _approve_client_rerun(self, request: Document, owner_id: int) -> Document:
        source = await self.repositories.get_campaign(request["campaign_id"])
        if not source:
            raise ValueError("the source campaign was deleted")
        draft = await self.campaigns.fork_to_draft(source["campaign_id"], owner_id)
        initial_start = as_utc(draft["start_at_utc"])
        duration = max(timedelta(minutes=1), as_utc(draft["current_end_at_utc"]) - initial_start)
        requested = request.get("desired_start_at_utc")
        start = as_utc(requested) if requested and as_utc(requested) > datetime.now(UTC) else datetime.now(UTC)
        await self.repositories.update_campaign(
            draft["campaign_id"],
            {
                "start_at_utc": start,
                "original_end_at_utc": start + duration,
                "current_end_at_utc": start + duration,
                "rerun_ready": False,
                "client_request_id": request["request_id"],
                "updated_at": datetime.now(UTC),
            },
        )
        return await self.campaigns.activate(draft["campaign_id"])

    async def _approve_client_new_request(self, request: Document, owner_id: int) -> Document:
        materials = list(request.get("materials") or [])
        if not materials:
            raise ValueError("the client has not submitted any material")
        source = await self.repositories.get_campaign(request["campaign_id"])
        name = f"{request.get('client_name') or 'Client'} promotion"
        draft = await self.campaigns.create_draft(owner_id, name)
        await self.repositories.update_campaign(
            draft["campaign_id"],
            {
                "variants": materials,
                "target_selector": deepcopy(source.get("target_selector", {})) if source else {},
                "client_request_id": request["request_id"],
                "client_requested_start_at_utc": request.get("desired_start_at_utc"),
                "client_notes": request.get("details", ""),
                "preview_sent": False,
                "updated_at": datetime.now(UTC),
            },
        )
        return await self.repositories.get_campaign(draft["campaign_id"]) or draft

    async def _campaign_action(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("campaign no longer exists")
        if action in {"open", "edit"}:
            await self.repositories.clear_owner_session(query.from_user.id)
            await self._show_campaign(query.message, campaign)
        elif action == "add":
            await self.campaigns.editable_campaign(campaign_id)
            next_number = len(campaign.get("variants", [])) + 1
            await query.message.answer(
                f"Add Variant {next_number}\n\nWhat kind of post should this variant contain?",
                reply_markup=content_type_keyboard(campaign_id),
            )
        elif action == "client":
            await self._share_client_portal(query, campaign)
        elif action == "revokeclient":
            revoked = await self.repositories.revoke_campaign_portal_shares(campaign_id, query.from_user.id)
            await query.message.answer(
                f"Revoked {revoked} client progress link{'s' if revoked != 1 else ''}.",
                reply_markup=campaign_keyboard(
                    campaign_id,
                    campaign["status"],
                    variant_count=len(campaign.get("variants", [])),
                    has_live_posts=bool(await self.repositories.campaign_live_state_count(campaign_id)),
                    has_schedule=bool(campaign.get("start_at_utc") and campaign.get("current_end_at_utc")),
                ),
            )
        elif action == "rename":
            await self.campaigns.editable_campaign(campaign_id)
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_campaign_rename", "campaign_id": campaign_id})
            await query.message.answer(
                "Send the new campaign name.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "variants":
            await self.repositories.clear_owner_session(query.from_user.id)
            await self._show_variants(query.message, campaign)
        elif action in {"buttons", "button"}:
            await self._show_button_editor(query.message, campaign, max(0, len(campaign.get("variants", [])) - 1))
        elif action == "destination":
            await self._show_destinations(query.message, campaign)
        elif action == "targets":
            await query.message.answer(
                "Choose the source channels for this campaign. Promoted destinations remain excluded automatically.", reply_markup=target_keyboard(campaign_id)
            )
        elif action == "mode":
            variant_count = len(campaign.get("variants", []))
            if variant_count < 2:
                if campaign.get("mode") != CampaignMode.STANDARD.value:
                    await self.repositories.update_campaign(
                        campaign_id,
                        {"mode": CampaignMode.STANDARD.value, "updated_at": datetime.now(UTC)},
                    )
                    campaign["mode"] = CampaignMode.STANDARD.value
                notice = await query.message.answer(
                    "This campaign has one variant, so I set it to Standard. Add another variant to unlock Rotate and Mix + Rotate."
                )
                await self._show_campaign(notice, campaign)
                return
            await query.message.answer("Choose how variants rotate across source channels.", reply_markup=mode_keyboard(campaign_id, variant_count))
        elif action == "schedule":
            timezone = await self.repositories.get_setting("owner_timezone", "UTC")
            await self.repositories.set_owner_session(
                query.from_user.id,
                {"action": "await_schedule_start", "campaign_id": campaign_id, "timezone": timezone},
            )
            await query.message.answer(
                f"For a future start, send YYYY-MM-DD HH:MM in {timezone}, for example 2026-09-02 09:30. "
                "I will show this timezone again before scheduling.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "send":
            if not campaign.get("variants"):
                raise ValueError("Add at least one campaign variant before sending.")
            await query.message.answer(
                "Send campaign\n\nWhen should the campaign end? It will start as soon as you confirm launch.",
                reply_markup=quick_duration_keyboard(campaign_id),
            )
        elif action == "preview":
            notice = await self._preview_all(query.message, query.from_user.id, campaign_id)
            await self._show_campaign(notice, await self.repositories.get_campaign(campaign_id))
        elif action == "test":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_test_channel", "campaign_id": campaign_id})
            await query.message.answer(
                "Send 1-3 owner-controlled test channel IDs, separated by commas. Every saved variant will be posted to each channel once.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "launch":
            await self._show_launch_confirmation(query.message, campaign_id)
        elif action == "extend6":
            await self.campaigns.extend(campaign_id, query.from_user.id, 6 * 3600)
            await self._show_campaign(query.message, await self.repositories.get_campaign(campaign_id))
        elif action == "extend24":
            await self.campaigns.extend(campaign_id, query.from_user.id, 24 * 3600)
            await self._show_campaign(query.message, await self.repositories.get_campaign(campaign_id))
        elif action == "extend72":
            await self.campaigns.extend(campaign_id, query.from_user.id, 72 * 3600)
            await self._show_campaign(query.message, await self.repositories.get_campaign(campaign_id))
        elif action == "extendcustom":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_campaign_extension", "campaign_id": campaign_id})
            await query.message.answer(
                "Send the extension, for example 45m, 3h, 3d, or 1mo.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "pause":
            paused = await self.campaigns.pause(campaign_id, query.from_user.id)
            await self._show_campaign(query.message, paused)
        elif action == "resume":
            resumed = await self.campaigns.resume(campaign_id, query.from_user.id)
            await self._show_campaign(query.message, resumed)
        elif action == "retention":
            await query.message.answer(
                "After the last repost, what should happen at campaign end? Stop & delete always overrides this choice.",
                reply_markup=retention_keyboard(
                    campaign_id,
                    campaign.get("delete_on_end", True),
                    campaign.get("delete_on_next_campaign", False),
                ),
            )
        elif action == "end":
            await query.message.answer(
                "End this campaign? It stops future cycles, deletes known live campaign posts, then archives the results.",
                reply_markup=_markup(
                    [
                        [
                            InlineKeyboardButton(text="Yes, end and clean up", callback_data=f"confirm:{campaign_id}:end"),
                            InlineKeyboardButton(text="Keep running", callback_data=f"c:{campaign_id}:open"),
                        ]
                    ]
                ),
            )
        elif action == "cleanup":
            changed = await self.repositories.mark_retained_campaign_ending(campaign_id)
            if not changed:
                raise ValueError("there are no retained posts available for cleanup on this campaign")
            notice = await query.message.answer(
                "Retained campaign posts are queued for deletion. Refresh the dashboard in a few seconds to see the final cleanup result."
            )
            await self._show_campaign(notice, await self.repositories.get_campaign(campaign_id))
        elif action == "duplicate":
            copied = await self.campaigns.duplicate(campaign_id, query.from_user.id)
            await self._show_campaign(query.message, copied)
        elif action == "rerun":
            copied = await self.campaigns.prepare_rerun(campaign_id, query.from_user.id)
            await self._show_launch_confirmation(query.message, copied["campaign_id"])
        elif action == "todraft":
            draft = await self.campaigns.return_to_draft(campaign_id, query.from_user.id)
            await self._show_campaign(query.message, draft)
        elif action == "refactor":
            copied = await self.campaigns.fork_to_draft(campaign_id, query.from_user.id)
            notice = await query.message.answer(
                "The running campaign remains unchanged. I created an editable draft copy so you can safely replace variants, buttons, targets, or schedule.",
            )
            await self._show_campaign(notice, copied)
        elif action == "delete":
            if campaign["status"] == CampaignStatus.DRAFT.value:
                await query.message.answer(
                    "Delete this draft? Its saved variants, CTA buttons, destinations, and schedule will be removed. This cannot be undone.",
                    reply_markup=_markup(
                        [
                            [
                                InlineKeyboardButton(text="Delete draft", callback_data=f"confirm:{campaign_id}:delete"),
                                InlineKeyboardButton(text="Keep draft", callback_data=f"c:{campaign_id}:open"),
                            ],
                        ]
                    ),
                )
            elif campaign["status"] == CampaignStatus.ARCHIVED.value:
                live_count = await self.repositories.campaign_live_state_count(campaign_id)
                if live_count:
                    await query.message.answer(
                        f"This past campaign still tracks {live_count} live post{'s' if live_count != 1 else ''}. "
                        "Delete the retained posts before removing its history.",
                        reply_markup=_markup(
                            [
                                [InlineKeyboardButton(text="Delete retained posts", callback_data=f"c:{campaign_id}:cleanup")],
                                *_navigation(f"c:{campaign_id}:open"),
                            ]
                        ),
                    )
                else:
                    await query.message.answer(
                        "Permanently delete this past campaign? Its campaign definition, delivery report, cycle history, failures, and join statistics "
                        "will be removed. This cannot be undone.",
                        reply_markup=_markup(
                            [
                                [
                                    InlineKeyboardButton(text="Delete campaign", callback_data=f"confirm:{campaign_id}:deletearchive"),
                                    InlineKeyboardButton(text="Keep history", callback_data=f"c:{campaign_id}:open"),
                                ]
                            ]
                        ),
                    )
            else:
                raise ValueError("end a live campaign before deleting it")
        elif action == "progress":
            await self._show_campaign(query.message, campaign)
        elif action == "failures":
            # Keep the drill-down safely below Telegram's message-size limit;
            # the newest failures are the most actionable ones.
            failures = await self.repositories.failed_deliveries(campaign_id, limit=8)
            if not failures:
                await query.message.answer(
                    "No failed, unknown, or cleanup-failed deliveries in this campaign.",
                    reply_markup=campaign_keyboard(campaign_id, campaign["status"], variant_count=len(campaign.get("variants", []))),
                )
                return
            lines = [f"Campaign failures ({len(failures)} shown)", f"Recovery campaign ID: {campaign_id}", ""]
            for item in failures:
                channel = await self.repositories.get_channel(item["channel_id"])
                title = channel.get("title") if channel else str(item["channel_id"])
                next_step = (
                    _cleanup_guidance(item.get("error_category"))
                    if item["status"] == "CLEANUP_FAILED"
                    else "Review the error, then use the matching campaign control."
                )
                lines.append(
                    f"- {title} ({item['channel_id']})\n"
                    f"  {item['status'].replace('_', ' ').title()} • {item.get('error_category', 'no category')}\n"
                    f"  Attempts: {item.get('attempts', 0)} • Last: {self._date(item.get('updated_at'), campaign.get('owner_timezone', 'UTC'))}\n"
                    f"  {item.get('error_summary', 'No additional Telegram error summary was stored.')}\n"
                    f"  Next step: {next_step}"
                )
            await query.message.answer(
                "\n".join(lines),
                reply_markup=campaign_keyboard(campaign_id, campaign["status"], variant_count=len(campaign.get("variants", []))),
            )
        elif action == "retry":
            count = await self.repositories.retry_failed_deliveries(campaign_id)
            noun = "delivery" if count == 1 else "deliveries"
            notice = await query.message.answer(
                f"Queued {count} failed {noun} across this campaign for one owner-requested retry. "
                "Unknown send results were not retried."
            )
            await self._show_campaign(notice, await self.repositories.get_campaign(campaign_id))
        elif action == "retrycleanup":
            if campaign["status"] != CampaignStatus.ENDING.value:
                raise ValueError("cleanup retries are only available while a campaign is ending")
            count = await self.repositories.retry_failed_cleanup_deliveries(campaign_id)
            noun = "cleanup" if count == 1 else "cleanups"
            notice = await query.message.answer(
                f"Queued {count} failed {noun} for another deletion attempt. "
                "Cancelled or interrupted cleanup work is repaired automatically."
            )
            await self._show_campaign(notice, await self.repositories.get_campaign(campaign_id))
        else:
            raise ValueError("that campaign control is no longer valid")

    async def _set_mode(self, query: CallbackQuery, campaign_id: str, mode: str) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        if mode != CampaignMode.STANDARD.value and len(campaign.get("variants", [])) < 2:
            raise ValueError("Rotate and Mix + Rotate need at least two variants.")
        await self.repositories.update_campaign(
            campaign_id,
            {
                "mode": CampaignMode(mode).value,
                "mode_explicitly_selected": True,
                "rotation_adjustment_notes": [],
                "updated_at": datetime.now(UTC),
            },
        )
        campaign, notes = await self.campaigns.normalize_draft_rotation_schedule(campaign_id)
        notice = query.message
        if notes:
            notice = await query.message.answer(
                "Rotation timing was auto-fitted: " + "; ".join(notes) + "."
            )
        await self._show_campaign(notice, campaign)

    async def _set_retention(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] in {CampaignStatus.ENDING.value, CampaignStatus.ARCHIVED.value}:
            raise ValueError("end behavior can only be changed before a campaign is ending or archived")
        if action not in {"delete", "replace", "keep"}:
            raise ValueError("invalid end behavior")
        delete_on_end = action == "delete"
        delete_on_next_campaign = action == "replace"
        if delete_on_end and not self._campaign_supports_final_cleanup(campaign):
            raise ValueError("this repost plan has a gap over 47 hours, so choose Keep final post or add a nearer final repost")
        await self.repositories.update_campaign(
            campaign_id,
            {
                "delete_on_end": delete_on_end,
                "delete_on_next_campaign": delete_on_next_campaign,
                "updated_at": datetime.now(UTC),
            },
        )
        campaign["delete_on_end"] = delete_on_end
        campaign["delete_on_next_campaign"] = delete_on_next_campaign
        await self._show_campaign(query.message, campaign)

    async def _set_button_layout(self, query: CallbackQuery, campaign_id: str, index: int, layout: str) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        variants[index].button_layout = layout
        await self.repositories.update_campaign(
            campaign_id,
            {
                "variants": [item.model_dump(mode="json") for item in variants],
                "preview_sent": False,
                "previewed_variant_ids": [],
                "updated_at": datetime.now(UTC),
            },
        )
        campaign["variants"] = [item.model_dump(mode="json") for item in variants]
        await self._show_button_editor(query.message, campaign, index)

    async def _begin_button_style_edit(self, query: CallbackQuery, campaign_id: str, index: int, button_id: str) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        if index < 0 or index >= len(variants):
            raise ValueError("CTA variant no longer exists")
        button = next((item for item in variants[index].buttons if item.id == button_id), None)
        if not button:
            raise ValueError("CTA button no longer exists")
        await self.repositories.set_owner_session(
            query.from_user.id,
            {
                "action": "choose_existing_button_style",
                "campaign_id": campaign_id,
                "variant_index": index,
                "button_id": button_id,
            },
        )
        await query.message.answer(
            f"Choose the native Telegram color for “{button.text}”.",
            reply_markup=cta_style_keyboard(f"edit:{campaign_id}:{index}"),
        )

    async def _apply_button_style(self, query: CallbackQuery, style: str) -> None:
        if style not in _CTA_STYLES:
            raise ValueError("invalid CTA color")
        session = await self.repositories.owner_session(query.from_user.id)
        if not session:
            raise ValueError("that color chooser expired; reopen the CTA editor")
        action = session.get("action")
        campaign_id = str(session.get("campaign_id") or "")
        if not campaign_id:
            raise ValueError("that color chooser is missing its campaign")
        if action == "choose_new_button_style":
            await self._save_button(query.message, {**session, "style": style})
            return
        if action != "choose_existing_button_style":
            raise ValueError("that color chooser is no longer active")
        index = int(session["variant_index"])
        campaign = await self.campaigns.editable_campaign(campaign_id)
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        if index < 0 or index >= len(variants):
            raise ValueError("CTA variant no longer exists")
        button = next((item for item in variants[index].buttons if item.id == session.get("button_id")), None)
        if not button:
            raise ValueError("CTA button no longer exists")
        button.style = style
        stored = [item.model_dump(mode="json") for item in variants]
        await self.repositories.update_campaign(
            campaign_id,
            {
                "variants": stored,
                "preview_sent": False,
                "previewed_variant_ids": [],
                "updated_at": datetime.now(UTC),
            },
        )
        await self.repositories.clear_owner_session(query.from_user.id)
        campaign["variants"] = stored
        await query.message.answer(f"CTA color saved: {_cta_style_label(style)}.")
        await self._show_button_editor(query.message, campaign, index)

    async def _remove_button(self, query: CallbackQuery, campaign_id: str, index: int, button_id: str) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        before = len(variants[index].buttons)
        variants[index].buttons = [button for button in variants[index].buttons if button.id != button_id]
        if len(variants[index].buttons) == before:
            raise ValueError("button no longer exists")
        await self.repositories.update_campaign(
            campaign_id,
            {
                "variants": [item.model_dump(mode="json") for item in variants],
                "preview_sent": False,
                "previewed_variant_ids": [],
                "updated_at": datetime.now(UTC),
            },
        )
        campaign["variants"] = [item.model_dump(mode="json") for item in variants]
        await self._show_button_editor(query.message, campaign, index)

    async def _preview_variant(self, query: CallbackQuery, campaign_id: str, index: int) -> None:
        notice = await self._send_preview(query.message, query.from_user.id, campaign_id, index)
        campaign = await self.repositories.get_campaign(campaign_id)
        if campaign:
            await self._show_button_editor(notice, campaign, index)

    async def _show_variants(self, message: Message, campaign: Document) -> None:
        variants = campaign.get("variants", [])
        campaign_id = campaign["campaign_id"]
        status = campaign["status"]
        if not variants:
            await message.answer(
                "No variants yet. Add Variant 1 to begin.",
                reply_markup=content_type_keyboard(campaign_id),
            )
            return
        rows: list[list[InlineKeyboardButton]] = []
        for index, item in enumerate(variants):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Variant {index + 1} • {item['kind'].replace('_', ' ').title()}",
                        callback_data=f"var:{campaign_id}:{index}:preview",
                    ),
                    InlineKeyboardButton(text="Share manually", callback_data=f"var:{campaign_id}:{index}:share"),
                ]
            )
            if status == CampaignStatus.DRAFT.value:
                rows.append(
                    [
                        InlineKeyboardButton(text="Replace", callback_data=f"var:{campaign_id}:{index}:replace"),
                        InlineKeyboardButton(text="CTA", callback_data=f"var:{campaign_id}:{index}:buttons"),
                        InlineKeyboardButton(text="Delete", callback_data=f"var:{campaign_id}:{index}:delete"),
                    ]
                )
            elif status in {CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text="Replace in future cycles",
                            callback_data=f"var:{campaign_id}:{index}:replace",
                        )
                    ]
                )
        if status == CampaignStatus.DRAFT.value:
            rows.append([InlineKeyboardButton(text="+ Add variant", callback_data=f"c:{campaign_id}:add")])
        elif status == CampaignStatus.SCHEDULED.value:
            rows.append([InlineKeyboardButton(text="Edit before it starts", callback_data=f"c:{campaign_id}:todraft")])
        rows.extend(_navigation(f"c:{campaign_id}:open"))
        explanation = (
            "Add, preview, replace, or remove variants. Rotate and Mix + Rotate automatically assign them across cycles."
            if status == CampaignStatus.DRAFT.value
            else "A live replacement is versioned: the current queued cycle stays consistent, and the new post is used when future rotation selects it."
            if status in {CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}
            else "Saved variants for this campaign."
        )
        await self._render(
            message,
            f"{campaign['name']} • Variants ({len(variants)})\n"
            f"Mode: {campaign.get('mode', CampaignMode.STANDARD.value).replace('_', ' ').title()}\n\n"
            f"{explanation}",
            _markup(rows),
        )

    async def _show_variant_share(self, message: Message, owner_id: int, campaign: Document, index: int) -> None:
        variants = campaign.get("variants", [])
        if index < 0 or index >= len(variants):
            raise ValueError("that variant no longer exists")
        creative = deepcopy(variants[index])
        campaign_id = campaign["campaign_id"]
        if support_error := inline_support_error(creative):
            rows: list[list[InlineKeyboardButton]] = []
            manifest = button_layout_manifest(creative)
            if creative.get("buttons") and len(manifest) <= 256:
                rows.append(
                    [InlineKeyboardButton(text="Copy CTA layout", copy_text=CopyTextButton(text=manifest))]
                )
            elif creative.get("buttons"):
                rows.append(
                    [InlineKeyboardButton(text="Show CTA layout", callback_data=f"var:{campaign_id}:{index}:manifest")]
                )
            rows.extend(_navigation(f"c:{campaign_id}:variants"))
            await self._render(
                message,
                f"Variant {index + 1} cannot be inserted inline as one exact post.\n\n{support_error}",
                _markup(rows),
            )
            return

        bot_user = await self.sender.bot.get_me()
        if not bot_user.username:
            raise ValueError("this bot needs a public username before inline sharing can be used")
        if not bot_user.supports_inline_queries:
            rows: list[list[InlineKeyboardButton]] = []
            if creative.get("buttons"):
                manifest = button_layout_manifest(creative)
                rows.append(
                    [
                        InlineKeyboardButton(text="Copy CTA fallback", copy_text=CopyTextButton(text=manifest))
                        if len(manifest) <= 256
                        else InlineKeyboardButton(text="Show CTA fallback", callback_data=f"var:{campaign_id}:{index}:manifest")
                    ]
                )
            rows.extend(_navigation(f"c:{campaign_id}:variants"))
            await self._render(
                message,
                "Inline sharing needs a one-time BotFather setting.\n\n"
                "Open @BotFather, send /setinline, select this bot, and use a placeholder such as "
                "‘Enter an iHarvester share code’. Then return here and tap Share manually again.",
                _markup(rows),
            )
            return

        snapshot_hash = creative_snapshot_hash(creative)
        variant_id = str(creative["id"])
        revision_value = campaign.get("variant_current_revisions", {}).get(variant_id)
        variant_revision = int(revision_value) if revision_value is not None else None
        share = await self.repositories.get_or_create_variant_share(
            owner_id=owner_id,
            campaign_id=campaign_id,
            campaign_name=str(campaign.get("name") or "Campaign"),
            variant_id=variant_id,
            variant_index=index,
            variant_revision=variant_revision,
            snapshot_hash=snapshot_hash,
            creative=creative,
        )
        active_shares = await self.repositories.active_variant_shares(owner_id, campaign_id, variant_id)
        older_count = sum(item.get("snapshot_hash") != snapshot_hash for item in active_shares)
        code = share["share_code"]
        inline_command = f"@{bot_user.username} {code}"
        revision_label = f"revision {variant_revision}" if variant_revision is not None else f"snapshot {snapshot_hash[:8]}"
        rows = [
            [
                InlineKeyboardButton(
                    text="Use in another bot or chat",
                    switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
                        query=code,
                        allow_user_chats=True,
                        allow_bot_chats=True,
                        allow_group_chats=True,
                        allow_channel_chats=True,
                    ),
                )
            ],
            [InlineKeyboardButton(text="Copy inline command", copy_text=CopyTextButton(text=inline_command))],
        ]
        if creative.get("buttons"):
            manifest = button_layout_manifest(creative)
            if len(manifest) <= 256:
                rows.append([InlineKeyboardButton(text="Copy CTA fallback", copy_text=CopyTextButton(text=manifest))])
            else:
                rows.append(
                    [InlineKeyboardButton(text="Show CTA fallback", callback_data=f"share:{campaign_id}:{index}:buttons:{code}")]
                )
        rows.append([InlineKeyboardButton(text="Revoke this code", callback_data=f"share:{campaign_id}:{index}:revoke:{code}")])
        if older_count:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Revoke {older_count} older snapshot code{'s' if older_count != 1 else ''}",
                        callback_data=f"share:{campaign_id}:{index}:older:{code}",
                    )
                ]
            )
        if len(active_shares) > 1:
            rows.append([InlineKeyboardButton(text="Revoke all variant codes", callback_data=f"share:{campaign_id}:{index}:all:{code}")])
        rows.extend(_navigation(f"c:{campaign_id}:variants"))
        await self._render(
            message,
            f"Manual share • {campaign['name']} • Variant {index + 1}\n\n"
            f"Code: {code}\n"
            f"Frozen version: {revision_label}\n"
            f"Active codes for this variant: {len(active_shares)}\n\n"
            f"In the other broadcast bot's chat, type:\n{inline_command}\n\n"
            "Choose the result to insert this exact frozen post with its formatting and URL buttons. "
            "The receiving bot must preserve the keyboard when it republishes the post; the CTA fallback is available if it does not.",
            _markup(rows),
        )

    async def _send_button_manifest(self, message: Message, creative: Document, back_callback: str) -> None:
        manifest = button_layout_manifest(creative)
        chunks: list[str] = []
        current = ""
        for line in manifest.splitlines(keepends=True):
            if current and len(current) + len(line) > 3500:
                chunks.append(current.rstrip())
                current = ""
            current += line
        if current:
            chunks.append(current.rstrip())
        for chunk_index, chunk in enumerate(chunks):
            await message.answer(
                chunk,
                reply_markup=_markup(_navigation(back_callback)) if chunk_index == len(chunks) - 1 else None,
            )

    async def _share_action(
        self,
        query: CallbackQuery,
        campaign_id: str,
        index: int,
        action: str,
        share_code: str,
    ) -> None:
        code = normalize_share_code(share_code)
        share = await self.repositories.get_variant_share(code, query.from_user.id)
        if not share or share.get("campaign_id") != campaign_id:
            raise ValueError("that manual-share code is revoked or no longer available")
        if action == "buttons":
            await self._send_button_manifest(query.message, share["creative"], f"c:{campaign_id}:variants")
            return
        if action == "revoke":
            await self.repositories.revoke_variant_share(code, query.from_user.id)
            await self._render(
                query.message,
                f"Manual-share code {code} was revoked. It no longer returns an inline result.",
                _markup(_navigation(f"c:{campaign_id}:variants")),
            )
            return
        if action not in {"older", "all"}:
            raise ValueError("that manual-share action is no longer valid")
        revoked = await self.repositories.revoke_variant_shares(
            owner_id=query.from_user.id,
            campaign_id=campaign_id,
            variant_id=share["variant_id"],
            except_snapshot_hash=share["snapshot_hash"] if action == "older" else None,
        )
        if action == "older":
            campaign = await self.repositories.get_campaign(campaign_id)
            if campaign and 0 <= index < len(campaign.get("variants", [])):
                notice = await query.message.answer(f"Revoked {revoked} older snapshot code{'s' if revoked != 1 else ''}.")
                await self._show_variant_share(notice, query.from_user.id, campaign, index)
                return
        await self._render(
            query.message,
            f"Revoked {revoked} manual-share code{'s' if revoked != 1 else ''} for this variant.",
            _markup(_navigation(f"c:{campaign_id}:variants")),
        )

    async def _variant_action(self, query: CallbackQuery, campaign_id: str, index: int, action: str) -> None:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("campaign no longer exists")
        variants = campaign.get("variants", [])
        if index < 0 or index >= len(variants):
            raise ValueError("that variant no longer exists")
        status = campaign["status"]
        if action == "preview":
            notice = await self._send_preview(query.message, query.from_user.id, campaign_id, index)
            updated = await self.repositories.get_campaign(campaign_id)
            if not updated:
                raise ValueError("campaign no longer exists")
            await self._show_variants(notice, updated)
        elif action == "share":
            await self._show_variant_share(query.message, query.from_user.id, campaign, index)
        elif action == "manifest":
            await self._send_button_manifest(query.message, variants[index], f"c:{campaign_id}:variants")
        elif action == "buttons":
            if status != CampaignStatus.DRAFT.value:
                raise ValueError("CTA layout can only be changed in a draft; replace the live variant content or edit a copy.")
            await self._show_button_editor(query.message, campaign, index)
        elif action == "replace":
            if status not in {CampaignStatus.DRAFT.value, CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}:
                raise ValueError("this campaign is not editable in its current state")
            await self.repositories.set_owner_session(
                query.from_user.id,
                {
                    "action": "await_replace_creative",
                    "campaign_id": campaign_id,
                    "variant_index": index,
                    "campaign_status": status,
                },
            )
            await query.message.answer(
                f"Replace Variant {index + 1}\n\n"
                "Send or forward the replacement post. Formatting and media are preserved, and its existing CTA buttons stay attached."
                + (
                    " I will preview it and ask for confirmation. The current queued cycle keeps the old revision; the replacement starts next cycle."
                    if status in {CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}
                    else ""
                ),
                reply_markup=_markup(_navigation(f"c:{campaign_id}:variants")),
            )
        elif action == "apply":
            if status not in {CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}:
                raise ValueError("this live replacement is no longer applicable")
            session = await self.repositories.owner_session(query.from_user.id)
            if (
                not session
                or session.get("action") != "confirm_running_variant_replace"
                or session.get("campaign_id") != campaign_id
                or int(session.get("variant_index", -1)) != index
            ):
                raise ValueError("that replacement preview expired; choose Replace again")
            replacement = Creative.model_validate(session["replacement"])
            updated, applies_from_cycle, has_future = await self.campaigns.replace_running_variant(
                campaign_id,
                index,
                replacement,
                query.from_user.id,
            )
            await self.repositories.clear_owner_session(query.from_user.id)
            if has_future:
                text = (
                    f"Variant {index + 1} replaced safely. Future planning starts at cycle {applies_from_cycle + 1}; "
                    "the already queued cycle keeps its original revision, and each channel receives the new version when rotation next selects this variant."
                )
                if as_utc(updated["current_end_at_utc"]) > as_utc(campaign["current_end_at_utc"]):
                    text += " I extended the campaign window so the replacement still has a complete rotation pass across every frozen target."
            else:
                text = (
                    f"Variant {index + 1} was saved, but this run has no future repost cycle. "
                    "Its current live posts stay unchanged; use Run again after completion to use the replacement."
                )
            notice = await query.message.answer(text)
            await self._show_variants(notice, updated)
        elif action == "delete":
            if status != CampaignStatus.DRAFT.value:
                raise ValueError("running variants cannot be removed because that would change frozen cohorts")
            await query.message.answer(
                f"Delete Variant {index + 1}?",
                reply_markup=_markup(
                    [
                        [
                            InlineKeyboardButton(text="Delete variant", callback_data=f"var:{campaign_id}:{index}:remove"),
                            InlineKeyboardButton(text="Cancel", callback_data=f"c:{campaign_id}:variants"),
                        ]
                    ]
                ),
            )
        elif action == "remove":
            if status != CampaignStatus.DRAFT.value:
                raise ValueError("running variants cannot be removed because that would change frozen cohorts")
            variants.pop(index)
            update: Document = {
                "variants": variants,
                "preview_sent": False,
                "previewed_variant_ids": [],
                "rotation_adjustment_notes": [],
                "updated_at": datetime.now(UTC),
            }
            if len(variants) < 2:
                update["mode"] = CampaignMode.STANDARD.value
                update["mode_explicitly_selected"] = False
                update["rotation_adjustment_notes"] = []
            await self.repositories.update_campaign(campaign_id, update)
            campaign, notes = await self.campaigns.normalize_draft_rotation_schedule(campaign_id)
            notice_text = "Variant deleted. Rotation was reset to Standard if fewer than two remain."
            if notes:
                notice_text += " Timing was auto-fitted for the remaining variants."
            notice = await query.message.answer(notice_text)
            await self._show_campaign(notice, campaign)
        else:
            raise ValueError("unknown variant action")

    async def _send_preview(self, message: Message, owner_id: int, campaign_id: str, index: int) -> Message:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or index >= len(campaign.get("variants", [])):
            raise ValueError("add a variant first")
        await self.sender.send_variant(owner_id, campaign["variants"][index])
        if campaign.get("status") == CampaignStatus.DRAFT.value:
            previewed = set(campaign.get("previewed_variant_ids", []))
            previewed.add(campaign["variants"][index]["id"])
            current_ids = {variant["id"] for variant in campaign["variants"]}
            await self.repositories.update_campaign(
                campaign_id,
                {
                    "previewed_variant_ids": sorted(previewed & current_ids),
                    "preview_sent": current_ids <= previewed,
                    "updated_at": datetime.now(UTC),
                },
            )
        return await message.answer("That is a real Telegram preview using the saved content and CTA keyboard.")

    async def _preview_all(self, message: Message, owner_id: int, campaign_id: str) -> Message:
        campaign = await self.repositories.get_campaign(campaign_id)
        variants = campaign.get("variants", []) if campaign else []
        if not variants:
            raise ValueError("add a variant first")
        for variant in variants:
            await self.sender.send_variant(owner_id, variant)
        await self.repositories.update_campaign(
            campaign_id,
            {
                "previewed_variant_ids": [variant["id"] for variant in variants],
                "preview_sent": True,
                "updated_at": datetime.now(UTC),
            },
        )
        return await message.answer(
            f"Previewed all {len(variants)} variant{'s' if len(variants) != 1 else ''} with their saved formatting and CTA keyboards."
        )

    async def _show_launch_confirmation(self, message: Message, campaign_id: str) -> None:
        campaign, errors, source_count, protected_count, eligible_count = await self.campaigns.launch_summary(campaign_id)
        if errors:
            await self._render(
                message,
                "Launch checklist\n\n" + "\n".join(f"- {error}" for error in errors),
                campaign_keyboard(campaign_id, campaign["status"], variant_count=len(campaign.get("variants", []))),
            )
            return
        interval = campaign.get("repost_interval_seconds")
        offsets = campaign.get("repost_offsets_seconds")
        repost_text = (
            "single post"
            if not interval and not offsets
            else f"every {_period_label(interval // 60)}"
            if interval
            else "at " + ", ".join(_period_label(offset // 60) for offset in offsets)
        )
        cleanup_text = (
            "delete final post"
            if campaign.get("delete_on_end", True)
            else "keep until replaced by a future campaign"
            if campaign.get("delete_on_next_campaign", False)
            else "keep until manually deleted"
        )
        display_timezone = campaign.get("owner_timezone", "UTC")
        estimated_cycle_seconds = max(1, ceil(eligible_count / self.campaigns.send_rps))
        reused_invite_count = (
            sum(1 for destination in campaign.get("destinations", []) if destination.get("campaign_invite_link"))
            if campaign.get("derived_from_campaign_id")
            else 0
        )
        tracking_notice = (
            f"\nInvite attribution: {reused_invite_count} saved campaign link{'s are' if reused_invite_count != 1 else ' is'} reused; "
            "replace under Promoted links when you need a new campaign-unique link."
            if reused_invite_count
            else ""
        )
        rotation_notice = ""
        if campaign.get("mode") != CampaignMode.STANDARD.value:
            cycle_count = (
                1 + len(offsets)
                if offsets is not None
                else 1 + (max(1, int((as_utc(campaign["current_end_at_utc"]) - as_utc(campaign["start_at_utc"])).total_seconds())) - 1)
                // int(interval)
                if interval
                else 1
            )
            rotation_notice = (
                f"\nRotation coverage: {cycle_count} cycles for {len(campaign['variants'])} variants; "
                "every variant is scheduled to reach every target at least once."
            )
        adjustment_notice = (
            "\nTiming auto-fit: " + "; ".join(campaign.get("rotation_adjustment_notes", [])) + "."
            if campaign.get("rotation_adjustment_notes")
            else ""
        )
        await self._render(
            message,
            "Ready to launch\n\n"
            f"This campaign will target {eligible_count} source channels.\n"
            f"{protected_count} promoted destination channels are excluded automatically.\n"
            f"Start: {self._date(campaign['start_at_utc'], display_timezone)}\n"
            f"End: {self._date(campaign['current_end_at_utc'], display_timezone)}\n"
            f"Repost: {repost_text}\n"
            f"End behavior: {cleanup_text}\n"
            f"Mode: {campaign['mode']} ({len(campaign['variants'])} variant{'s' if len(campaign['variants']) != 1 else ''})\n"
            f"Active sources before destination protection: {source_count}\n"
            f"Estimated first cycle: {self._duration_label(estimated_cycle_seconds)} at the configured send rate"
            f"{rotation_notice}{adjustment_notice}{tracking_notice}",
            _markup(
                [
                    [
                        InlineKeyboardButton(
                            text=(
                                f"Schedule for {self._date(campaign['start_at_utc'], display_timezone)}"
                                if as_utc(campaign["start_at_utc"]) > datetime.now(UTC)
                                else "Launch now"
                            ),
                            callback_data=f"confirm:{campaign_id}:launch",
                        ),
                        InlineKeyboardButton(text="Cancel", callback_data=f"c:{campaign_id}:open"),
                    ],
                ]
            ),
        )

    async def _show_destinations(self, message: Message, campaign: Document) -> None:
        destinations = campaign.get("destinations", [])
        lines = ["Promoted destinations", ""]
        if destinations:
            for index, destination in enumerate(destinations, start=1):
                identity = destination.get("raw_url") or destination.get("username") or destination.get("telegram_chat_id")
                protected = "protected" if destination.get("telegram_chat_id") else "link only"
                lines.append(f"{index}. {destination['display_name']} - {identity} ({protected})")
        else:
            lines.append("None yet. Add each promoted channel/link here.")
        cid = campaign["campaign_id"]
        controls: list[list[InlineKeyboardButton]] = []
        for index, destination in enumerate(destinations):
            controls.append([InlineKeyboardButton(text=f"Remove {destination['display_name']}", callback_data=f"dest:{cid}:remove-{index}")])
        controls.extend(
            [
                [InlineKeyboardButton(text="+ Add destination", callback_data=f"dest:{cid}:add")],
                [InlineKeyboardButton(text="Back", callback_data=f"c:{cid}:open")],
            ]
        )
        await self._render(message, "\n".join(lines), _markup(controls))

    async def _destination_action(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        await self.campaigns.editable_campaign(campaign_id)
        if action == "add":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_destination_name", "campaign_id": campaign_id})
            await query.message.answer(
                "Send the destination's display name.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "skip":
            session = await self.repositories.owner_session(query.from_user.id)
            if not session or session.get("action") != "await_destination_id" or session.get("campaign_id") != campaign_id:
                raise ValueError("that destination entry expired")
            await self._save_destination(campaign_id, session["name"], session["url"], None)
            await self.repositories.clear_owner_session(query.from_user.id)
            campaign = await self.repositories.get_campaign(campaign_id)
            notice = await query.message.answer("Destination saved. A link-only destination cannot be automatically excluded until its channel is registered.")
            await self._show_destinations(notice, campaign)
        elif action.startswith("remove-"):
            campaign = await self.campaigns.editable_campaign(campaign_id)
            destinations = list(campaign.get("destinations", []))
            index = int(action.removeprefix("remove-"))
            if index < 0 or index >= len(destinations):
                raise ValueError("that destination no longer exists")
            destinations.pop(index)
            await self.repositories.update_campaign(campaign_id, {"destinations": destinations, "updated_at": datetime.now(UTC)})
            campaign["destinations"] = destinations
            await self._show_destinations(query.message, campaign)
        else:
            raise ValueError("that destination control is no longer valid")

    async def _target_action(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        await self.campaigns.editable_campaign(campaign_id)
        if action == "all":
            await self.repositories.update_campaign(campaign_id, {"target_selector": {}, "updated_at": datetime.now(UTC)})
            await self._show_campaign(query.message, await self.repositories.get_campaign(campaign_id))
        else:
            prompts = {
                "tags": "Send comma-separated tags to include, for example: movies, kenya.",
                "members": "Send an audience range such as 1000-50000, a minimum such as 1000+, or just 1000 for a minimum.",
                "include": "Send comma-separated numeric channel IDs to include.",
                "exclude": "Send comma-separated numeric channel IDs to exclude.",
            }
            await self.repositories.set_owner_session(query.from_user.id, {"action": f"await_target_{action}", "campaign_id": campaign_id})
            await query.message.answer(prompts[action], reply_markup=_markup(_navigation(f"c:{campaign_id}:open")))

    async def _schedule_interval(self, query: CallbackQuery, campaign_id: str, value: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        if not session or session.get("action") != "await_schedule_interval" or session.get("campaign_id") != campaign_id:
            raise ValueError("schedule entry expired; start again")
        if value == "custom":
            await self.repositories.set_owner_session(query.from_user.id, {**session, "action": "await_schedule_interval_custom"})
            await query.message.answer(
                "Send the repost interval, for example 5m, 2h, or 1d. It must be shorter than the campaign; a final partial gap simply ends at campaign end.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
            return
        interval_minutes = int(value)
        duration_minutes = self._duration_minutes(session["start"], session["end"])
        self._validate_repost_interval(duration_minutes, interval_minutes)
        await self._save_schedule(
            campaign_id,
            session["start"],
            session["end"],
            interval_minutes,
            owner_timezone=session.get("timezone"),
        )
        await self.repositories.clear_owner_session(query.from_user.id)
        notice = await query.message.answer("Timing saved. Review and confirm the scheduled launch below.")
        await self._show_launch_confirmation(notice, campaign_id)

    async def _specific_repost_times(self, query: CallbackQuery, campaign_id: str, flow: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        expected_action = {"qmin": "await_quick_interval", "sint": "await_schedule_interval"}.get(flow)
        if not expected_action or not session or session.get("action") != expected_action or session.get("campaign_id") != campaign_id:
            raise ValueError("repost-time entry expired; choose Send campaign or Plan for later again")
        duration_minutes = int(session["duration_minutes"]) if flow == "qmin" else self._duration_minutes(session["start"], session["end"])
        action = "await_quick_repost_times" if flow == "qmin" else "await_schedule_repost_times"
        await self.repositories.set_owner_session(query.from_user.id, {**session, "action": action})
        await query.message.answer(
            "Enter 1-20 exact elapsed repost times after launch, separated by commas. For example 1d, 4d, 6d means "
            "an initial post now, then reposts at day 1, day 4, and day 6. Times can be uneven. If a final time runs "
            f"past the {_period_label(duration_minutes)} campaign, it is moved just before the end.",
            reply_markup=_markup(_navigation(f"c:{campaign_id}:send" if flow == "qmin" else f"c:{campaign_id}:open")),
        )

    async def _custom_repost_gaps(self, query: CallbackQuery, campaign_id: str, flow: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        expected_action = {"qmin": "await_quick_interval", "sint": "await_schedule_interval"}.get(flow)
        if not expected_action or not session or session.get("action") != expected_action or session.get("campaign_id") != campaign_id:
            raise ValueError("repost-gap entry expired; choose Send campaign or Plan for later again")
        duration_minutes = int(session["duration_minutes"]) if flow == "qmin" else self._duration_minutes(session["start"], session["end"])
        action = "await_quick_repost_gaps" if flow == "qmin" else "await_schedule_repost_gaps"
        await self.repositories.set_owner_session(query.from_user.id, {**session, "action": action})
        await query.message.answer(
            "Enter 1-20 custom gaps between posts, separated by commas. For example 1d, 3d, 2d means "
            "post now, then repost after 1 day, then 3 days later, then 2 days later. "
            f"If the final gap would run past the {_period_label(duration_minutes)} campaign, it is shortened to land just before the end.",
            reply_markup=_markup(_navigation(f"c:{campaign_id}:send" if flow == "qmin" else f"c:{campaign_id}:open")),
        )

    async def _quick_duration(self, query: CallbackQuery, campaign_id: str, minutes: int) -> None:
        await self._begin_quick_interval(query.from_user.id, query.message, campaign_id, minutes)

    async def _custom_duration(self, query: CallbackQuery, campaign_id: str) -> None:
        await self.campaigns.editable_campaign(campaign_id)
        await self.repositories.set_owner_session(query.from_user.id, {"action": "await_quick_duration", "campaign_id": campaign_id})
        await query.message.answer(
            "Send the campaign duration, for example 45m, 2h, 3d, or 1mo. One month is 30 days.",
            reply_markup=_markup(_navigation(f"c:{campaign_id}:send")),
        )

    async def _begin_quick_interval(self, owner_id: int, message: Message, campaign_id: str, minutes: int) -> None:
        await self.campaigns.editable_campaign(campaign_id)
        if minutes < 1:
            raise ValueError("campaign duration must be at least 1 minute")
        default_hours = float(await self.repositories.get_setting("quick_interval_hours", 6))
        await self.repositories.set_owner_session(
            owner_id,
            {"action": "await_quick_interval", "campaign_id": campaign_id, "duration_minutes": minutes},
        )
        await message.answer(
            f"Campaign duration: {_period_label(minutes)}. Choose a repost interval. "
            "The offered presets line up neatly; Custom interval can use any shorter period.",
            reply_markup=quick_interval_keyboard(campaign_id, minutes, default_hours),
        )

    async def _quick_interval(self, query: CallbackQuery, campaign_id: str, value: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        if not session or session.get("action") != "await_quick_interval" or session.get("campaign_id") != campaign_id:
            raise ValueError("send setup expired; open the campaign and choose Send campaign again")
        if value == "custom":
            await self.repositories.set_owner_session(query.from_user.id, {**session, "action": "await_quick_interval_custom"})
            await query.message.answer(
                "Send the repost interval, for example 5m, 2h, or 1d. It must be shorter than the campaign; the campaign ends after its final partial gap.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:send")),
            )
            return
        await self._complete_quick_send(query.from_user.id, query.message, campaign_id, int(session["duration_minutes"]), int(value))

    async def _complete_quick_send(
        self,
        owner_id: int,
        message: Message,
        campaign_id: str,
        duration_minutes: int,
        interval_minutes: int,
        repost_offsets_minutes: list[int] | None = None,
        repost_plan_adjusted: bool = False,
    ) -> None:
        if repost_offsets_minutes is None:
            self._validate_repost_interval(duration_minutes, interval_minutes)
        start = datetime.now(UTC)
        end = start + timedelta(minutes=duration_minutes)
        await self._save_schedule(campaign_id, start, end, interval_minutes, repost_offsets_minutes)
        await self.repositories.clear_owner_session(owner_id)
        if repost_plan_adjusted:
            await message.answer("The requested repost plan ran past campaign end, so its final repost was moved just before the end of the campaign.")
        try:
            notice = await self._preview_all(message, owner_id, campaign_id)
        except Exception:
            logger.exception("Quick-send preview failed", extra={"campaign_id": campaign_id})
            campaign = await self.repositories.get_campaign(campaign_id)
            await message.answer(
                "Timing was saved, but Telegram could not complete the owner preview. Nothing was launched. "
                "Open the draft and use Preview all to retry.",
                reply_markup=campaign_keyboard(campaign_id, CampaignStatus.DRAFT.value, variant_count=len(campaign.get("variants", [])) if campaign else 0),
            )
            return
        await self._show_launch_confirmation(notice, campaign_id)

    @staticmethod
    def _duration_minutes(start: datetime, end: datetime) -> int:
        minutes = int((as_utc(end) - as_utc(start)).total_seconds() // 60)
        if minutes < 1:
            raise ValueError("campaign duration must be at least 1 minute")
        return minutes

    @staticmethod
    def _validate_repost_interval(duration_minutes: int, interval_minutes: int) -> None:
        if interval_minutes == 0:
            if duration_minutes > _SAFE_REPOST_MAX_MINUTES:
                raise ValueError("campaigns over 47 hours need a repost interval for reliable cleanup")
            return
        if interval_minutes < 1 or interval_minutes >= duration_minutes:
            raise ValueError("repost interval must be at least 1 minute and shorter than the campaign")
        if duration_minutes > _SAFE_REPOST_MAX_MINUTES and interval_minutes > _SAFE_REPOST_MAX_MINUTES:
            raise ValueError("campaigns over 47 hours need a repost interval of 47 hours or less")

    @staticmethod
    def _specific_times_support_final_cleanup(duration_minutes: int, offsets_minutes: list[int]) -> bool:
        points = [0, *offsets_minutes, duration_minutes]
        return all(right - left <= _SAFE_REPOST_MAX_MINUTES for left, right in zip(points, points[1:], strict=False))

    @staticmethod
    def _campaign_supports_final_cleanup(campaign: Document) -> bool:
        """Whether the current schedule can still safely delete its final post."""
        if not campaign.get("start_at_utc") or not campaign.get("current_end_at_utc"):
            return True
        duration_minutes = OwnerHandlers._duration_minutes(campaign["start_at_utc"], campaign["current_end_at_utc"])
        offsets = campaign.get("repost_offsets_seconds")
        if offsets is not None:
            return OwnerHandlers._specific_times_support_final_cleanup(duration_minutes, [int(value // 60) for value in offsets])
        interval = campaign.get("repost_interval_seconds")
        return duration_minutes <= _SAFE_REPOST_MAX_MINUTES or bool(interval and interval <= _SAFE_REPOST_MAX_MINUTES * 60)

    async def _settings_action(self, query: CallbackQuery, action: str, value: str) -> None:
        if action == "timezone" and value == "custom":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_setting_timezone"})
            await query.message.answer(
                "Send an IANA timezone, for example Africa/Lagos, Europe/London, or America/New_York.",
                reply_markup=_markup(_navigation("home:settings")),
            )
            return
        if action == "timezone" and value in {"UTC", "Africa/Nairobi"}:
            await self.repositories.set_setting("owner_timezone", value)
        elif action == "interval" and value == "custom":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_setting_interval"})
            await query.message.answer(
                "Send a default repost period such as 45m, 6h, or 2d.",
                reply_markup=_markup(_navigation("home:settings")),
            )
            return
        elif action == "interval" and value in {"0", "6", "24"}:
            await self.repositories.set_setting("quick_interval_hours", float(value))
        elif action == "backup" and value in {"on", "off"}:
            await self.repositories.set_setting("auto_backup_enabled", value == "on")
        elif action == "backup_channels" and value == "custom":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_setting_backup_channels"})
            await query.message.answer(
                "Send how many newly discovered channels should trigger a backup, for example 100.",
                reply_markup=_markup(_navigation("home:settings")),
            )
            return
        elif action == "backup_interval" and value == "custom":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_setting_backup_interval"})
            await query.message.answer(
                "Send the maximum time between backups, for example 7d or 12h.",
                reply_markup=_markup(_navigation("home:settings")),
            )
            return
        else:
            raise ValueError("invalid setting")
        await self._show_settings(query.message)

    async def _network_action(self, query: CallbackQuery, bot: Bot, data: str) -> None:
        parts = data.split(":")
        if parts[1] in {"home", "back"}:
            await self._show_network(query.message) if parts[1] == "home" else await self._show_home(query.message)
        elif parts[1] == "forward":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_channel_forward"})
            await query.message.answer(
                "Forward any post from the channel. I will register or refresh that channel and then show its details.",
                reply_markup=_markup(_navigation("net:home")),
            )
        elif parts[1] == "refresh_attention":
            result = await refresh_attention_channels(bot, self.repositories)
            if not result["checked"]:
                notice = "Nothing needed refreshing."
            else:
                notice = (
                    f"Bulk refresh complete: {result['checked']} checked\n"
                    f"Restored to active: {result['active']}\n"
                    f"Still needs attention: {result['needs_attention']}\n"
                    f"Unavailable or missing permissions: {result['unavailable']}"
                )
                if result["other"]:
                    notice += f"\nOther outcomes: {result['other']}"
            await self._show_network(query.message, notice=notice)
        elif parts[1] == "list" and len(parts) == 4:
            await self._show_network_list(query.message, parts[2], int(parts[3]))
        elif parts[1] == "top" and len(parts) == 3:
            await self._show_top_channels(query.message, int(parts[2]))
        else:
            raise ValueError("that network control is no longer valid")

    async def _channel_action(self, query: CallbackQuery, bot: Bot, chat_id: int, action: str) -> None:
        if action in {"refresh", "Refresh"}:
            success = await refresh_channel(bot, self.repositories, chat_id)
            notice = await query.message.answer("Channel refreshed." if success else "Channel needs attention; check bot admin/posting rights.")
            await self._show_channel(notice, chat_id)
        elif action == "tag":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_channel_tag", "channel_id": chat_id})
            await query.message.answer("Send one or more comma-separated tags.", reply_markup=_markup(_navigation("net:home")))
        elif action == "disable":
            await self.repositories.set_channel_manual_enabled(chat_id, False)
            notice = await query.message.answer("Source channel paused. It will not be selected by new campaigns.")
            await self._show_channel(notice, chat_id)
        elif action == "enable":
            success = await refresh_channel(bot, self.repositories, chat_id)
            notice = await query.message.answer(
                "Source channel enabled and access verified."
                if success
                else "The channel remains unavailable. Restore the bot's admin/post permission, then refresh it."
            )
            await self._show_channel(notice, chat_id)
        elif action == "view":
            await self._show_channel(query.message, chat_id)
        else:
            raise ValueError("that channel control is no longer valid")

    async def _confirm(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        if action == "launch":
            activated = await self.campaigns.activate(campaign_id)
            notice = await query.message.answer(
                f"{activated['name']} is now {activated['status']}. It targets {len(activated['target_snapshot'])} source channels. "
                "The first deliveries are being queued now and normally appear within a few seconds."
            )
            await self._show_campaign(notice, activated)
        elif action == "end":
            changed = await self.campaigns.end_early(campaign_id)
            notice = await query.message.answer(
                "Campaign ending: future cycles are stopped and live posts will be cleaned up." if changed else "Campaign was already ending or archived."
            )
            campaign = await self.repositories.get_campaign(campaign_id)
            if campaign:
                await self._show_campaign(notice, campaign)
        elif action == "delete":
            deleted = await self.campaigns.delete_draft(campaign_id)
            notice = await query.message.answer("Draft deleted." if deleted else "That draft has already been removed.")
            await self._show_home(notice)
        elif action == "deletearchive":
            deleted = await self.repositories.delete_archived_campaign(campaign_id)
            await query.message.answer("Past campaign deleted." if deleted else "That past campaign has already been removed.")
            await self._home(query, "campaigns:0")
        else:
            raise ValueError("that confirmation is no longer valid")

    async def _restore_confirm(self, query: CallbackQuery, restore_id: str, action: str) -> None:
        if action == "cancel":
            await self.repositories.delete_pending_restore(restore_id, query.from_user.id)
            await query.message.answer("Restore cancelled.", reply_markup=home_keyboard())
            return
        if action != "confirm":
            raise ValueError("that restore control is no longer valid")
        pending = await self.repositories.get_pending_restore(restore_id, query.from_user.id)
        if not pending:
            raise ValueError("restore request expired")
        result = await restore_backup(self.repositories, pending["backup"])
        await self.repositories.delete_pending_restore(restore_id, query.from_user.id)
        await query.message.answer(f"Restore complete: {result}", reply_markup=home_keyboard())

    async def message(self, message: Message, bot: Bot) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        session = await self.repositories.owner_session(message.from_user.id)
        if not session:
            if not await register_forwarded_channel(message, bot, self.repositories):
                await message.answer(
                    "No setup step is waiting for this message. Use the controls below, or forward a channel post to register it.",
                    reply_markup=home_keyboard(),
                )
            return
        try:
            await self._handle_session_message(message, bot, session)
        except (ValueError, ValidationError) as error:
            await message.answer(
                f"That did not work: {error}. Correct the value and try again, or leave this step with the controls below.",
                reply_markup=self._session_recovery_keyboard(session),
            )

    @staticmethod
    def _session_recovery_keyboard(session: Document) -> InlineKeyboardMarkup:
        campaign_id = session.get("campaign_id")
        if campaign_id:
            return _markup(_navigation(f"c:{campaign_id}:open"))
        if session.get("action") in {"await_channel_forward", "await_channel_tag"}:
            return _markup(_navigation("net:home"))
        return _markup(_navigation())

    async def _handle_session_message(self, message: Message, bot: Bot, session: Document) -> None:
        action = session.get("action")
        owner_id = message.from_user.id
        if action == "await_restore_file":
            if not message.document:
                raise ValueError("attach the compressed backup file")
            file = await bot.get_file(message.document.file_id)
            stream = await bot.download_file(file.file_path)
            backup = parse_backup(stream.read())
            restore_id = opaque_id("restore")
            await self.repositories.save_pending_restore(restore_id, owner_id, backup)
            await self.repositories.clear_owner_session(owner_id)
            counts = {name: len(rows) for name, rows in backup["collections"].items()}
            await message.answer(
                f"Validated {backup['kind']} backup: {counts}. Restore via upsert?",
                reply_markup=_markup(
                    [
                        [
                            InlineKeyboardButton(text="Confirm restore", callback_data=f"restore:{restore_id}:confirm"),
                            InlineKeyboardButton(text="Cancel", callback_data=f"restore:{restore_id}:cancel"),
                        ]
                    ]
                ),
            )
            return
        if action == "await_campaign_name":
            name = (message.text or "").strip()
            if not name:
                raise ValueError("send a campaign name as text")
            campaign = await self.campaigns.create_draft(owner_id, name)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Draft created. Start by choosing the post type for Variant 1.", reply_markup=content_type_keyboard(campaign["campaign_id"]))
            return
        if action == "await_channel_forward":
            if not await register_forwarded_channel(message, bot, self.repositories):
                raise ValueError("forward a normal post from a channel where the bot is an administrator")
            await self.repositories.clear_owner_session(owner_id)
            return
        if action == "await_channel_tag":
            tags = sorted({value.strip().lower() for value in (message.text or "").split(",") if value.strip()})
            await self.repositories.set_channel_tags(session["channel_id"], tags)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(f"Tags saved: {', '.join(tags) or 'none'}")
            await self._show_channel(message, session["channel_id"])
            return
        if action == "await_setting_timezone":
            timezone = (message.text or "").strip()
            try:
                ZoneInfo(timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("use a valid IANA timezone such as Africa/Nairobi") from error
            await self.repositories.set_setting("owner_timezone", timezone)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(f"Timezone saved as {timezone}.")
            await self._show_settings(message)
            return
        if action == "await_setting_interval":
            minutes = parse_period_minutes(message.text or "", field="default repost interval")
            await self.repositories.set_setting("quick_interval_hours", minutes / 60)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(f"Default repost interval saved as {_period_label(minutes)}.")
            await self._show_settings(message)
            return
        if action == "await_setting_backup_channels":
            threshold = int((message.text or "").strip())
            if threshold < 1:
                raise ValueError("backup channel trigger must be at least 1")
            await self.repositories.set_setting("auto_backup_every_new_channels", threshold)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(f"A registry backup will be triggered every {threshold} new channels.")
            await self._show_settings(message)
            return
        if action == "await_setting_backup_interval":
            minutes = parse_period_minutes(message.text or "", field="backup interval")
            hours = max(1, (minutes + 59) // 60)
            await self.repositories.set_setting("auto_backup_interval_hours", hours)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(f"Automatic backup interval saved as {_period_label(hours * 60)}.")
            await self._show_settings(message)
            return
        campaign_id = session.get("campaign_id")
        if not campaign_id:
            await self.repositories.clear_owner_session(owner_id)
            return
        if action == "await_creative":
            await self._capture_creative(message, session)
        elif action == "await_campaign_rename":
            name = (message.text or "").strip()
            if not name:
                raise ValueError("send a campaign name as text")
            campaign = await self.campaigns.rename_draft(campaign_id, name)
            await self.repositories.clear_owner_session(owner_id)
            await self._show_campaign(message, campaign)
        elif action == "await_replace_creative":
            await self._replace_creative(message, session)
        elif action == "confirm_running_variant_replace":
            index = int(session["variant_index"])
            await message.answer(
                "Use the Confirm replacement button on the preview above, or cancel and choose Replace again.",
                reply_markup=_markup(
                    [
                        [InlineKeyboardButton(text="Confirm replacement", callback_data=f"var:{campaign_id}:{index}:apply")],
                        *_navigation(f"c:{campaign_id}:variants"),
                    ]
                ),
            )
        elif action == "await_album":
            await self._capture_album_item(message, session)
        elif action == "await_button_label":
            label = (message.text or "").strip()
            if not label:
                raise ValueError("send a button label")
            await self.repositories.set_owner_session(owner_id, {**session, "action": "await_button_url", "label": label})
            await message.answer(
                "Now send its direct destination URL, for example https://t.me/example.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "await_button_url":
            url = (message.text or "").strip()
            Button(id="validate", text=str(session.get("label") or ""), url=url)
            await self.repositories.set_owner_session(
                owner_id,
                {**session, "action": "choose_new_button_style", "url": url},
            )
            await message.answer(
                "Choose this CTA button's native Telegram color. Neutral is safest; blue is best for the main action.",
                reply_markup=cta_style_keyboard(f"edit:{campaign_id}:{session['variant_index']}"),
            )
        elif action == "await_destination_name":
            name = (message.text or "").strip()
            if not name:
                raise ValueError("send a destination name")
            await self.repositories.set_owner_session(owner_id, {"action": "await_destination_url", "campaign_id": campaign_id, "name": name})
            await message.answer(
                "Send its direct Telegram/channel URL.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "await_destination_url":
            raw_url = (message.text or "").strip()
            Destination(display_name=session["name"], raw_url=raw_url)
            await self.repositories.set_owner_session(
                owner_id, {"action": "await_destination_id", "campaign_id": campaign_id, "name": session["name"], "url": raw_url}
            )
            await message.answer(
                "If you know this channel's numeric ID, send it now so it is protected from this campaign. Otherwise tap below.",
                reply_markup=_markup(
                    [
                        [
                            InlineKeyboardButton(text="Save without channel ID", callback_data=f"dest:{campaign_id}:skip"),
                        ]
                    ]
                    + _navigation(f"c:{campaign_id}:open")
                ),
            )
        elif action == "await_destination_id":
            await self._save_destination(campaign_id, session["name"], session["url"], int((message.text or "").strip()))
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Destination saved and will be excluded from its own campaign.")
            await self._show_destinations(message, await self.repositories.get_campaign(campaign_id))
        elif action.startswith("await_target_"):
            await self._save_target(message, session)
        elif action == "await_campaign_extension":
            minutes = parse_period_minutes(message.text or "", field="campaign extension")
            await self.campaigns.extend(campaign_id, owner_id, minutes * 60)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(f"Campaign extended by {_period_label(minutes)}.")
            await self._show_campaign(message, await self.repositories.get_campaign(campaign_id))
        elif action == "await_schedule_start":
            timezone = session.get("timezone", "UTC")
            start = self._parse_owner_time(message.text or "", timezone)
            await self.repositories.set_owner_session(
                owner_id,
                {
                    "action": "await_schedule_end",
                    "campaign_id": campaign_id,
                    "start": start,
                    "timezone": timezone,
                },
            )
            await message.answer(
                f"When should it end? Send YYYY-MM-DD HH:MM in {timezone}.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "await_schedule_end":
            end = self._parse_owner_time(message.text or "", session.get("timezone", "UTC"))
            if end <= session["start"]:
                raise ValueError("end time must be after start time")
            await self.repositories.set_owner_session(
                owner_id,
                {
                    "action": "await_schedule_interval",
                    "campaign_id": campaign_id,
                    "start": session["start"],
                    "end": end,
                    "timezone": session.get("timezone", "UTC"),
                },
            )
            duration_minutes = self._duration_minutes(session["start"], end)
            await message.answer(
                "How often should the post be replaced? The offered presets line up neatly; Custom interval can use any shorter period.",
                reply_markup=schedule_interval_keyboard(campaign_id, duration_minutes),
            )
        elif action == "await_schedule_hours":
            # Complete a short-lived session created by a previous deployment.
            hours = int((message.text or "").strip())
            if hours < 0:
                raise ValueError("interval cannot be negative")
            await self._save_schedule(campaign_id, session["start"], session["end"], hours * 60, owner_timezone=session.get("timezone"))
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Timing saved. Review and confirm the scheduled launch below.")
            await self._show_launch_confirmation(message, campaign_id)
        elif action == "await_schedule_interval_custom":
            interval_minutes = parse_period_minutes(message.text or "", field="repost interval")
            duration_minutes = self._duration_minutes(session["start"], session["end"])
            self._validate_repost_interval(duration_minutes, interval_minutes)
            await self._save_schedule(campaign_id, session["start"], session["end"], interval_minutes, owner_timezone=session.get("timezone"))
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Timing saved. Review and confirm the scheduled launch below.")
            await self._show_launch_confirmation(message, campaign_id)
        elif action == "await_schedule_repost_times":
            duration_minutes = self._duration_minutes(session["start"], session["end"])
            offsets_minutes, adjusted = build_repost_offsets_minutes(message.text or "", duration_minutes=duration_minutes)
            final_cleanup = await self._save_schedule(
                campaign_id, session["start"], session["end"], 0, offsets_minutes, owner_timezone=session.get("timezone")
            )
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(self._specific_schedule_saved_text(offsets_minutes, final_cleanup, adjusted))
            await self._show_launch_confirmation(message, campaign_id)
        elif action == "await_schedule_repost_gaps":
            duration_minutes = self._duration_minutes(session["start"], session["end"])
            offsets_minutes, adjusted = build_repost_gaps_minutes(message.text or "", duration_minutes=duration_minutes)
            final_cleanup = await self._save_schedule(
                campaign_id, session["start"], session["end"], 0, offsets_minutes, owner_timezone=session.get("timezone")
            )
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(self._specific_schedule_saved_text(offsets_minutes, final_cleanup, adjusted))
            await self._show_launch_confirmation(message, campaign_id)
        elif action == "await_quick_duration":
            duration_minutes = parse_period_minutes(message.text or "", field="campaign duration")
            await self._begin_quick_interval(owner_id, message, campaign_id, duration_minutes)
        elif action == "await_quick_interval_custom":
            interval_minutes = parse_period_minutes(message.text or "", field="repost interval")
            await self._complete_quick_send(owner_id, message, campaign_id, int(session["duration_minutes"]), interval_minutes)
        elif action == "await_quick_repost_times":
            duration_minutes = int(session["duration_minutes"])
            offsets_minutes, adjusted = build_repost_offsets_minutes(message.text or "", duration_minutes=duration_minutes)
            await self._complete_quick_send(owner_id, message, campaign_id, duration_minutes, 0, offsets_minutes, adjusted)
        elif action == "await_quick_repost_gaps":
            duration_minutes = int(session["duration_minutes"])
            offsets_minutes, adjusted = build_repost_gaps_minutes(message.text or "", duration_minutes=duration_minutes)
            await self._complete_quick_send(owner_id, message, campaign_id, duration_minutes, 0, offsets_minutes, adjusted)
        elif action == "await_test_channel":
            campaign = await self.repositories.get_campaign(campaign_id)
            if not campaign or not campaign.get("variants"):
                raise ValueError("add a variant before testing")
            test_channel_ids = [int(value.strip()) for value in (message.text or "").split(",") if value.strip()]
            if not 1 <= len(test_channel_ids) <= 3 or len(set(test_channel_ids)) != len(test_channel_ids):
                raise ValueError("send 1-3 unique numeric test channel IDs")
            successful = failed = 0
            for test_channel_id in test_channel_ids:
                for variant in campaign["variants"]:
                    try:
                        await self.sender.send_variant(test_channel_id, variant)
                        successful += 1
                    except Exception:
                        failed += 1
                        logger.exception(
                            "Owner test send failed",
                            extra={"campaign_id": campaign_id, "test_channel_id": test_channel_id},
                        )
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(
                f"Test send finished: {successful} accepted, {failed} failed across "
                f"{len(test_channel_ids)} channel{'s' if len(test_channel_ids) != 1 else ''}."
            )
            await self._show_campaign(message, campaign)

    async def _capture_creative(self, message: Message, session: Document) -> None:
        kind = session["kind"]
        if kind == "TEXT" and not message.text:
            raise ValueError("send a text message")
        if kind.startswith("PHOTO") and not message.photo:
            raise ValueError("send a photo")
        if kind == "PHOTO_TEXT" and not message.caption:
            raise ValueError("this option needs a photo with a caption")
        if kind.startswith("VIDEO") and not message.video:
            raise ValueError("send a video")
        if kind == "VIDEO_TEXT" and not message.caption:
            raise ValueError("this option needs a video with a caption")
        creative = capture_creative(message)
        campaign = await self.campaigns.editable_campaign(session["campaign_id"])
        if len(campaign.get("variants", [])) >= _MAX_VARIANTS:
            raise ValueError(f"a campaign can contain at most {_MAX_VARIANTS} variants")
        variants = [*campaign.get("variants", []), creative.model_dump(mode="json")]
        add_update: Document = {
            "variants": variants,
            "preview_sent": False,
            "rotation_adjustment_notes": [],
            "updated_at": datetime.now(UTC),
        }
        auto_mix = len(variants) == 2 and not campaign.get("mode_explicitly_selected", False)
        if auto_mix:
            add_update["mode"] = CampaignMode.MIX_ROTATE.value
        await self.repositories.update_campaign(
            session["campaign_id"],
            add_update,
        )
        campaign, timing_notes = await self.campaigns.normalize_draft_rotation_schedule(session["campaign_id"])
        await self.repositories.clear_owner_session(message.from_user.id)
        preview_ok = True
        try:
            await self.sender.send_variant(message.from_user.id, creative.model_dump(mode="json"))
        except Exception:
            preview_ok = False
            logger.exception("Saved creative preview failed", extra={"campaign_id": session["campaign_id"]})
        previewed_ids = set(campaign.get("previewed_variant_ids", []))
        if preview_ok:
            previewed_ids.add(creative.id)
        current_variant_ids = {item["id"] for item in variants}
        await self.repositories.update_campaign(
            session["campaign_id"],
            {
                "previewed_variant_ids": sorted(previewed_ids & current_variant_ids),
                "preview_sent": current_variant_ids <= previewed_ids,
                "updated_at": datetime.now(UTC),
            },
        )
        saved_text = (
            f"Variant {len(variants)} saved. The post above is replayable. Add CTA buttons, add another variant, or continue setup."
            if preview_ok
            else f"Variant {len(variants)} saved, but Telegram could not render its owner preview. Use Preview all after checking the content."
        )
        if auto_mix:
            saved_text += " Mix + Rotate is now the default: channels are split into balanced cohorts and variants rotate across them."
        await message.answer(
            saved_text,
            reply_markup=_markup(
                [
                    [InlineKeyboardButton(text="+ Add CTA button", callback_data=f"btn:{session['campaign_id']}:{len(variants) - 1}:first")],
                    [
                        InlineKeyboardButton(text="Edit CTA layout", callback_data=f"edit:{session['campaign_id']}:{len(variants) - 1}"),
                        InlineKeyboardButton(text="+ Add another variant", callback_data=f"c:{session['campaign_id']}:add"),
                    ],
                    *(
                        [[InlineKeyboardButton(text="Review auto-fitted timing", callback_data=f"c:{session['campaign_id']}:open")]]
                        if timing_notes
                        else []
                    ),
                    [InlineKeyboardButton(text="Continue campaign setup", callback_data=f"c:{session['campaign_id']}:open")],
                ]
            ),
        )

    async def _replace_creative(self, message: Message, session: Document) -> None:
        campaign = await self.repositories.get_campaign(session["campaign_id"])
        if not campaign or campaign.get("status") not in {
            CampaignStatus.DRAFT.value,
            CampaignStatus.ACTIVE.value,
            CampaignStatus.PAUSED.value,
        }:
            raise ValueError("this campaign is no longer editable")
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        index = int(session["variant_index"])
        if index < 0 or index >= len(variants):
            raise ValueError("that variant no longer exists")
        replacement = capture_creative(message)
        replacement.id = variants[index].id
        replacement.buttons = variants[index].buttons
        replacement.button_layout = variants[index].button_layout
        if campaign["status"] in {CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}:
            try:
                await self.sender.send_variant(message.from_user.id, replacement.model_dump(mode="json"))
            except Exception:
                logger.exception("Live replacement variant preview failed", extra={"campaign_id": session["campaign_id"]})
                await message.answer(
                    "Telegram could not render that replacement preview, so nothing changed. Send the replacement again or go back.",
                    reply_markup=_markup(_navigation(f"c:{session['campaign_id']}:variants")),
                )
                return
            await self.repositories.set_owner_session(
                message.from_user.id,
                {
                    "action": "confirm_running_variant_replace",
                    "campaign_id": session["campaign_id"],
                    "variant_index": index,
                    "replacement": replacement.model_dump(mode="json"),
                },
            )
            await message.answer(
                f"Preview for replacement Variant {index + 1}\n\n"
                "Confirm to use it from the next repost cycle. The current queued cycle and already-live posts are not mixed or changed mid-cycle.",
                reply_markup=_markup(
                    [
                        [
                            InlineKeyboardButton(text="Confirm replacement", callback_data=f"var:{session['campaign_id']}:{index}:apply"),
                            InlineKeyboardButton(text="Cancel", callback_data=f"c:{session['campaign_id']}:variants"),
                        ],
                        *_navigation(f"c:{session['campaign_id']}:variants"),
                    ]
                ),
            )
            return
        variants[index] = replacement
        stored = [item.model_dump(mode="json") for item in variants]
        await self.repositories.update_campaign(
            session["campaign_id"],
            {
                "variants": stored,
                "preview_sent": False,
                "previewed_variant_ids": [],
                "updated_at": datetime.now(UTC),
            },
        )
        await self.repositories.clear_owner_session(message.from_user.id)
        try:
            await self._send_preview(message, message.from_user.id, session["campaign_id"], index)
            replacement_text = f"Variant {index + 1} replaced. Its existing CTA buttons and placement were preserved."
        except Exception:
            logger.exception("Replacement creative preview failed", extra={"campaign_id": session["campaign_id"]})
            replacement_text = (
                f"Variant {index + 1} replaced and its CTA layout was preserved, but Telegram could not render the preview. "
                "Use Preview all to retry."
            )
        await message.answer(replacement_text)
        await self._show_campaign(message, await self.repositories.get_campaign(session["campaign_id"]))

    async def _capture_album_item(self, message: Message, session: Document) -> None:
        captured = capture_creative(message)
        if captured.kind not in {"PHOTO", "VIDEO"}:
            raise ValueError("albums currently accept photos and videos only")
        media = [*session.get("media", []), captured.media[0]]
        if len(media) > 10:
            raise ValueError("an album can contain at most 10 items")
        await self.repositories.set_owner_session(
            message.from_user.id,
            {
                **session,
                "media": media,
                "caption": session.get("caption") or captured.caption,
                "caption_entities": session.get("caption_entities") or captured.caption_entities,
            },
        )
        await message.answer(
            f"Album item {len(media)} saved. Send another photo/video, or finish the album.",
            reply_markup=_markup(
                [
                    [
                        InlineKeyboardButton(text="Finish album", callback_data=f"album:{session['campaign_id']}:finish"),
                    ],
                    *_navigation(f"c:{session['campaign_id']}:open"),
                ]
            ),
        )

    async def _finish_album(self, query: CallbackQuery, campaign_id: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        if not session or session.get("action") != "await_album" or session.get("campaign_id") != campaign_id:
            raise ValueError("album session expired; start a new album")
        media = session.get("media", [])
        if len(media) < 2:
            raise ValueError("an album needs at least two items")
        campaign = await self.campaigns.editable_campaign(campaign_id)
        if len(campaign.get("variants", [])) >= _MAX_VARIANTS:
            raise ValueError(f"a campaign can contain at most {_MAX_VARIANTS} variants")
        creative = Creative(
            id=opaque_id("var"),
            kind="MEDIA_GROUP",
            media=media,
            caption=session.get("caption"),
            caption_entities=session.get("caption_entities", []),
        )
        variants = [*campaign.get("variants", []), creative.model_dump(mode="json")]
        add_update: Document = {
            "variants": variants,
            "preview_sent": False,
            "rotation_adjustment_notes": [],
            "updated_at": datetime.now(UTC),
        }
        auto_mix = len(variants) == 2 and not campaign.get("mode_explicitly_selected", False)
        if auto_mix:
            add_update["mode"] = CampaignMode.MIX_ROTATE.value
        await self.repositories.update_campaign(
            campaign_id,
            add_update,
        )
        _, timing_notes = await self.campaigns.normalize_draft_rotation_schedule(campaign_id)
        await self.repositories.clear_owner_session(query.from_user.id)
        preview_ok = True
        try:
            await self.sender.send_variant(query.from_user.id, creative.model_dump(mode="json"))
        except Exception:
            preview_ok = False
            logger.exception("Saved album preview failed", extra={"campaign_id": campaign_id})
        previewed_ids = set(campaign.get("previewed_variant_ids", []))
        if preview_ok:
            previewed_ids.add(creative.id)
        current_variant_ids = {item["id"] for item in variants}
        await self.repositories.update_campaign(
            campaign_id,
            {
                "previewed_variant_ids": sorted(previewed_ids & current_variant_ids),
                "preview_sent": current_variant_ids <= previewed_ids,
                "updated_at": datetime.now(UTC),
            },
        )
        saved_text = (
            f"Variant {len(variants)} (album) saved. Telegram renders its CTA as a compact message after the album when buttons are added."
            if preview_ok
            else f"Variant {len(variants)} (album) saved, but Telegram could not render its owner preview. Use Preview all to retry."
        )
        if auto_mix:
            saved_text += " Mix + Rotate is now the default: channels are split into balanced cohorts and variants rotate across them."
        await query.message.answer(
            saved_text,
            reply_markup=_markup(
                [
                    [InlineKeyboardButton(text="+ Add CTA button", callback_data=f"btn:{campaign_id}:{len(variants) - 1}:first")],
                    [InlineKeyboardButton(text="+ Add another variant", callback_data=f"c:{campaign_id}:add")],
                    *(
                        [[InlineKeyboardButton(text="Review auto-fitted timing", callback_data=f"c:{campaign_id}:open")]]
                        if timing_notes
                        else []
                    ),
                    [InlineKeyboardButton(text="Continue campaign setup", callback_data=f"c:{campaign_id}:open")],
                ]
            ),
        )

    async def _save_button(self, message: Message, session: Document) -> None:
        campaign = await self.campaigns.editable_campaign(session["campaign_id"])
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        index = int(session["variant_index"])
        creative = variants[index]
        if len(creative.buttons) >= _MAX_CTA_BUTTONS:
            raise ValueError(f"a variant can contain at most {_MAX_CTA_BUTTONS} CTA buttons")
        placement = session["placement"]
        if placement == "right" and creative.buttons:
            row = max(button.row for button in creative.buttons)
            position = max(button.position for button in creative.buttons if button.row == row) + 1
            creative.button_layout = "CUSTOM"
        elif placement == "below" and creative.buttons:
            row = max(button.row for button in creative.buttons) + 1
            position = 0
            creative.button_layout = "CUSTOM"
        else:
            row = position = 0
        creative.buttons.append(
            Button(
                id=opaque_id("btn"),
                text=session["label"],
                url=str(session.get("url") or message.text or "").strip(),
                style=str(session.get("style") or "default"),
                row=row,
                position=position,
            )
        )
        stored = [item.model_dump(mode="json") for item in variants]
        await self.repositories.update_campaign(
            session["campaign_id"],
            {
                "variants": stored,
                "preview_sent": False,
                "previewed_variant_ids": [],
                "updated_at": datetime.now(UTC),
            },
        )
        await self.repositories.clear_owner_session(message.from_user.id)
        campaign["variants"] = stored
        await message.answer("CTA button saved with its native Telegram color. Use the placement controls to add the next one beside it or on a new row.")
        await self._show_button_editor(message, campaign, index)

    async def _save_destination(self, campaign_id: str, name: str, url: str, chat_id: int | None) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        if len(campaign.get("destinations", [])) >= _MAX_DESTINATIONS:
            raise ValueError(f"a campaign can contain at most {_MAX_DESTINATIONS} promoted destinations")
        is_invite = bool(re.match(r"^https://t\.me/(?:\+|joinchat/)", url, flags=re.IGNORECASE))
        destination = Destination(
            display_name=name,
            raw_url=url,
            telegram_chat_id=chat_id,
            campaign_invite_link=url if is_invite else None,
            join_tracking_enabled=is_invite,
        )
        await self.repositories.update_campaign(
            campaign_id,
            {"destinations": [*campaign.get("destinations", []), destination.model_dump(mode="json")], "updated_at": datetime.now(UTC)},
        )

    async def _save_target(self, message: Message, session: Document) -> None:
        kind = session["action"].removeprefix("await_target_")
        raw = (message.text or "").strip()
        await self.campaigns.editable_campaign(session["campaign_id"])
        selector: Document = {}
        if kind == "tags":
            selector["tags_any"] = sorted({item.strip().lower() for item in raw.split(",") if item.strip()})
        elif kind == "members":
            audience_range = re.fullmatch(r"(\d+)\s*-\s*(\d+)", raw)
            audience_minimum = re.fullmatch(r"(\d+)\+?", raw)
            if audience_range:
                selector["minimum_members"] = int(audience_range.group(1))
                selector["maximum_members"] = int(audience_range.group(2))
                if selector["maximum_members"] < selector["minimum_members"]:
                    raise ValueError("maximum audience must be at least the minimum")
            elif audience_minimum:
                selector["minimum_members"] = int(audience_minimum.group(1))
            else:
                raise ValueError("use an audience range such as 1000-50000 or a minimum such as 1000+")
        else:
            selector[f"{kind}_ids"] = [int(item.strip()) for item in raw.split(",") if item.strip()]
        if not next(iter(selector.values()), None) and kind != "members":
            raise ValueError("send at least one tag or channel ID")
        await self.repositories.update_campaign(session["campaign_id"], {"target_selector": selector, "updated_at": datetime.now(UTC)})
        await self.repositories.clear_owner_session(message.from_user.id)
        await message.answer("Target selection saved. Destination channels will remain protected.")
        await self._show_campaign(message, await self.repositories.get_campaign(session["campaign_id"]))

    async def _save_schedule(
        self,
        campaign_id: str,
        start: datetime,
        end: datetime,
        interval_minutes: int,
        repost_offsets_minutes: list[int] | None = None,
        owner_timezone: str | None = None,
    ) -> bool:
        interval = interval_minutes * 60 or None
        start = as_utc(start)
        end = as_utc(end)
        duration_minutes = self._duration_minutes(start, end)
        final_cleanup_available = (
            self._specific_times_support_final_cleanup(duration_minutes, repost_offsets_minutes)
            if repost_offsets_minutes is not None
            else interval is None or interval <= _SAFE_REPOST_MAX_MINUTES * 60
        )
        campaign = await self.campaigns.editable_campaign(campaign_id)
        delete_on_end = bool(campaign.get("delete_on_end", True)) and final_cleanup_available
        delete_on_next_campaign = bool(campaign.get("delete_on_next_campaign", False)) or (
            bool(campaign.get("delete_on_end", True)) and not final_cleanup_available
        )
        timezone = owner_timezone or await self.repositories.get_setting("owner_timezone", "UTC")
        await self.repositories.update_campaign(
            campaign_id,
            {
                "start_at_utc": start,
                "original_end_at_utc": end,
                "current_end_at_utc": end,
                "repost_interval_seconds": interval,
                "repost_offsets_seconds": [minutes * 60 for minutes in repost_offsets_minutes] if repost_offsets_minutes is not None else None,
                "delete_on_repost": True,
                "delete_on_end": delete_on_end,
                "delete_on_next_campaign": delete_on_next_campaign,
                "owner_timezone": timezone,
                "rerun_ready": False,
                "rotation_adjustment_notes": [],
                "updated_at": datetime.now(UTC),
            },
        )
        normalized, _ = await self.campaigns.normalize_draft_rotation_schedule(campaign_id)
        final_cleanup_available = self._campaign_supports_final_cleanup(normalized)
        final_delete_on_end = bool(delete_on_end) and final_cleanup_available
        if final_delete_on_end != normalized.get("delete_on_end"):
            await self.repositories.update_campaign(
                campaign_id,
                {
                    "delete_on_end": final_delete_on_end,
                    "delete_on_next_campaign": not final_delete_on_end,
                    "updated_at": datetime.now(UTC),
                },
            )
        return final_delete_on_end

    @staticmethod
    def _specific_schedule_saved_text(offsets_minutes: list[int], final_cleanup: bool, adjusted: bool = False) -> str:
        plan = ", ".join(_period_label(value) for value in offsets_minutes)
        adjusted_text = " The final overrun was moved just before campaign end." if adjusted else ""
        if final_cleanup:
            return f"Specific repost plan saved: {plan}. Every repost replaces the previous post; final cleanup is enabled.{adjusted_text}"
        return (
            f"Specific repost plan saved: {plan}. Every repost replaces the previous post. "
            f"The final post will remain after campaign end because this plan has a gap over 47 hours.{adjusted_text}"
        )

    @staticmethod
    def _parse_owner_time(raw: str, timezone: str) -> datetime:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(timezone))
        return value.astimezone(UTC)
