"""Explicit Mongo repositories: no ORM and no ephemeral delivery queue."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pymongo import ASCENDING, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from app.campaigns.models import ChannelStatus, DeliveryStatus
from app.campaigns.sharing import new_share_code
from app.db.client import Database
from app.utils.time import utcnow

Document = dict[str, Any]


class Repositories:
    def __init__(self, database: Database) -> None:
        self.db = database.db

    async def upsert_channel(self, document: Document) -> None:
        now = utcnow()
        chat_id = document["telegram_chat_id"]
        document = {**document, "updated_at": now, "last_admin_update_at": now}
        await self.db.channels.update_one(
            {"telegram_chat_id": chat_id},
            {"$set": document, "$setOnInsert": {"registered_at": now}},
            upsert=True,
        )

    async def set_channel_status(self, chat_id: int, status: ChannelStatus, **details: Any) -> None:
        await self.db.channels.update_one(
            {"telegram_chat_id": chat_id},
            {"$set": {"status": status.value, "updated_at": utcnow(), **details}},
        )

    async def get_channel(self, chat_id: int) -> Document | None:
        return await self.db.channels.find_one({"telegram_chat_id": chat_id})

    async def active_channels(self, selector: Document | None = None) -> list[Document]:
        query = self._active_channel_query(selector)
        return await self.db.channels.find(query).to_list(None)

    def _active_channel_query(self, selector: Document | None = None) -> Document:
        query: Document = {"status": ChannelStatus.ACTIVE.value, "permissions.can_post_messages": True}
        if selector:
            if tags := selector.get("tags_any"):
                query["tags"] = {"$in": tags}
            if include := selector.get("include_ids"):
                query["telegram_chat_id"] = {"$in": include}
            if exclude := selector.get("exclude_ids"):
                query.setdefault("telegram_chat_id", {})["$nin"] = exclude
            minimum_members = selector.get("minimum_members")
            maximum_members = selector.get("maximum_members")
            if minimum_members is not None or maximum_members is not None:
                audience: Document = {}
                if minimum_members is not None:
                    audience["$gte"] = minimum_members
                if maximum_members is not None:
                    audience["$lte"] = maximum_members
                query["member_count"] = audience
        return query

    async def active_channel_count(self, selector: Document | None = None, *, exclude_ids: set[int] | None = None) -> int:
        query = self._active_channel_query(selector)
        if exclude_ids:
            channel_query = query.setdefault("telegram_chat_id", {})
            if not isinstance(channel_query, dict):
                raise ValueError("target selector cannot combine a fixed include list with destination protection")
            existing_exclusions = list(channel_query.get("$nin", []))
            channel_query["$nin"] = [*existing_exclusions, *exclude_ids]
        return await self.db.channels.count_documents(query)

    async def channel_count(self, active_only: bool = False) -> int:
        query = {"status": ChannelStatus.ACTIVE.value, "permissions.can_post_messages": True} if active_only else {}
        return await self.db.channels.count_documents(query)

    async def channel_status_counts(self) -> Document:
        cursor = await self.db.channels.aggregate(
            [
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {row["_id"]: row["count"] for row in rows if row.get("_id")}

    async def list_channels(self, status: str | None = None, *, skip: int = 0, limit: int = 10) -> list[Document]:
        query: Document = {"status": status} if status else {}
        return await self.db.channels.find(query).sort("title", ASCENDING).skip(skip).limit(limit).to_list(limit)

    async def top_channels_by_members(self, limit: int = 15) -> list[Document]:
        """Rank every registered channel that has a verified numeric count."""
        safe_limit = max(1, min(int(limit), 30))
        return await self.db.channels.find({"member_count": {"$type": "number"}}).sort(
            [("member_count", -1), ("title", ASCENDING)]
        ).limit(safe_limit).to_list(safe_limit)

    async def channel_member_count_coverage(self) -> tuple[int, int]:
        known = await self.db.channels.count_documents({"member_count": {"$type": "number"}})
        total = await self.db.channels.count_documents({})
        return known, max(0, total - known)

    async def channel_ids_by_status(self, status: str) -> list[int]:
        rows = await self.db.channels.find(
            {"status": status},
            {"_id": 0, "telegram_chat_id": 1},
        ).to_list(None)
        return [int(row["telegram_chat_id"]) for row in rows]

    async def set_channel_tags(self, chat_id: int, tags: list[str]) -> None:
        await self.db.channels.update_one(
            {"telegram_chat_id": chat_id},
            {"$set": {"tags": tags, "updated_at": utcnow()}},
        )

    async def set_channel_manual_enabled(self, chat_id: int, enabled: bool) -> None:
        status = ChannelStatus.ACTIVE.value if enabled else ChannelStatus.INACTIVE_MANUAL.value
        await self.db.channels.update_one(
            {"telegram_chat_id": chat_id},
            {"$set": {"status": status, "updated_at": utcnow()}},
        )

    async def create_campaign(self, campaign: Document) -> None:
        await self.db.campaigns.insert_one(campaign)

    async def get_campaign(self, campaign_id: str) -> Document | None:
        return await self.db.campaigns.find_one({"campaign_id": campaign_id})

    async def recent_pending_rerun(self, source_campaign_id: str, owner_id: int, since: Any) -> Document | None:
        """Debounce rapid repeated taps on an archived campaign's rerun button."""
        return await self.db.campaigns.find_one(
            {
                "rerun_of_campaign_id": source_campaign_id,
                "created_by": owner_id,
                "status": "DRAFT",
                "rerun_ready": True,
                "created_at": {"$gte": since},
            },
            sort=[("created_at", -1)],
        )

    async def update_campaign(self, campaign_id: str, update: Document) -> bool:
        result = await self.db.campaigns.update_one({"campaign_id": campaign_id}, {"$set": update})
        return result.modified_count == 1

    async def get_or_create_variant_share(
        self,
        *,
        owner_id: int,
        campaign_id: str,
        campaign_name: str,
        variant_id: str,
        variant_index: int,
        variant_revision: int | None,
        snapshot_hash: str,
        creative: Document,
    ) -> Document:
        """Reuse one active code per exact snapshot and tolerate concurrent taps."""
        active_query = {
            "owner_id": owner_id,
            "campaign_id": campaign_id,
            "variant_id": variant_id,
            "snapshot_hash": snapshot_hash,
            "revoked_at": None,
        }
        if existing := await self.db.variant_shares.find_one(active_query):
            return existing
        now = utcnow()
        for _ in range(10):
            document: Document = {
                **active_query,
                "share_code": new_share_code(),
                "campaign_name": campaign_name,
                "variant_index": variant_index,
                "variant_revision": variant_revision,
                "creative": creative,
                "created_at": now,
                "updated_at": now,
                "query_count": 0,
            }
            try:
                await self.db.variant_shares.insert_one(document)
                return document
            except DuplicateKeyError:
                # Either the random code collided, or another callback created
                # the same active snapshot between find_one and insert_one.
                if existing := await self.db.variant_shares.find_one(active_query):
                    return existing
        raise RuntimeError("could not allocate a unique manual-share code")

    async def get_variant_share(self, share_code: str, owner_id: int) -> Document | None:
        return await self.db.variant_shares.find_one(
            {"share_code": share_code, "owner_id": owner_id, "revoked_at": None}
        )

    async def active_variant_shares(self, owner_id: int, campaign_id: str, variant_id: str) -> list[Document]:
        return await self.db.variant_shares.find(
            {
                "owner_id": owner_id,
                "campaign_id": campaign_id,
                "variant_id": variant_id,
                "revoked_at": None,
            }
        ).sort("created_at", -1).to_list(None)

    async def record_variant_share_query(self, share_code: str, owner_id: int) -> None:
        await self.db.variant_shares.update_one(
            {"share_code": share_code, "owner_id": owner_id, "revoked_at": None},
            {"$set": {"last_queried_at": utcnow(), "updated_at": utcnow()}, "$inc": {"query_count": 1}},
        )

    async def revoke_variant_share(self, share_code: str, owner_id: int) -> bool:
        now = utcnow()
        result = await self.db.variant_shares.update_one(
            {"share_code": share_code, "owner_id": owner_id, "revoked_at": None},
            {"$set": {"revoked_at": now, "updated_at": now, "purge_at": now + timedelta(days=30)}},
        )
        return result.modified_count == 1

    async def revoke_variant_shares(
        self,
        *,
        owner_id: int,
        campaign_id: str,
        variant_id: str,
        except_snapshot_hash: str | None = None,
    ) -> int:
        query: Document = {
            "owner_id": owner_id,
            "campaign_id": campaign_id,
            "variant_id": variant_id,
            "revoked_at": None,
        }
        if except_snapshot_hash:
            query["snapshot_hash"] = {"$ne": except_snapshot_hash}
        now = utcnow()
        result = await self.db.variant_shares.update_many(
            query,
            {"$set": {"revoked_at": now, "updated_at": now, "purge_at": now + timedelta(days=30)}},
        )
        return result.modified_count

    async def advance_running_campaign(self, campaign_id: str, update: Document) -> bool:
        """Advance cycle state without reviving a concurrently paused/ending run."""
        result = await self.db.campaigns.update_one(
            {"campaign_id": campaign_id, "status": {"$in": ["SCHEDULED", "ACTIVE"]}},
            {"$set": update},
        )
        return result.modified_count == 1

    async def activate_draft(self, campaign_id: str, update: Document) -> Document | None:
        """Atomically win activation so repeated Launch callbacks cannot race."""
        return await self.db.campaigns.find_one_and_update(
            {"campaign_id": campaign_id, "status": "DRAFT"},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )

    async def return_scheduled_to_draft(self, campaign_id: str, update: Document) -> Document | None:
        return await self.db.campaigns.find_one_and_update(
            {"campaign_id": campaign_id, "status": "SCHEDULED"},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )

    async def pause_running_campaign(self, campaign_id: str, update: Document) -> Document | None:
        return await self.db.campaigns.find_one_and_update(
            {"campaign_id": campaign_id, "status": {"$in": ["SCHEDULED", "ACTIVE"]}},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )

    async def resume_paused_campaign(self, campaign_id: str, update: Document) -> Document | None:
        return await self.db.campaigns.find_one_and_update(
            {"campaign_id": campaign_id, "status": "PAUSED"},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )

    async def extend_running_campaign(self, campaign_id: str, update: Document, event: Document) -> Document | None:
        return await self.db.campaigns.find_one_and_update(
            {"campaign_id": campaign_id, "status": {"$in": ["SCHEDULED", "ACTIVE"]}},
            {"$set": update, "$push": {"extensions": event}},
            return_document=ReturnDocument.AFTER,
        )

    async def replace_running_variant(
        self,
        *,
        campaign_id: str,
        index: int,
        variant_id: str,
        expected_revision: int,
        revision: int,
        creative: Document,
        event: Document,
        schedule_update: Document | None = None,
    ) -> bool:
        """Atomically version one variant without changing cohort identities."""
        revision_path = f"variant_current_revisions.{variant_id}"
        set_update = {
            f"variants.{index}": creative,
            revision_path: revision,
            "updated_at": utcnow(),
            **(schedule_update or {}),
        }
        result = await self.db.campaigns.update_one(
            {
                "campaign_id": campaign_id,
                "status": {"$in": ["ACTIVE", "PAUSED"]},
                f"variants.{index}.id": variant_id,
                revision_path: expected_revision,
            },
            {
                "$set": set_update,
                "$push": {
                    f"variant_versions.{variant_id}": {
                        "revision": revision,
                        "creative": creative,
                        "created_at": utcnow(),
                    },
                    "variant_edit_events": event,
                },
                "$inc": {"version": 1},
            },
        )
        return result.modified_count == 1

    async def end_campaign_early(self, campaign_id: str) -> bool:
        now = utcnow()
        result = await self.db.campaigns.update_one(
            {"campaign_id": campaign_id, "status": {"$in": ["SCHEDULED", "ACTIVE", "PAUSED"]}},
            {
                "$set": {
                    "status": "ENDING",
                    "delete_on_end": True,
                    "delete_on_next_campaign": False,
                    "end_reason": "ended_early",
                    "ending_at": now,
                    "updated_at": now,
                }
            },
        )
        return result.modified_count == 1

    async def due_campaigns(self, now: Any) -> list[Document]:
        return await self.db.campaigns.find(
            {
                "$or": [
                    {"status": "SCHEDULED", "start_at_utc": {"$lte": now}},
                    {"status": "ACTIVE"},
                    {"status": "ENDING"},
                ]
            }
        ).to_list(None)

    async def create_cycle(self, cycle: Document, deliveries: list[Document]) -> bool:
        """Idempotently materialize durable cycle work, including recovery after a partial insert."""
        try:
            await self.db.campaign_cycles.insert_one(cycle)
            created = True
        except DuplicateKeyError:
            created = False
        if deliveries:
            operations = [
                UpdateOne(
                    {"campaign_id": delivery["campaign_id"], "cycle_number": delivery["cycle_number"], "channel_id": delivery["channel_id"]},
                    {"$setOnInsert": delivery},
                    upsert=True,
                )
                for delivery in deliveries
            ]
            for offset in range(0, len(operations), 500):
                await self.db.deliveries.bulk_write(operations[offset : offset + 500], ordered=False)
        return created

    async def cycle_exists(self, campaign_id: str, cycle_number: int) -> bool:
        return await self.db.campaign_cycles.count_documents({"campaign_id": campaign_id, "cycle_number": cycle_number}, limit=1) > 0

    async def campaigns_with_due_cleanup(self) -> list[Document]:
        return await self.db.campaigns.find({"status": "ENDING"}).to_list(None)

    async def claim_delivery(self, worker_id: str, lease_seconds: int) -> Document | None:
        now = utcnow()
        return await self.db.deliveries.find_one_and_update(
            {
                "$or": [
                    {"status": DeliveryStatus.PENDING.value},
                    {"status": DeliveryStatus.RETRY_WAIT.value, "next_retry_at": {"$lte": now}},
                    {"status": DeliveryStatus.PROCESSING.value, "lease_until": {"$lte": now}},
                ]
            },
            {
                "$set": {
                    "status": DeliveryStatus.PROCESSING.value,
                    "worker_id": worker_id,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("dispatch_rank", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    async def complete_delivery(self, delivery_id: Any, status: DeliveryStatus, **details: Any) -> None:
        await self.db.deliveries.update_one(
            {"_id": delivery_id},
            {"$set": {"status": status.value, "lease_until": None, "updated_at": utcnow(), **details}},
        )

    async def retry_delivery(self, delivery_id: Any, retry_after_seconds: float, **details: Any) -> None:
        now = utcnow()
        await self.db.deliveries.update_one(
            {"_id": delivery_id},
            {
                "$set": {
                    "status": DeliveryStatus.RETRY_WAIT.value,
                    "lease_until": None,
                    "next_retry_at": now + timedelta(seconds=retry_after_seconds),
                    "updated_at": now,
                    **details,
                }
            },
        )

    async def live_state(self, campaign_id: str, channel_id: int) -> Document | None:
        return await self.db.campaign_channel_state.find_one({"campaign_id": campaign_id, "channel_id": channel_id})

    async def save_live_state(self, campaign_id: str, channel_id: int, cycle_number: int, variant_index: int, message_ids: list[int]) -> None:
        await self.db.campaign_channel_state.update_one(
            {"campaign_id": campaign_id, "channel_id": channel_id},
            {
                "$set": {
                    "current_message_ids": message_ids,
                    "current_cycle_number": cycle_number,
                    "current_variant_index": variant_index,
                    "updated_at": utcnow(),
                }
            },
            upsert=True,
        )

    async def clear_live_state(self, campaign_id: str, channel_id: int) -> None:
        await self.db.campaign_channel_state.delete_one({"campaign_id": campaign_id, "channel_id": channel_id})

    async def clear_campaign_live_states(self, campaign_id: str) -> None:
        await self.db.campaign_channel_state.delete_many({"campaign_id": campaign_id})

    async def live_states(self, campaign_id: str) -> list[Document]:
        return await self.db.campaign_channel_state.find({"campaign_id": campaign_id}).to_list(None)

    async def oldest_live_state_updated_at(self, campaign_id: str) -> Any | None:
        """Oldest confirmed campaign post, used to stay inside Telegram's delete window."""
        state = await self.db.campaign_channel_state.find_one(
            {"campaign_id": campaign_id},
            {"updated_at": 1},
            sort=[("updated_at", ASCENDING)],
        )
        return state.get("updated_at") if state else None

    async def mark_campaign_archived(self, campaign_id: str, reason: str) -> None:
        await self.db.campaigns.update_one(
            {"campaign_id": campaign_id},
            {"$set": {"status": "ARCHIVED", "archived_at": utcnow(), "end_reason": reason}},
        )

    async def mark_campaign_ending(self, campaign_id: str, reason: str) -> bool:
        result = await self.db.campaigns.update_one(
            {"campaign_id": campaign_id, "status": {"$in": ["SCHEDULED", "ACTIVE"]}},
            {"$set": {"status": "ENDING", "end_reason": reason, "ending_at": utcnow()}},
        )
        return result.modified_count == 1

    async def mark_retained_campaign_ending(self, campaign_id: str) -> bool:
        """Reopen any archived campaign that still has live state for cleanup."""
        result = await self.db.campaigns.update_one(
            {"campaign_id": campaign_id, "status": "ARCHIVED"},
            {
                "$set": {
                    "status": "ENDING",
                    "delete_on_end": True,
                    "delete_on_next_campaign": False,
                    "end_reason": "retained_posts_cleanup",
                    "ending_at": utcnow(),
                    "updated_at": utcnow(),
                }
            },
        )
        changed = result.modified_count == 1
        if changed:
            await self.db.deliveries.update_many(
                {
                    "campaign_id": campaign_id,
                    "operation": "CLEANUP",
                    "status": DeliveryStatus.CLEANUP_FAILED.value,
                },
                {
                    "$set": {
                        "status": DeliveryStatus.PENDING.value,
                        "attempts": 0,
                        "worker_id": None,
                        "lease_until": None,
                        "next_retry_at": None,
                        "error_category": None,
                        "updated_at": utcnow(),
                    }
                },
            )
        return changed

    async def campaign_send_work_is_quiescent(self, campaign_id: str) -> bool:
        """Ending waits for sends already inside a Telegram request to settle."""
        outstanding = await self.db.deliveries.count_documents(
            {
                "campaign_id": campaign_id,
                "operation": {"$ne": "CLEANUP"},
                "status": DeliveryStatus.PROCESSING.value,
            },
            limit=1,
        )
        return outstanding == 0

    async def cancel_pending_campaign_deliveries(self, campaign_id: str) -> None:
        """Cancel unfinished sends while leaving end-of-campaign cleanup runnable."""
        await self.db.deliveries.update_many(
            {
                "campaign_id": campaign_id,
                "operation": {"$ne": "CLEANUP"},
                "status": {"$in": ["PENDING", "RETRY_WAIT", "PAUSED"]},
            },
            {"$set": {"status": DeliveryStatus.CANCELLED.value, "updated_at": utcnow()}},
        )

    async def pause_campaign_deliveries(self, campaign_id: str) -> int:
        result = await self.db.deliveries.update_many(
            {"campaign_id": campaign_id, "status": {"$in": ["PENDING", "RETRY_WAIT"]}},
            {"$set": {"status": DeliveryStatus.PAUSED.value, "updated_at": utcnow()}},
        )
        return result.modified_count

    async def resume_campaign_deliveries(self, campaign_id: str) -> int:
        result = await self.db.deliveries.update_many(
            {"campaign_id": campaign_id, "status": DeliveryStatus.PAUSED.value},
            {"$set": {"status": DeliveryStatus.PENDING.value, "next_retry_at": None, "updated_at": utcnow()}},
        )
        return result.modified_count

    async def materialize_cleanup_deliveries(self, campaign_id: str) -> int:
        """Create cleanup work and repair orphaned cleanup jobs idempotently.

        ``campaign_channel_state`` is the source of truth for posts that are
        still live.  A prior release accidentally cancelled pending cleanup
        deliveries on the scheduler's next tick.  Existing unique delivery
        records therefore need to be revived as well as missing ones inserted.
        Reconciliation also makes a partially completed deployment/restart
        converge without an owner having to end the campaign again.
        """
        states = await self.live_states(campaign_id)
        now = utcnow()
        existing_rows = await self.db.deliveries.find(
            {"campaign_id": campaign_id, "cycle_number": -1},
            {"_id": 0, "channel_id": 1, "operation": 1, "status": 1},
        ).to_list(None)
        existing = {int(item["channel_id"]): item for item in existing_rows}
        insert_operations = [
            UpdateOne(
                {"campaign_id": campaign_id, "cycle_number": -1, "channel_id": state["channel_id"]},
                {
                    "$setOnInsert": {
                        "campaign_id": campaign_id,
                        "cycle_number": -1,
                        "channel_id": state["channel_id"],
                        "operation": "CLEANUP",
                        "message_ids": state["current_message_ids"],
                        "dispatch_rank": state["channel_id"] & ((1 << 63) - 1),
                        "status": DeliveryStatus.PENDING.value,
                        "attempts": 0,
                        "worker_id": None,
                        "lease_until": None,
                        "next_retry_at": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                },
                upsert=True,
            )
            for state in states
            if int(state["channel_id"]) not in existing
        ]
        for offset in range(0, len(insert_operations), 500):
            await self.db.deliveries.bulk_write(insert_operations[offset : offset + 500], ordered=False)

        # Do not reset active retries or permanent cleanup failures on every
        # scheduler tick.  Only terminal states that contradict a still-live
        # channel pointer are repaired automatically.
        repairable_statuses = [
            DeliveryStatus.CANCELLED.value,
            DeliveryStatus.CLEANED.value,
            DeliveryStatus.PAUSED.value,
        ]
        repair_operations = [
            UpdateOne(
                {
                    "campaign_id": campaign_id,
                    "cycle_number": -1,
                    "channel_id": state["channel_id"],
                    "operation": "CLEANUP",
                    "status": {"$in": repairable_statuses},
                },
                {
                    "$set": {
                        "message_ids": state["current_message_ids"],
                        "status": DeliveryStatus.PENDING.value,
                        "attempts": 0,
                        "worker_id": None,
                        "lease_until": None,
                        "next_retry_at": None,
                        "error_category": None,
                        "error_summary": None,
                        "updated_at": now,
                        "cleanup_recovered_at": now,
                    }
                },
            )
            for state in states
            if existing.get(int(state["channel_id"]), {}).get("operation") == "CLEANUP"
            and existing[int(state["channel_id"])].get("status") in repairable_statuses
        ]
        for offset in range(0, len(repair_operations), 500):
            await self.db.deliveries.bulk_write(repair_operations[offset : offset + 500], ordered=False)
        # A network/rate-limit failure is safe to retry for cleanup, but should
        # not require the owner to discover and tap a button for every outage.
        # Permanent access and deletion-policy failures deliberately remain
        # blocked and visible; retrying them in a hot loop cannot remove posts.
        recovered_retries = await self.recover_retryable_cleanup_failures(campaign_id)
        return len(insert_operations) + len(repair_operations) + recovered_retries

    async def recover_retryable_cleanup_failures(self, campaign_id: str) -> int:
        """Make a bounded delayed retry for exhausted *transient* cleanup work.

        Old versions stopped after a small retry budget and left every such
        job in ``CLEANUP_FAILED`` until a person happened to find the campaign
        screen.  This recovery never touches known permission or Telegram
        deletion-policy failures, and limits each job to four recovery rounds.
        """
        now = utcnow()
        result = await self.db.deliveries.update_many(
            {
                "campaign_id": campaign_id,
                "operation": "CLEANUP",
                "status": DeliveryStatus.CLEANUP_FAILED.value,
                "error_category": "RETRY_EXHAUSTED",
                "$and": [
                    {
                        "$or": [
                            {"cleanup_auto_retry_count": {"$exists": False}},
                            {"cleanup_auto_retry_count": {"$lt": 4}},
                        ]
                    },
                    {
                        "$or": [
                            {"cleanup_auto_retry_at": {"$exists": False}},
                            {"cleanup_auto_retry_at": {"$lte": now}},
                        ]
                    },
                ],
            },
            {
                "$set": {
                    "status": DeliveryStatus.PENDING.value,
                    "attempts": 0,
                    "worker_id": None,
                    "lease_until": None,
                    "next_retry_at": None,
                    "error_category": None,
                    "error_summary": None,
                    "cleanup_auto_recovered_at": now,
                    "updated_at": now,
                },
                "$inc": {"cleanup_auto_retry_count": 1},
            },
        )
        return result.modified_count

    async def recover_interrupted_cleanup_campaigns(self) -> int:
        """Reopen campaigns archived by the cancelled-cleanup regression.

        New ending campaigns are repaired by ``materialize_cleanup_deliveries``.
        This migration path targets only archived campaigns that have both a
        cancelled cleanup delivery and a live-state pointer, so intentionally
        retained campaigns are not reopened.
        """
        campaign_ids = await self.db.deliveries.distinct(
            "campaign_id",
            {"operation": "CLEANUP", "status": DeliveryStatus.CANCELLED.value},
        )
        recovered = 0
        now = utcnow()
        for campaign_id in campaign_ids:
            has_live_state = await self.db.campaign_channel_state.count_documents(
                {"campaign_id": campaign_id},
                limit=1,
            )
            if not has_live_state:
                continue
            result = await self.db.campaigns.update_one(
                {
                    "campaign_id": campaign_id,
                    "status": "ARCHIVED",
                    "delete_on_end": True,
                },
                {
                    "$set": {
                        "status": "ENDING",
                        "archived_at": None,
                        "end_reason": "cleanup_recovery",
                        "ending_at": now,
                        "updated_at": now,
                    }
                },
            )
            recovered += result.modified_count
        return recovered

    async def materialize_superseded_cleanup(self, channel_id: int, current_campaign_id: str) -> int:
        """Queue cleanup for retained posts only on the channel a new campaign reached."""
        states = await self.db.campaign_channel_state.find({"channel_id": channel_id, "campaign_id": {"$ne": current_campaign_id}}).to_list(None)
        queued = 0
        now = utcnow()
        for state in states:
            campaign = await self.db.campaigns.find_one(
                {
                    "campaign_id": state["campaign_id"],
                    "status": "ARCHIVED",
                    "delete_on_end": False,
                    "delete_on_next_campaign": True,
                },
                {"campaign_id": 1},
            )
            if not campaign:
                continue
            key = {"campaign_id": state["campaign_id"], "cycle_number": -1, "channel_id": channel_id}
            document = {
                **key,
                "operation": "CLEANUP",
                "message_ids": state["current_message_ids"],
                "dispatch_rank": channel_id & ((1 << 63) - 1),
                "status": DeliveryStatus.PENDING.value,
                "attempts": 0,
                "worker_id": None,
                "lease_until": None,
                "next_retry_at": None,
                "cleanup_reason": "superseded_by_campaign",
                "superseded_by_campaign_id": current_campaign_id,
                "created_at": now,
                "updated_at": now,
            }
            result = await self.db.deliveries.update_one(key, {"$setOnInsert": document}, upsert=True)
            if result.upserted_id is not None:
                queued += 1
                continue
            retry = await self.db.deliveries.update_one(
                {**key, "status": DeliveryStatus.CLEANUP_FAILED.value},
                {
                    "$set": {
                        "message_ids": state["current_message_ids"],
                        "status": DeliveryStatus.PENDING.value,
                        "attempts": 0,
                        "worker_id": None,
                        "lease_until": None,
                        "next_retry_at": None,
                        "error_category": None,
                        "superseded_by_campaign_id": current_campaign_id,
                        "updated_at": now,
                    }
                },
            )
            queued += retry.modified_count
        return queued

    async def cleanup_is_complete(self, campaign_id: str) -> bool:
        # Never archive while a live-state pointer says a campaign post still
        # exists.  This invariant prevents a cancelled/missing cleanup job from
        # turning into a false-success archive.
        live_posts = await self.db.campaign_channel_state.count_documents(
            {"campaign_id": campaign_id},
            limit=1,
        )
        if live_posts:
            return False
        outstanding = await self.db.deliveries.count_documents(
            {
                "campaign_id": campaign_id,
                "operation": "CLEANUP",
                "status": {"$in": ["PENDING", "PROCESSING", "RETRY_WAIT"]},
            }
        )
        return outstanding == 0

    async def retry_failed_cleanup_deliveries(self, campaign_id: str) -> int:
        """Owner-requested retry for permanent cleanup failures only."""
        now = utcnow()
        result = await self.db.deliveries.update_many(
            {
                "campaign_id": campaign_id,
                "operation": "CLEANUP",
                "status": DeliveryStatus.CLEANUP_FAILED.value,
            },
            {
                "$set": {
                    "status": DeliveryStatus.PENDING.value,
                    "attempts": 0,
                    "worker_id": None,
                    "lease_until": None,
                    "next_retry_at": None,
                    "error_category": None,
                    "error_summary": None,
                    "cleanup_auto_retry_count": 0,
                    "cleanup_auto_retry_at": None,
                    "updated_at": now,
                    "owner_retry_requested_at": now,
                }
            },
        )
        return result.modified_count

    async def delivery_summary(self, campaign_id: str, cycle_number: int) -> Document:
        cursor = await self.db.deliveries.aggregate(
            [
                {"$match": {"campaign_id": campaign_id, "cycle_number": cycle_number}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {item["_id"]: item["count"] for item in rows}

    async def campaign_delivery_totals(self, campaign_id: str) -> Document:
        cursor = await self.db.deliveries.aggregate(
            [
                {"$match": {"campaign_id": campaign_id}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {item["_id"]: item["count"] for item in rows}

    async def cleanup_status_summary(self, campaign_id: str) -> Document:
        cursor = await self.db.deliveries.aggregate(
            [
                {"$match": {"campaign_id": campaign_id, "operation": "CLEANUP"}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {item["_id"]: item["count"] for item in rows}

    async def cleanup_failure_summary(self, campaign_id: str) -> Document:
        cursor = await self.db.deliveries.aggregate(
            [
                {
                    "$match": {
                        "campaign_id": campaign_id,
                        "operation": "CLEANUP",
                        "status": DeliveryStatus.CLEANUP_FAILED.value,
                    }
                },
                {"$group": {"_id": {"$ifNull": ["$error_category", "UNKNOWN"]}, "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {item["_id"]: item["count"] for item in rows}

    async def campaign_cycle_count(self, campaign_id: str) -> int:
        return await self.db.campaign_cycles.count_documents({"campaign_id": campaign_id})

    async def latest_cycle_report(self, campaign_id: str) -> Document | None:
        cycle = await self.db.campaign_cycles.find_one({"campaign_id": campaign_id}, sort=[("cycle_number", -1)])
        if not cycle:
            return None
        cycle["delivery_counts"] = await self.delivery_summary(campaign_id, int(cycle["cycle_number"]))
        return cycle

    async def finish_cycle_if_complete(self, campaign_id: str, cycle_number: int) -> bool:
        if cycle_number < 0:
            return False
        outstanding = await self.db.deliveries.count_documents(
            {
                "campaign_id": campaign_id,
                "cycle_number": cycle_number,
                "status": {
                    "$in": [
                        DeliveryStatus.PENDING.value,
                        DeliveryStatus.PROCESSING.value,
                        DeliveryStatus.RETRY_WAIT.value,
                        DeliveryStatus.PAUSED.value,
                    ]
                },
            },
            limit=1,
        )
        if outstanding:
            return False
        result = await self.db.campaign_cycles.update_one(
            {"campaign_id": campaign_id, "cycle_number": cycle_number, "status": {"$ne": "COMPLETED"}},
            {"$set": {"status": "COMPLETED", "completed_at": utcnow(), "updated_at": utcnow()}},
        )
        return result.modified_count == 1

    async def finish_complete_cycles(self, campaign_id: str) -> int:
        cycle_numbers = await self.db.deliveries.distinct("cycle_number", {"campaign_id": campaign_id, "cycle_number": {"$gte": 0}})
        completed = 0
        for cycle_number in cycle_numbers:
            completed += int(await self.finish_cycle_if_complete(campaign_id, int(cycle_number)))
        return completed

    async def campaign_cycle_stats(self, campaign_id: str) -> Document:
        cursor = await self.db.campaign_cycles.aggregate(
            [
                {"$match": {"campaign_id": campaign_id}},
                {
                    "$group": {
                        "_id": None,
                        "planned": {"$sum": 1},
                        "completed": {"$sum": {"$cond": [{"$eq": ["$status", "COMPLETED"]}, 1, 0]}},
                        "first_started_at": {"$min": "$started_at"},
                        "last_completed_at": {"$max": "$completed_at"},
                    }
                },
            ]
        )
        rows = await cursor.to_list(1)
        return rows[0] if rows else {"planned": 0, "completed": 0}

    async def campaign_delivery_metrics(self, campaign_id: str) -> Document:
        cursor = await self.db.deliveries.aggregate(
            [
                {"$match": {"campaign_id": campaign_id}},
                {
                    "$group": {
                        "_id": None,
                        "attempts": {"$sum": {"$ifNull": ["$attempts", 0]}},
                        "replaced_messages": {"$sum": {"$ifNull": ["$replaced_message_count", 0]}},
                        "cleaned_messages": {"$sum": {"$ifNull": ["$cleaned_message_count", 0]}},
                        "first_created_at": {"$min": "$created_at"},
                        "last_updated_at": {"$max": "$updated_at"},
                    }
                },
            ]
        )
        rows = await cursor.to_list(1)
        return rows[0] if rows else {"attempts": 0, "replaced_messages": 0, "cleaned_messages": 0}

    async def campaign_live_state_count(self, campaign_id: str) -> int:
        return await self.db.campaign_channel_state.count_documents({"campaign_id": campaign_id})

    async def campaign_join_count(self, campaign_id: str) -> int:
        return await self.db.join_events.count_documents({"campaign_id": campaign_id})

    async def failed_deliveries(self, campaign_id: str, cycle_number: int | None = None, *, limit: int = 20) -> list[Document]:
        query: Document = {
            "campaign_id": campaign_id,
            "status": {
                "$in": [
                    DeliveryStatus.FAILED_PERMANENT.value,
                    DeliveryStatus.UNKNOWN_SEND_STATE.value,
                    DeliveryStatus.CLEANUP_FAILED.value,
                ]
            },
        }
        if cycle_number is not None:
            query["cycle_number"] = cycle_number
        return await self.db.deliveries.find(query).sort("updated_at", -1).limit(limit).to_list(limit)

    async def retry_failed_deliveries(self, campaign_id: str, cycle_number: int | None = None) -> int:
        """An owner-initiated retry never touches ambiguous send states."""
        query: Document = {
            "campaign_id": campaign_id,
            "status": DeliveryStatus.FAILED_PERMANENT.value,
        }
        if cycle_number is not None:
            query["cycle_number"] = cycle_number
        affected_cycles = await self.db.deliveries.distinct("cycle_number", query)
        result = await self.db.deliveries.update_many(
            query,
            {
                "$set": {
                    "status": DeliveryStatus.PENDING.value,
                    "next_retry_at": None,
                    "lease_until": None,
                    "worker_id": None,
                    "updated_at": utcnow(),
                    "owner_retry_requested_at": utcnow(),
                }
            },
        )
        if result.modified_count and affected_cycles:
            await self.db.campaign_cycles.update_many(
                {"campaign_id": campaign_id, "cycle_number": {"$in": affected_cycles}},
                {"$set": {"status": "RUNNING", "completed_at": None, "updated_at": utcnow()}},
            )
        return result.modified_count

    async def export_collections(self, full: bool) -> dict[str, list[Document]]:
        names = ["channels", "campaigns", "settings"]
        if full:
            names.extend(["campaign_cycles", "deliveries", "campaign_channel_state", "join_events"])
        return {name: await self.db[name].find({}).to_list(None) for name in names}

    async def set_owner_session(self, owner_id: int, state: Document, *, ttl_minutes: int = 30) -> None:
        now = utcnow()
        await self.db.owner_sessions.update_one(
            {"owner_id": owner_id},
            {
                "$set": {
                    "owner_id": owner_id,
                    "state": state,
                    "updated_at": now,
                    "expires_at": now + timedelta(minutes=ttl_minutes),
                }
            },
            upsert=True,
        )

    async def owner_session(self, owner_id: int) -> Document | None:
        document = await self.db.owner_sessions.find_one({"owner_id": owner_id, "expires_at": {"$gt": utcnow()}})
        return document.get("state") if document else None

    async def clear_owner_session(self, owner_id: int) -> None:
        await self.db.owner_sessions.delete_one({"owner_id": owner_id})

    async def list_campaigns(self, limit: int = 20, *, skip: int = 0) -> list[Document]:
        return await self.db.campaigns.find({}).sort("updated_at", -1).skip(skip).limit(limit).to_list(limit)

    async def campaign_status_counts(self) -> Document:
        cursor = await self.db.campaigns.aggregate(
            [
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {row["_id"]: row["count"] for row in rows if row.get("_id")}

    async def get_setting(self, key: str, default: Any = None) -> Any:
        document = await self.db.settings.find_one({"key": key})
        return document.get("value", default) if document else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.settings.update_one(
            {"key": key},
            {"$set": {"key": key, "value": value, "updated_at": utcnow()}},
            upsert=True,
        )

    async def delete_draft_campaign(self, campaign_id: str) -> bool:
        result = await self.db.campaigns.delete_one({"campaign_id": campaign_id, "status": "DRAFT"})
        if result.deleted_count:
            await self.db.variant_shares.delete_many({"campaign_id": campaign_id})
        return result.deleted_count == 1

    async def delete_archived_campaign(self, campaign_id: str) -> bool:
        """Permanently remove finished history only when no channel post is live.

        Children are removed before the campaign definition so an interrupted
        operation remains visible and can safely be retried. Archived campaigns
        cannot create new deliveries or live state.
        """
        campaign = await self.db.campaigns.find_one({"campaign_id": campaign_id, "status": "ARCHIVED"})
        if not campaign:
            return False
        live_count = await self.db.campaign_channel_state.count_documents({"campaign_id": campaign_id})
        if live_count:
            raise ValueError(f"this campaign still tracks {live_count} live post{'s' if live_count != 1 else ''}; delete retained posts first")

        campaign_query = {"campaign_id": campaign_id}
        await self.db.campaign_cycles.delete_many(campaign_query)
        await self.db.deliveries.delete_many(campaign_query)
        await self.db.join_events.delete_many(campaign_query)
        # This should already be empty due to the guard, but deleting it keeps
        # retries idempotent if legacy data contains empty-state artifacts.
        await self.db.campaign_channel_state.delete_many(campaign_query)
        result = await self.db.campaigns.delete_one({"campaign_id": campaign_id, "status": "ARCHIVED"})
        if result.deleted_count:
            await self.db.variant_shares.delete_many(campaign_query)
        return result.deleted_count == 1

    async def save_pending_restore(self, restore_id: str, owner_id: int, backup: Document) -> None:
        await self.db.pending_restores.update_one(
            {"restore_id": restore_id},
            {
                "$set": {
                    "restore_id": restore_id,
                    "owner_id": owner_id,
                    "backup": backup,
                    "expires_at": utcnow() + timedelta(hours=1),
                }
            },
            upsert=True,
        )

    async def get_pending_restore(self, restore_id: str, owner_id: int) -> Document | None:
        return await self.db.pending_restores.find_one({"restore_id": restore_id, "owner_id": owner_id})

    async def delete_pending_restore(self, restore_id: str, owner_id: int) -> None:
        await self.db.pending_restores.delete_one({"restore_id": restore_id, "owner_id": owner_id})

    async def register_update(self, update_id: int) -> bool:
        try:
            now = utcnow()
            await self.db.processed_updates.insert_one({"update_id": update_id, "received_at": now, "expires_at": now + timedelta(days=7)})
            return True
        except DuplicateKeyError:
            return False

    async def unregister_update(self, update_id: int) -> None:
        """Allow Telegram to retry an update whose handler did not complete."""
        await self.db.processed_updates.delete_one({"update_id": update_id})

    async def restore_collection(self, name: str, documents: list[Document]) -> tuple[int, int]:
        restored = skipped = 0
        unique_key = {"channels": "telegram_chat_id", "campaigns": "campaign_id", "settings": "key"}.get(name)
        for document in documents:
            document.pop("_id", None)
            if not unique_key or unique_key not in document:
                skipped += 1
                continue
            result = await self.db[name].update_one({unique_key: document[unique_key]}, {"$set": document}, upsert=True)
            restored += 1 if result.acknowledged else 0
        return restored, skipped
