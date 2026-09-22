from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus

from app.campaigns.models import ChannelStatus
from app.network.refresh_worker import ChannelRefreshWorker
from app.telegram.handlers_admin_updates import refresh_channel
from app.telegram.handlers_owner import OwnerHandlers


class NoopLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> None:
        self.calls += 1


class RefreshBot:
    id = 999

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, title="Channel", username=None)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR, can_post_messages=True, can_delete_messages=True, can_invite_users=True)

    async def get_chat_member_count(self, chat_id):
        return 1234

    async def get_chat_administrators(self, chat_id):
        return [
            SimpleNamespace(
                status=ChatMemberStatus.CREATOR,
                user=SimpleNamespace(id=77, first_name="Account", last_name="One", username="account_one"),
            )
        ]


class RefreshRepositories:
    def __init__(self) -> None:
        self.existing = {"telegram_chat_id": -1001, "status": ChannelStatus.INACTIVE_MANUAL.value}
        self.upserted = None
        self.completed = []

    async def get_channel(self, chat_id):
        return self.upserted or self.existing

    async def upsert_channel(self, document):
        self.upserted = document

    async def set_channel_status(self, chat_id, status, **details):
        self.existing = {"telegram_chat_id": chat_id, "status": status.value, **details}

    async def complete_network_refresh_job(self, job_id, status, **details):
        self.completed.append((job_id, status, details))

    async def retry_network_refresh_job(self, *args, **kwargs):
        raise AssertionError("successful refresh must not retry")


@pytest.mark.asyncio
async def test_full_refresh_keeps_a_manually_paused_channel_paused() -> None:
    repositories = RefreshRepositories()
    limiter = NoopLimiter()

    active = await refresh_channel(
        RefreshBot(),
        repositories,
        -1001,
        request_limiter=limiter,
        preserve_manual_pause=True,
    )

    assert active is False
    assert repositories.upserted["status"] == ChannelStatus.INACTIVE_MANUAL.value
    assert repositories.upserted["member_count"] == 1234
    assert repositories.upserted["access_link"] == "https://t.me/c/1/1"
    assert repositories.upserted["owner_account"]["telegram_user_id"] == 77
    assert repositories.upserted["owner_account"]["username"] == "account_one"
    assert limiter.calls == 4


@pytest.mark.asyncio
async def test_network_refresh_worker_records_a_completed_job(monkeypatch) -> None:
    repositories = RefreshRepositories()

    async def fake_refresh(*args, **kwargs):
        assert kwargs["preserve_manual_pause"] is True
        return True

    monkeypatch.setattr("app.network.refresh_worker.refresh_channel", fake_refresh)
    worker = ChannelRefreshWorker(
        worker_id="network",
        bot=object(),
        repositories=repositories,
        request_limiter=NoopLimiter(),
        lease_seconds=30,
        max_attempts=3,
    )

    await worker.process({"_id": "job", "channel_id": -1001, "attempts": 1})

    assert repositories.completed == [
        ("job", "COMPLETED", {"observed_status": "INACTIVE_MANUAL", "member_count": None, "access_verified": True})
    ]


@pytest.mark.asyncio
async def test_channel_details_show_owner_identity_and_open_link() -> None:
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.repositories = SimpleNamespace(
        get_channel=AsyncMock(
            return_value={
                "telegram_chat_id": -1001,
                "title": "Private channel",
                "status": "ACTIVE",
                "member_count": 1234,
                "permissions": {"can_post_messages": True},
                "access_link": "https://t.me/c/1/1",
                "owner_account": {"telegram_user_id": 77, "display_name": "Account One", "username": "account_one"},
            }
        )
    )
    handlers._render = AsyncMock()

    await handlers._show_channel(object(), -1001)

    _, text, markup = handlers._render.await_args.args
    assert "Owner account: Account One (@account_one) • ID 77" in text
    open_button = next(button for row in markup.inline_keyboard for button in row if button.text == "Open channel")
    assert open_button.url == "https://t.me/c/1/1"
