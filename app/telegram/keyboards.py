from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.campaigns.models import Button


def auto_button_rows(buttons: list[Button], layout: str = "AUTO") -> list[list[Button]]:
    """Horizontal-first pragmatic heuristic; the owner can override with explicit/custom rows."""
    if layout == "CUSTOM":
        rows: dict[int, list[Button]] = {}
        for button in sorted(buttons, key=lambda item: (item.row, item.position)):
            rows.setdefault(button.row, []).append(button)
        return list(rows.values())
    per_row = int(layout) if layout in {"1", "2", "3"} else None
    rows: list[list[Button]] = []
    for button in buttons:
        # Telegram labels vary by client. This deliberately conservative estimate avoids truncation.
        label_width = len(button.text) + 4
        if not rows or (per_row and len(rows[-1]) >= per_row) or (not per_row and (
            len(rows[-1]) >= 3 or sum(len(item.text) + 4 for item in rows[-1]) + label_width > 34
        )):
            rows.append([])
        rows[-1].append(button)
    return rows


def audience_markup(buttons: list[Button], layout: str = "AUTO") -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=button.text,
            url=str(button.url),
            # Telegram's native CTA colors are optional. Older saved campaigns
            # use ``default`` and remain neutral without a migration.
            style=None if button.style == "default" else button.style,
        )
        for button in row
    ] for row in auto_button_rows(buttons, layout)])


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Create Campaign", callback_data="home:create"),
         InlineKeyboardButton(text="Campaigns", callback_data="home:campaigns")],
        [InlineKeyboardButton(text="Network", callback_data="home:network"),
         InlineKeyboardButton(text="Backups", callback_data="home:backups")],
        [InlineKeyboardButton(text="Settings", callback_data="home:settings")],
    ])
