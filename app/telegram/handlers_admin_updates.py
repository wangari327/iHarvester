"""Automatic source-channel registry updates from Telegram admin events."""

from __future__ import annotations

from typing import Any

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import Chat, ChatMember, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.campaigns.models import ChannelStatus
from app.db.repositories import Repositories
from app.utils.time import utcnow


async def refresh_channel(
    bot: Bot,
    repositories: Repositories,
    chat_id: int,
    chat: Chat | None = None,
    *,
    request_limiter: Any | None = None,
    preserve_manual_pause: bool = False,
) -> bool:
    async def request(call):
        if request_limiter:
            await request_limiter.acquire()
        return await call

    existing = await repositories.get_channel(chat_id) if preserve_manual_pause else None
    keep_paused = bool(existing and existing.get("status") == ChannelStatus.INACTIVE_MANUAL.value)
    try:
        chat = chat or await request(bot.get_chat(chat_id))
        member: ChatMember = await request(bot.get_chat_member(chat_id, bot.id))
        member_count = await request(bot.get_chat_member_count(chat_id))
    except Exception:
        if chat:
            await repositories.upsert_channel(
                {
                    "telegram_chat_id": chat.id,
                    "title": chat.title or str(chat.id),
                    "username": chat.username,
                    "type": "channel",
                    "is_public": bool(chat.username),
                    "member_count": None,
                    "status": ChannelStatus.INACTIVE_MANUAL.value if keep_paused else ChannelStatus.NEEDS_ATTENTION.value,
                    "permissions": {
                        "is_admin": False,
                        "can_post_messages": False,
                        "can_delete_messages": False,
                        "can_invite_users": False,
                    },
                    "last_error_code": "REFRESH_FAILED",
                    "last_verified_at": utcnow(),
                }
            )
        else:
            await repositories.set_channel_status(
                chat_id,
                ChannelStatus.INACTIVE_MANUAL if keep_paused else ChannelStatus.NEEDS_ATTENTION,
                last_error_code="REFRESH_FAILED",
            )
        return False
    is_admin = member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    can_post = bool(getattr(member, "can_post_messages", is_admin))
    status = (
        ChannelStatus.INACTIVE_MANUAL
        if keep_paused and is_admin and can_post
        else ChannelStatus.ACTIVE
        if is_admin and can_post
        else ChannelStatus.UNAVAILABLE
    )
    await repositories.upsert_channel(
        {
            "telegram_chat_id": chat.id,
            "title": chat.title or str(chat.id),
            "username": chat.username,
            "type": "channel",
            "is_public": bool(chat.username),
            "member_count": member_count,
            "status": status.value,
            "permissions": {
                "is_admin": is_admin,
                "can_post_messages": can_post,
                "can_delete_messages": bool(getattr(member, "can_delete_messages", False)),
                "can_invite_users": bool(getattr(member, "can_invite_users", False)),
            },
            "last_verified_at": utcnow(),
        }
    )
    return status == ChannelStatus.ACTIVE


class ChannelAdminHandlers:
    def __init__(self, repositories: Repositories) -> None:
        self.repositories = repositories
        self.router = Router(name="channel_admin")
        self.router.my_chat_member.register(self.my_chat_member)

    async def my_chat_member(self, update: ChatMemberUpdated, bot: Bot) -> None:
        if update.chat.type != "channel":
            return
        new_status = update.new_chat_member.status
        if new_status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            await refresh_channel(bot, self.repositories, update.chat.id, update.chat)
        else:
            await self.repositories.set_channel_status(
                update.chat.id,
                ChannelStatus.UNAVAILABLE,
                last_error_code=f"BOT_STATUS_{new_status}",
                last_admin_update_at=utcnow(),
            )


async def register_forwarded_channel(message: Message, bot: Bot, repositories: Repositories) -> bool:
    origin = getattr(message, "forward_origin", None)
    source_chat = getattr(origin, "chat", None)
    # Older Bot API updates and a few forwarded-content cases surface this alternate field.
    source_chat = source_chat or getattr(message, "forward_from_chat", None)
    if not source_chat or source_chat.type != "channel":
        return False
    success = await refresh_channel(bot, repositories, source_chat.id, source_chat)
    controls = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Register/Refresh", callback_data=f"reg:{source_chat.id}:refresh"),
                InlineKeyboardButton(text="Tag", callback_data=f"reg:{source_chat.id}:tag"),
            ],
            [InlineKeyboardButton(text="Back", callback_data="home:network")],
        ]
    )
    await message.answer(f"{'Registered' if success else 'Saved for attention'}: {source_chat.title or source_chat.id}", reply_markup=controls)
    return True
