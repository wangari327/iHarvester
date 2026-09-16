"""Owner-only inline exports of immutable campaign-variant snapshots."""

from __future__ import annotations

from typing import Any

from aiogram.types import (
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedSticker,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
    InputRichMessage,
    InputRichMessageContent,
    InputTextMessageContent,
    LinkPreviewOptions,
    MessageEntity,
)

from app.campaigns.models import Creative
from app.telegram.keyboards import audience_markup, auto_button_rows

_INLINE_KINDS = {
    "TEXT",
    "PHOTO",
    "VIDEO",
    "ANIMATION",
    "DOCUMENT",
    "AUDIO",
    "VOICE",
    "STICKER",
    "RICH_MESSAGE",
}


class UnsupportedInlineCreative(ValueError):
    """The Bot API cannot reproduce this creative as one inline result."""


def inline_support_error(creative: dict[str, Any]) -> str | None:
    kind = str(creative.get("kind", ""))
    if kind == "MEDIA_GROUP":
        return "Telegram inline mode cannot insert an album as one selectable result. Share one album item as a separate variant instead."
    if kind == "VIDEO_NOTE":
        return "Telegram inline mode has no video-note result type. Save the clip as a normal video variant to share it inline."
    if kind not in _INLINE_KINDS:
        return f"Telegram inline mode cannot reproduce {kind.replace('_', ' ').lower() or 'this content type'}."
    if kind == "RICH_MESSAGE" and not (creative.get("rich_payload") or {}).get("rich_message"):
        return "This legacy rich-message snapshot has no reusable rich_message payload."
    return None


def button_layout_manifest(creative_data: dict[str, Any]) -> str:
    """Portable, human-readable fallback for broadcast bots that drop markup."""
    creative = Creative.model_validate(creative_data)
    rows = auto_button_rows(creative.buttons, creative.button_layout)
    if not rows:
        return "This variant has no CTA buttons."
    lines = ["iHarvester CTA button layout"]
    for row_number, row in enumerate(rows, start=1):
        lines.append(f"\nRow {row_number}")
        for button_number, button in enumerate(row, start=1):
            lines.append(f"{button_number}. {button.text}")
            lines.append(f"Style: {button.style}")
            lines.append(str(button.url))
    return "\n".join(lines)


def _entities(rows: list[dict[str, Any]]) -> list[MessageEntity] | None:
    return [MessageEntity.model_validate(row) for row in rows] or None


def _title(share: dict[str, Any], creative: Creative) -> str:
    campaign_name = str(share.get("campaign_name") or "Campaign")
    number = int(share.get("variant_index", 0)) + 1
    return f"{campaign_name} · Variant {number} · {creative.kind.replace('_', ' ').title()}"[:128]


def inline_result_for_share(share: dict[str, Any]) -> Any:
    """Build one exact, keyboard-bearing inline result from a frozen share."""
    creative_data = share["creative"]
    if error := inline_support_error(creative_data):
        raise UnsupportedInlineCreative(error)
    creative = Creative.model_validate(creative_data)
    markup = audience_markup(creative.buttons, creative.button_layout)
    title = _title(share, creative)
    result_id = str(share["share_code"]).replace("-", "_").lower()

    if creative.kind == "TEXT":
        link_options = LinkPreviewOptions.model_validate(creative.link_preview_options) if creative.link_preview_options else None
        return InlineQueryResultArticle(
            id=result_id,
            title=title,
            description=f"Saved snapshot · {len(creative.buttons)} CTA button{'s' if len(creative.buttons) != 1 else ''}",
            input_message_content=InputTextMessageContent(
                message_text=creative.text or "",
                parse_mode=None,
                entities=_entities(creative.entities),
                link_preview_options=link_options,
            ),
            reply_markup=markup,
        )

    if creative.kind == "RICH_MESSAGE":
        rich_message = InputRichMessage.model_validate((creative.rich_payload or {})["rich_message"])
        return InlineQueryResultArticle(
            id=result_id,
            title=title,
            description="Saved rich-message snapshot",
            input_message_content=InputRichMessageContent(rich_message=rich_message),
            reply_markup=markup,
        )

    media = creative.media[0]
    file_id = media["file_id"]
    caption = creative.caption
    caption_entities = _entities(creative.caption_entities)
    common: dict[str, Any] = {
        "id": result_id,
        "caption": caption,
        "parse_mode": None,
        "caption_entities": caption_entities,
        "reply_markup": markup,
    }
    if creative.kind in {"PHOTO", "VIDEO", "ANIMATION"}:
        # Explicit None prevents aiogram's client-default sentinel from leaking
        # into nested inline-result serialization when no preference was saved.
        common["show_caption_above_media"] = creative.caption_above_media
    if creative.kind == "PHOTO":
        return InlineQueryResultCachedPhoto(photo_file_id=file_id, title=title, **common)
    if creative.kind == "VIDEO":
        return InlineQueryResultCachedVideo(video_file_id=file_id, title=title, **common)
    if creative.kind == "ANIMATION":
        mime_type = str(media.get("mime_type") or "").lower()
        file_name = str(media.get("file_name") or "").lower()
        if mime_type == "image/gif" or file_name.endswith(".gif"):
            return InlineQueryResultCachedGif(gif_file_id=file_id, title=title, **common)
        return InlineQueryResultCachedMpeg4Gif(mpeg4_file_id=file_id, title=title, **common)
    if creative.kind == "DOCUMENT":
        common.pop("show_caption_above_media", None)
        return InlineQueryResultCachedDocument(document_file_id=file_id, title=title, **common)
    if creative.kind == "AUDIO":
        common.pop("show_caption_above_media", None)
        return InlineQueryResultCachedAudio(audio_file_id=file_id, **common)
    if creative.kind == "VOICE":
        common.pop("show_caption_above_media", None)
        return InlineQueryResultCachedVoice(voice_file_id=file_id, title=title, **common)
    if creative.kind == "STICKER":
        return InlineQueryResultCachedSticker(id=result_id, sticker_file_id=file_id, reply_markup=markup)
    raise UnsupportedInlineCreative("This content type cannot be shared inline.")
