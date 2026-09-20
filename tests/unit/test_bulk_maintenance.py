from types import SimpleNamespace

import pytest

from app.db.repositories import Repositories
from app.telegram.handlers_owner import refresh_attention_channels


class BulkRefreshRepositories:
    def __init__(self) -> None:
        self.statuses = {
            -1001: "NEEDS_ATTENTION",
            -1002: "NEEDS_ATTENTION",
            -1003: "NEEDS_ATTENTION",
            -1004: "NEEDS_ATTENTION",
            -2001: "INACTIVE_MANUAL",
        }

    async def channel_ids_by_status(self, status: str) -> list[int]:
        return [chat_id for chat_id, current in self.statuses.items() if current == status]

    async def get_channel(self, chat_id: int):
        return {"telegram_chat_id": chat_id, "status": self.statuses[chat_id]}


@pytest.mark.asyncio
async def test_bulk_refresh_checks_only_attention_channels_and_summarizes_every_outcome(monkeypatch) -> None:
    repositories = BulkRefreshRepositories()
    refreshed: list[int] = []

    async def fake_refresh(bot, current_repositories, chat_id):
        assert current_repositories is repositories
        refreshed.append(chat_id)
        current_repositories.statuses[chat_id] = {
            -1001: "ACTIVE",
            -1002: "ACTIVE",
            -1003: "UNAVAILABLE",
            -1004: "NEEDS_ATTENTION",
        }[chat_id]
        return current_repositories.statuses[chat_id] == "ACTIVE"

    monkeypatch.setattr("app.telegram.handlers_owner.refresh_channel", fake_refresh)

    result = await refresh_attention_channels(object(), repositories, concurrency=2)

    assert sorted(refreshed) == [-1004, -1003, -1002, -1001]
    assert -2001 not in refreshed
    assert result == {"checked": 4, "active": 2, "needs_attention": 1, "unavailable": 1, "other": 0}


class DeleteResult:
    def __init__(self, deleted_count: int = 0) -> None:
        self.deleted_count = deleted_count


class DeleteCollection:
    def __init__(self, *, document=None, count: int = 0, deleted_count: int = 0) -> None:
        self.document = document
        self.count = count
        self.deleted_count = deleted_count
        self.deleted_many: list[dict] = []
        self.deleted_one: list[dict] = []

    async def find_one(self, query):
        return self.document

    async def count_documents(self, query):
        return self.count

    async def delete_many(self, query):
        self.deleted_many.append(query)
        return DeleteResult()

    async def delete_one(self, query):
        self.deleted_one.append(query)
        return DeleteResult(self.deleted_count)


def deletion_repositories(*, live_count: int = 0) -> tuple[Repositories, SimpleNamespace]:
    db = SimpleNamespace(
        campaigns=DeleteCollection(document={"campaign_id": "cmp", "status": "ARCHIVED"}, deleted_count=1),
        campaign_cycles=DeleteCollection(),
        deliveries=DeleteCollection(),
        join_events=DeleteCollection(),
        campaign_channel_state=DeleteCollection(count=live_count),
        variant_shares=DeleteCollection(),
        campaign_portal_shares=DeleteCollection(),
        client_promotion_requests=DeleteCollection(),
    )
    repositories = Repositories.__new__(Repositories)
    repositories.db = db
    return repositories, db


@pytest.mark.asyncio
async def test_archived_deletion_cascades_finished_campaign_history() -> None:
    repositories, db = deletion_repositories()

    assert await repositories.delete_archived_campaign("cmp")

    expected = [{"campaign_id": "cmp"}]
    assert db.campaign_cycles.deleted_many == expected
    assert db.deliveries.deleted_many == expected
    assert db.join_events.deleted_many == expected
    assert db.campaign_channel_state.deleted_many == expected
    assert db.variant_shares.deleted_many == expected
    assert db.campaign_portal_shares.deleted_many == expected
    assert db.client_promotion_requests.deleted_many == expected
    assert db.campaigns.deleted_one == [{"campaign_id": "cmp", "status": "ARCHIVED"}]


@pytest.mark.asyncio
async def test_archived_deletion_is_blocked_while_retained_posts_are_live() -> None:
    repositories, db = deletion_repositories(live_count=3)

    with pytest.raises(ValueError, match="3 live posts"):
        await repositories.delete_archived_campaign("cmp")

    assert not db.campaign_cycles.deleted_many
    assert not db.deliveries.deleted_many
    assert not db.join_events.deleted_many
    assert not db.campaigns.deleted_one
