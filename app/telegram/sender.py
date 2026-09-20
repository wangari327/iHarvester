from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    MessageEntity,
)

from app.campaigns.models import Creative
from app.telegram.keyboards import audience_markup
from app.telegram.raw_api import RawTelegramAPI


@dataclass(frozen=True)
class SendResult:
    message_ids: list[int]


class TelegramSender:
    def __init__(self, bot: Bot, raw_api: RawTelegramAPI) -> None:
        self.bot = bot
        self.raw_api = raw_api

    @staticmethod
    def _entities(rows: list[dict[str, Any]]) -> list[MessageEntity]:
        return [MessageEntity.model_validate(row) for row in rows]

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        for message_id in message_ids:
            await self.bot.delete_message(chat_id, message_id)

    async def send_variant(self, chat_id: int, variant: dict[str, Any]) -> SendResult:
        creative = Creative.model_validate(variant)
        markup = audience_markup(creative.buttons, creative.button_layout)
        if creative.kind == "TEXT":
            link_options = LinkPreviewOptions.model_validate(creative.link_preview_options) if creative.link_preview_options else None
            message = await self.bot.send_message(
                chat_id, creative.text or "", entities=self._entities(creative.entities), reply_markup=markup,
                link_preview_options=link_options,
            )
            return SendResult([message.message_id])
        if creative.kind == "RICH_MESSAGE":
            payload = {**(creative.rich_payload or {}), "chat_id": chat_id}
            if creative.buttons:
                payload["reply_markup"] = audience_markup(creative.buttons, creative.button_layout).model_dump(mode="json")
            message = await self.raw_api.call("sendMessage", payload)
            return SendResult([message["message_id"]])
        if creative.kind == "MEDIA_GROUP":
            sent = await self.bot.send_media_group(chat_id, self._album(creative))
            ids = [message.message_id for message in sent]
            if markup:
                cta = await self.bot.send_message(chat_id, "·", reply_markup=markup)
                ids.append(cta.message_id)
            return SendResult(ids)
        file_id = creative.media[0]["file_id"]
        kwargs: dict[str, Any] = {
            "caption": creative.caption,
            "caption_entities": self._entities(creative.caption_entities),
            "reply_markup": markup,
        }
        if creative.caption_above_media is not None and creative.kind in {"PHOTO", "VIDEO", "ANIMATION"}:
            kwargs["show_caption_above_media"] = creative.caption_above_media
        method = {
            "PHOTO": self.bot.send_photo, "VIDEO": self.bot.send_video, "ANIMATION": self.bot.send_animation,
            "DOCUMENT": self.bot.send_document, "AUDIO": self.bot.send_audio, "VOICE": self.bot.send_voice,
            "VIDEO_NOTE": self.bot.send_video_note, "STICKER": self.bot.send_sticker,
        }[creative.kind]
        # Telegram rejects captions/markup options for a few special media kinds, so pass only accepted fields.
        if creative.kind in {"VIDEO_NOTE", "STICKER"}:
            kwargs = {"reply_markup": markup}
        message = await method(chat_id, file_id, **kwargs)
        return SendResult([message.message_id])

    async def edit_live_text_variant(self, chat_id: int, message_id: int, variant: dict[str, Any]) -> None:
        """Edit one existing text post without changing its channel position."""
        creative = Creative.model_validate(variant)
        if creative.kind != "TEXT":
            raise ValueError("only text posts can be repaired in place")
        link_options = LinkPreviewOptions.model_validate(creative.link_preview_options) if creative.link_preview_options else None
        await self.bot.edit_message_text(
            creative.text or "",
            chat_id=chat_id,
            message_id=message_id,
            entities=self._entities(creative.entities),
            reply_markup=audience_markup(creative.buttons, creative.button_layout),
            link_preview_options=link_options,
        )

    def _album(self, creative: Creative) -> list[Any]:
        items: list[Any] = []
        mapping = {"photo": InputMediaPhoto, "video": InputMediaVideo, "audio": InputMediaAudio, "document": InputMediaDocument}
        for index, media in enumerate(creative.media):
            cls = mapping.get(media["field"])
            if not cls:
                raise ValueError("Albums support photo, video, audio, and document items.")
            items.append(cls(
                media=media["file_id"], caption=creative.caption if index == 0 else None,
                caption_entities=self._entities(creative.caption_entities) if index == 0 else None,
            ))
        return items
