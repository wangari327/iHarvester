from types import SimpleNamespace

import pytest

from app.db.repositories import Repositories


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.requested_length = None

    async def to_list(self, length):
        self.requested_length = length
        return self.rows


class AsyncCollection:
    def __init__(self, rows):
        self.rows = rows
        self.pipeline = None

    async def aggregate(self, pipeline):
        self.pipeline = pipeline
        return Cursor(self.rows)


class CleanupDeliveryCollection:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.update_many_calls = []
        self.bulk_write_calls = []

    def find(self, query, projection):
        assert query == {"campaign_id": "cmp", "cycle_number": -1}
        assert projection == {"_id": 0, "channel_id": 1, "operation": 1, "status": 1}
        return ShareCursor(self.existing)

    async def update_many(self, query, update):
        self.update_many_calls.append((query, update))
        return SimpleNamespace(modified_count=0)

    async def bulk_write(self, operations, ordered=False):
        self.bulk_write_calls.append((operations, ordered))
        return SimpleNamespace(modified_count=0, upserted_count=0)


class CountCollection:
    def __init__(self, counts):
        self.counts = iter(counts)
        self.queries = []

    async def count_documents(self, query, **kwargs):
        self.queries.append((query, kwargs))
        return next(self.counts)


class InterruptedCleanupDeliveries:
    async def distinct(self, field, query):
        assert field == "campaign_id"
        assert query == {"operation": "CLEANUP", "status": "CANCELLED"}
        return ["cmp_archived"]


class RecoverableCampaigns:
    def __init__(self):
        self.calls = []

    async def update_one(self, query, update):
        self.calls.append((query, update))
        return SimpleNamespace(modified_count=1)


class ShareCursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_args):
        return self

    async def to_list(self, _length):
        return list(self.rows)


class VariantShareCollection:
    def __init__(self):
        self.documents = []

    @staticmethod
    def _matches(document, query):
        for key, expected in query.items():
            value = document.get(key)
            if isinstance(expected, dict) and "$ne" in expected:
                if value == expected["$ne"]:
                    return False
            elif value != expected:
                return False
        return True

    async def find_one(self, query):
        return next((item for item in self.documents if self._matches(item, query)), None)

    async def insert_one(self, document):
        self.documents.append(dict(document))
        return SimpleNamespace(inserted_id=document["share_code"])

    def find(self, query):
        return ShareCursor(item for item in self.documents if self._matches(item, query))

    async def update_one(self, query, update):
        document = await self.find_one(query)
        if not document:
            return SimpleNamespace(modified_count=0)
        document.update(update.get("$set", {}))
        for key, amount in update.get("$inc", {}).items():
            document[key] = document.get(key, 0) + amount
        return SimpleNamespace(modified_count=1)

    async def update_many(self, query, update):
        matches = [item for item in self.documents if self._matches(item, query)]
        for document in matches:
            document.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=len(matches))


@pytest.mark.asyncio
async def test_status_aggregates_await_cursor_before_reading_rows() -> None:
    channels = AsyncCollection([{"_id": "ACTIVE", "count": 3}])
    campaigns = AsyncCollection([{"_id": "SCHEDULED", "count": 2}])
    deliveries = AsyncCollection([{"_id": "SENT", "count": 4}])
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(channels=channels, campaigns=campaigns, deliveries=deliveries)

    assert await repositories.channel_status_counts() == {"ACTIVE": 3}
    assert await repositories.campaign_status_counts() == {"SCHEDULED": 2}
    assert await repositories.delivery_summary("cmp", 0) == {"SENT": 4}


@pytest.mark.asyncio
async def test_ending_cancels_future_sends_but_never_cleanup_jobs() -> None:
    """Regression: 1,643 live posts matched 1,643 cancelled cleanup jobs."""
    deliveries = CleanupDeliveryCollection()
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(deliveries=deliveries)

    await repositories.cancel_pending_campaign_deliveries("cmp")

    query, update = deliveries.update_many_calls[0]
    assert query == {
        "campaign_id": "cmp",
        "operation": {"$ne": "CLEANUP"},
        "status": {"$in": ["PENDING", "RETRY_WAIT", "PAUSED"]},
    }
    assert update["$set"]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cleanup_materialization_revives_cancelled_jobs_for_live_posts() -> None:
    deliveries = CleanupDeliveryCollection(
        [{"channel_id": -1001, "operation": "CLEANUP", "status": "CANCELLED"}]
    )
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(deliveries=deliveries)

    async def live_states(_campaign_id):
        return [{"channel_id": -1001, "current_message_ids": [41, 42]}]

    repositories.live_states = live_states
    assert await repositories.materialize_cleanup_deliveries("cmp") == 1

    assert len(deliveries.bulk_write_calls) == 1
    repair = deliveries.bulk_write_calls[0][0][0]
    assert repair._filter["status"] == {"$in": ["CANCELLED", "CLEANED", "PAUSED"]}
    assert repair._doc["$set"]["message_ids"] == [41, 42]
    assert repair._doc["$set"]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_transient_cleanup_failures_are_recovered_without_touching_permission_blocks() -> None:
    deliveries = CleanupDeliveryCollection()
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(deliveries=deliveries)

    assert await repositories.recover_retryable_cleanup_failures("cmp") == 0

    query, update = deliveries.update_many_calls[0]
    assert query["status"] == "CLEANUP_FAILED"
    assert query["error_category"] == "RETRY_EXHAUSTED"
    assert {"cleanup_auto_retry_count": {"$exists": False}} in query["$and"][0]["$or"]
    assert update["$set"]["status"] == "PENDING"
    assert update["$inc"]["cleanup_auto_retry_count"] == 1


@pytest.mark.asyncio
async def test_cleanup_cannot_report_complete_while_any_post_is_still_live() -> None:
    live_states = CountCollection([1])
    deliveries = CountCollection([0])
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(campaign_channel_state=live_states, deliveries=deliveries)

    assert not await repositories.cleanup_is_complete("cmp")
    assert not deliveries.queries

    repositories.db.campaign_channel_state = CountCollection([0])
    assert await repositories.cleanup_is_complete("cmp")


@pytest.mark.asyncio
async def test_false_success_archive_with_cancelled_cleanup_is_reopened_automatically() -> None:
    campaigns = RecoverableCampaigns()
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(
        deliveries=InterruptedCleanupDeliveries(),
        campaign_channel_state=CountCollection([1]),
        campaigns=campaigns,
    )

    assert await repositories.recover_interrupted_cleanup_campaigns() == 1
    query, update = campaigns.calls[0]
    assert query == {
        "campaign_id": "cmp_archived",
        "status": "ARCHIVED",
        "delete_on_end": True,
    }
    assert update["$set"]["status"] == "ENDING"
    assert update["$set"]["end_reason"] == "cleanup_recovery"


def test_audience_selector_supports_minimum_and_bounded_size_ranges() -> None:
    repositories = Repositories.__new__(Repositories)

    assert repositories._active_channel_query({"minimum_members": 1_000})["member_count"] == {"$gte": 1_000}
    assert repositories._active_channel_query({"minimum_members": 1_000, "maximum_members": 50_000})["member_count"] == {
        "$gte": 1_000,
        "$lte": 50_000,
    }


@pytest.mark.asyncio
async def test_variant_share_repository_reuses_exact_snapshots_and_revokes_them() -> None:
    collection = VariantShareCollection()
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(variant_shares=collection)
    arguments = {
        "owner_id": 11,
        "campaign_id": "cmp",
        "campaign_name": "Campaign",
        "variant_id": "var_1",
        "variant_index": 0,
        "variant_revision": 2,
        "snapshot_hash": "hash-one",
        "creative": {"id": "var_1", "kind": "TEXT", "text": "Hello"},
    }

    created = await repositories.get_or_create_variant_share(**arguments)
    reused = await repositories.get_or_create_variant_share(**arguments)
    assert reused["share_code"] == created["share_code"]
    assert len(collection.documents) == 1

    newer = await repositories.get_or_create_variant_share(
        **{**arguments, "snapshot_hash": "hash-two", "creative": {**arguments["creative"], "text": "Changed"}}
    )
    assert newer["share_code"] != created["share_code"]
    assert len(await repositories.active_variant_shares(11, "cmp", "var_1")) == 2

    assert await repositories.revoke_variant_share(created["share_code"], 11)
    assert await repositories.get_variant_share(created["share_code"], 11) is None
    assert collection.documents[0]["purge_at"] > collection.documents[0]["revoked_at"]
