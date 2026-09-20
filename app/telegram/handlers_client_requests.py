"""Narrow Telegram intake for materials attached to a client portal request."""

from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.filters import Filter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.db.repositories import Repositories
from app.telegram.formatting import capture_creative

_PROMO_START = re.compile(r"^/start\s+promo_(r[0-9a-f]{16})_([0-9a-f]{24})(?:\s|$)")


class PromoMaterialStart(Filter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.chat.type == "private" and message.text and _PROMO_START.match(message.text))


class HasClientMaterialSession(Filter):
    def __init__(self, repositories: Repositories) -> None:
        self.repositories = repositories

    async def __call__(self, message: Message) -> bool:
        return bool(
            message.chat.type == "private"
            and message.from_user
            and await self.repositories.client_material_session(message.from_user.id)
        )


class ClientRequestHandlers:
    """Lets a client attach post materials without opening the owner controls."""

    def __init__(self, *, repositories: Repositories, owner_ids: frozenset[int]) -> None:
        self.repositories = repositories
        self.owner_ids = owner_ids
        self.router = Router(name="client-promotion-requests")
        self.router.message.register(self.start_material_upload, PromoMaterialStart())
        self.router.message.register(self.capture_material, HasClientMaterialSession(repositories), F.chat.type == "private")

    async def start_material_upload(self, message: Message) -> None:
        if not message.from_user or not message.text:
            return
        match = _PROMO_START.match(message.text)
        if not match:
            return
        request = await self.repositories.begin_client_material_session(match.group(1), match.group(2), message.from_user.id)
        if not request:
            await message.answer("This materials link is no longer available, has already been submitted, or belongs to another Telegram user.")
            return
        await message.answer(
            "Send or forward the finished post material now. Formatting and supported media are retained. "
            "You can send up to 20 separate posts/variants. When you are finished, send /done. Send /cancel to stop without submitting."
        )

    async def capture_material(self, message: Message, bot: Bot) -> None:
        if not message.from_user:
            return
        session = await self.repositories.client_material_session(message.from_user.id)
        if not session:
            return
        command = (message.text or "").strip().lower()
        if command == "/cancel":
            await self.repositories.cancel_client_material_session(message.from_user.id)
            await message.answer("Material upload cancelled. Your campaign manager has not received an approval request from this upload.")
            return
        if command == "/done":
            request = await self.repositories.finish_client_material_session(session["request_id"], message.from_user.id)
            if not request:
                await message.answer("Send at least one supported post before /done.")
                return
            await message.answer("Materials received. Your promotion request is now waiting for payment and manager approval.")
            await self._notify_owners(bot, request)
            return
        try:
            creative = capture_creative(message).model_dump(mode="json")
        except ValueError as error:
            await message.answer(
                f"I cannot use that as campaign material: {error}. "
                "Send a text, photo, video, document, audio, voice, GIF, sticker, or video note."
            )
            return
        request = await self.repositories.append_client_request_material(session["request_id"], message.from_user.id, creative)
        if not request:
            await message.answer("This upload is no longer accepting material. Open the shared progress page to make a new request.")
            await self.repositories.cancel_client_material_session(message.from_user.id)
            return
        count = len(request.get("materials", []))
        await message.answer(f"Saved material {count}/20. Send another post or /done when you are finished.")

    async def _notify_owners(self, bot: Bot, request: dict[str, object]) -> None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Review client request", callback_data=f"req:{request['request_id']}:open")]]
        )
        text = (
            "Client promotion request ready for approval\n\n"
            f"Client: {request.get('client_name') or 'not supplied'}\n"
            f"Type: {str(request.get('request_type') or '').title()}\n"
            f"Materials: {len(request.get('materials') or [])}\n"
            "Open it to review, confirm payment, and approve or reject."
        )
        for owner_id in self.owner_ids:
            try:
                await bot.send_message(owner_id, text, reply_markup=markup)
            except Exception:
                # Material acceptance must not be retried merely because an
                # owner notification was transiently unavailable.
                continue
