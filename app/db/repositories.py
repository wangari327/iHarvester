"""Explicit Mongo repositories: no ORM and no ephemeral delivery queue."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import ASCENDING, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from app.campaigns.models import ChannelStatus, DeliveryStatus
from app.campaigns.sharing import new_share_code
from app.db.client import Database
from app.utils.time import as_utc, utcnow

Document = dict[str, Any]
VIEW_MATURITY = timedelta(hours=6)
MIN_PUBLIC_VIEW_PLACEMENTS = 50


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

    @staticmethod
    def _channel_visibility_query(is_public: bool) -> Document:
        """Include legacy rows that predate the explicit is_public field."""
        if is_public:
            return {
                "$or": [
                    {"is_public": True},
                    {"is_public": {"$exists": False}, "username": {"$type": "string", "$ne": ""}},
                ]
            }
        return {
            "$or": [
                {"is_public": False},
                {"is_public": {"$exists": False}, "username": None},
                {"is_public": {"$exists": False}, "username": {"$exists": False}},
            ]
        }

    async def ranked_channels_by_members(
        self,
        *,
        is_public: bool,
        skip: int = 0,
        limit: int = 10,
    ) -> list[Document]:
        """Return a complete public/private ranking, with unknown counts last."""
        safe_skip = max(0, int(skip))
        safe_limit = max(1, min(int(limit), 20))
        return await self.db.channels.find(self._channel_visibility_query(is_public)).sort(
            [("member_count", -1), ("title", ASCENDING)]
        ).skip(safe_skip).limit(safe_limit).to_list(safe_limit)

    async def ranked_channel_count(self, *, is_public: bool) -> int:
        return await self.db.channels.count_documents(self._channel_visibility_query(is_public))

    async def channel_member_count_coverage(self, is_public: bool | None = None) -> tuple[int, int]:
        visibility = self._channel_visibility_query(is_public) if is_public is not None else {}
        known_query: Document = {"member_count": {"$type": "number"}}
        if visibility:
            known_query = {"$and": [visibility, known_query]}
        known = await self.db.channels.count_documents(known_query)
        total = await self.db.channels.count_documents(visibility)
        return known, max(0, total - known)

    async def channel_visibility_counts(self) -> Document:
        public = await self.ranked_channel_count(is_public=True)
        private = await self.ranked_channel_count(is_public=False)
        return {"public": public, "private": private}

    async def channel_ids_by_status(self, status: str) -> list[int]:
        rows = await self.db.channels.find(
            {"status": status},
            {"_id": 0, "telegram_chat_id": 1},
        ).to_list(None)
        return [int(row["telegram_chat_id"]) for row in rows]

    async def start_network_refresh(self, owner_id: int) -> tuple[Document, bool]:
        """Create one durable full-registry refresh, or return the active run.

        Member counts and bot permissions change outside Telegram update events.
        The work is consequently queued one channel at a time and can survive
        a deployment restart without losing the operator's request.
        """
        if active := await self.db.network_refresh_runs.find_one({"status": {"$in": ["QUEUED", "RUNNING"]}}):
            return active, False
        rows = await self.db.channels.find({}, {"_id": 0, "telegram_chat_id": 1}).to_list(None)
        now = utcnow()
        run: Document = {
            "refresh_id": f"netrefresh_{secrets.token_urlsafe(9)}",
            "owner_id": owner_id,
            "status": "QUEUED" if rows else "COMPLETED",
            "total": len(rows),
            "requested_at": now,
            "started_at": None,
            "completed_at": now if not rows else None,
            "updated_at": now,
        }
        await self.db.network_refresh_runs.insert_one(run)
        operations = [
            UpdateOne(
                {"refresh_id": run["refresh_id"], "channel_id": int(row["telegram_chat_id"])},
                {
                    "$setOnInsert": {
                        "refresh_id": run["refresh_id"],
                        "channel_id": int(row["telegram_chat_id"]),
                        "status": "PENDING",
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
            for row in rows
        ]
        for offset in range(0, len(operations), 500):
            await self.db.network_refresh_jobs.bulk_write(operations[offset : offset + 500], ordered=False)
        return run, True

    async def latest_network_refresh(self) -> Document | None:
        return await self.db.network_refresh_runs.find_one({}, sort=[("requested_at", -1)])

    async def network_refresh_summary(self, refresh_id: str) -> Document:
        cursor = await self.db.network_refresh_jobs.aggregate(
            [
                {"$match": {"refresh_id": refresh_id}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {str(row["_id"]): int(row["count"]) for row in rows}

    async def claim_network_refresh_job(self, worker_id: str, lease_seconds: int) -> Document | None:
        now = utcnow()
        job = await self.db.network_refresh_jobs.find_one_and_update(
            {
                "$or": [
                    {"status": "PENDING"},
                    {"status": "RETRY_WAIT", "next_retry_at": {"$lte": now}},
                    {"status": "PROCESSING", "lease_until": {"$lte": now}},
                ]
            },
            {
                "$set": {
                    "status": "PROCESSING",
                    "worker_id": worker_id,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        if job:
            await self.db.network_refresh_runs.update_one(
                {"refresh_id": job["refresh_id"], "status": "QUEUED"},
                {"$set": {"status": "RUNNING", "started_at": now, "updated_at": now}},
            )
        return job

    async def complete_network_refresh_job(self, job_id: Any, status: str, **details: Any) -> None:
        terminal = status in {"COMPLETED", "FAILED"}
        job = await self.db.network_refresh_jobs.find_one_and_update(
            {"_id": job_id},
            {
                "$set": {
                    "status": status,
                    "lease_until": None,
                    "updated_at": utcnow(),
                    **({"finished_at": utcnow()} if terminal else {}),
                    **details,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if not job:
            return
        outstanding = await self.db.network_refresh_jobs.count_documents(
            {"refresh_id": job["refresh_id"], "status": {"$in": ["PENDING", "PROCESSING", "RETRY_WAIT"]}},
            limit=1,
        )
        if not outstanding:
            await self.db.network_refresh_runs.update_one(
                {"refresh_id": job["refresh_id"]},
                {"$set": {"status": "COMPLETED", "completed_at": utcnow(), "updated_at": utcnow()}},
            )

    async def retry_network_refresh_job(self, job_id: Any, retry_after_seconds: float, **details: Any) -> None:
        now = utcnow()
        await self.db.network_refresh_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "RETRY_WAIT",
                    "lease_until": None,
                    "next_retry_at": now + timedelta(seconds=retry_after_seconds),
                    "updated_at": now,
                    **details,
                }
            },
        )

    async def create_bot_fleet_job(
        self,
        *,
        owner_id: int,
        channel_id: int,
        bot_usernames: tuple[str, ...],
    ) -> tuple[Document, bool]:
        """Queue one owner-approved fleet promotion without duplicate runs.

        The web bot merely validates that iHarvester already administers the
        registered channel.  A separate private MTProto worker later selects
        an authorised *human* account which has that exact channel in its own
        Telegram directory and can add administrators.
        """
        channel = await self.get_channel(channel_id)
        if not channel or not bool(channel.get("permissions", {}).get("is_admin")):
            raise ValueError("iHarvester must already be an administrator of that registered channel")
        if not bot_usernames:
            raise ValueError("the approved bot fleet is empty")
        existing = await self.db.bot_fleet_jobs.find_one(
            {
                "owner_id": owner_id,
                "channel_id": channel_id,
                "status": {"$in": ["QUEUED", "RUNNING", "CANCEL_REQUESTED"]},
            },
            sort=[("requested_at", -1)],
        )
        if existing:
            return existing, False
        now = utcnow()
        job: Document = {
            "job_id": f"fleet_{secrets.token_urlsafe(9)}",
            "owner_id": owner_id,
            "channel_id": channel_id,
            "channel_title": str(channel.get("title") or channel_id),
            "channel_username": channel.get("username"),
            "bot_usernames": list(bot_usernames),
            "results": [
                {
                    "username": username,
                    "status": "PENDING",
                    "account_label": None,
                    "error": None,
                    "note": None,
                    "updated_at": now,
                }
                for username in bot_usernames
            ],
            "status": "QUEUED",
            "requested_at": now,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "worker_id": None,
            "lease_until": None,
        }
        try:
            await self.db.bot_fleet_jobs.insert_one(job)
        except DuplicateKeyError:
            # The unique ID collision is extraordinarily unlikely.  Retrying
            # once keeps a callback idempotent without creating a second job.
            job["job_id"] = f"fleet_{secrets.token_urlsafe(12)}"
            await self.db.bot_fleet_jobs.insert_one(job)
        return job, True

    async def get_bot_fleet_job(self, job_id: str, owner_id: int) -> Document | None:
        return await self.db.bot_fleet_jobs.find_one({"job_id": job_id, "owner_id": owner_id})

    async def list_bot_fleet_jobs(self, owner_id: int, *, skip: int = 0, limit: int = 6) -> list[Document]:
        safe_skip = max(0, int(skip))
        safe_limit = max(1, min(int(limit), 10))
        return await self.db.bot_fleet_jobs.find({"owner_id": owner_id}).sort(
            "requested_at", -1
        ).skip(safe_skip).limit(safe_limit).to_list(safe_limit)

    async def cancel_bot_fleet_job(self, job_id: str, owner_id: int) -> Document | None:
        """Cancel a queued job immediately or ask a running worker to stop."""
        now = utcnow()
        queued = await self.db.bot_fleet_jobs.find_one_and_update(
            {"job_id": job_id, "owner_id": owner_id, "status": "QUEUED"},
            {
                "$set": {
                    "status": "CANCELLED",
                    "completed_at": now,
                    "updated_at": now,
                    "lease_until": None,
                    "results.$[].status": "CANCELLED",
                    "results.$[].updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if queued:
            return queued
        return await self.db.bot_fleet_jobs.find_one_and_update(
            {"job_id": job_id, "owner_id": owner_id, "status": "RUNNING"},
            {"$set": {"status": "CANCEL_REQUESTED", "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )

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

    async def rebase_unstarted_scheduled_campaign(
        self,
        campaign_id: str,
        expected_start: Any,
        update: Document,
    ) -> Document | None:
        """Recover a schedule whose entire window elapsed while the host slept."""
        return await self.db.campaigns.find_one_and_update(
            {
                "campaign_id": campaign_id,
                "status": "SCHEDULED",
                "start_at_utc": expected_start,
                "next_cycle_number": 0,
            },
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

    async def has_unfinished_send_work(self, campaign_id: str) -> bool:
        """Whether an earlier non-cleanup cycle still owns send work.

        A campaign may overlap other campaigns, but not itself: a later cycle
        replaces the prior cycle's posts and must wait for it to settle.
        """
        return bool(
            await self.db.deliveries.count_documents(
                {
                    "campaign_id": campaign_id,
                    "operation": {"$ne": "CLEANUP"},
                    "status": {"$in": ["PENDING", "PROCESSING", "RETRY_WAIT", "PAUSED"]},
                },
                limit=1,
            )
        )

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
        posted_at = utcnow()
        await self.db.campaign_channel_state.update_one(
            {"campaign_id": campaign_id, "channel_id": channel_id},
            {
                "$set": {
                    "current_message_ids": message_ids,
                    "current_cycle_number": cycle_number,
                    "current_variant_index": variant_index,
                    # Kept on the existing bounded live-state record so the
                    # optional view sampler can calculate an honest view rate
                    # without retaining a second delivery analytics history.
                    "posted_at": posted_at,
                    "updated_at": posted_at,
                },
                # A replacement post is a fresh observation target. Never
                # accidentally show the previous post's counter on it.
                "$unset": {
                    "view_count": "",
                    "view_observed_at": "",
                    "view_first_observed_at": "",
                    "view_tracker_session": "",
                    "view_finalized_at": "",
                },
            },
            upsert=True,
        )

    async def finalize_live_state_views(self, campaign_id: str, channel_id: int, expected_message_ids: list[int]) -> int:
        """Preserve one sampled post counter before its live-state is removed.

        The campaign-level total is bounded data, while the per-placement
        state is removed as normal after a repost or cleanup. Marking the
        state first makes duplicate worker retries harmless; a database outage
        can only omit a final sample, never inflate a client's reported views.
        """
        message_ids = [int(value) for value in expected_message_ids if int(value) > 0]
        if not message_ids:
            return 0
        now = utcnow()
        state = await self.db.campaign_channel_state.find_one_and_update(
            {
                "campaign_id": campaign_id,
                "channel_id": channel_id,
                "current_message_ids": message_ids,
                "view_count": {"$type": "number"},
                "view_finalized_at": {"$exists": False},
            },
            {"$set": {"view_finalized_at": now}},
            return_document=ReturnDocument.BEFORE,
        )
        if not state:
            return 0
        views = max(0, int(state.get("view_count") or 0))
        observed_at = as_utc(state.get("view_observed_at")) or now
        posted_at = as_utc(state.get("posted_at"))
        mature = bool(posted_at and observed_at - posted_at >= VIEW_MATURITY)
        increment: Document = {
            "view_tracking.finalized_views": views,
            "view_tracking.finalized_placements": 1,
        }
        if mature:
            elapsed_hours = max(0.0, (observed_at - posted_at).total_seconds() / 3600)
            increment.update(
                {
                    "view_tracking.finalized_mature_views": views,
                    "view_tracking.finalized_mature_placements": 1,
                    "view_tracking.finalized_mature_placement_hours": round(elapsed_hours, 3),
                }
            )
        await self.db.campaigns.update_one(
            {"campaign_id": campaign_id},
            {
                "$inc": increment,
                "$min": {"view_tracking.first_sample_at": observed_at},
                "$max": {"view_tracking.last_sample_at": observed_at},
            },
        )
        return views

    async def clear_live_state(self, campaign_id: str, channel_id: int) -> None:
        await self.db.campaign_channel_state.delete_one({"campaign_id": campaign_id, "channel_id": channel_id})

    async def clear_campaign_live_states(self, campaign_id: str) -> None:
        await self.db.campaign_channel_state.delete_many({"campaign_id": campaign_id})

    async def live_states(self, campaign_id: str) -> list[Document]:
        return await self.db.campaign_channel_state.find({"campaign_id": campaign_id}).to_list(None)

    async def queue_live_text_repairs(
        self,
        *,
        campaign_id: str,
        repair_id: str,
        variant_id: str,
        variant_index: int,
        variant_revision: int,
        creative: Document,
        states: list[Document],
    ) -> int:
        """Durably queue an in-place text repair for the matching live posts.

        A repair records the exact message IDs and creative snapshot that were
        current when the owner confirmed it.  The worker re-checks that live
        pointer before editing, so a later repost can never be overwritten by
        an older repair job.
        """
        now = utcnow()
        operations: list[UpdateOne] = []
        for state in states:
            if int(state.get("current_variant_index", -1)) != variant_index:
                continue
            message_ids = [int(item) for item in state.get("current_message_ids", [])]
            if not message_ids:
                continue
            channel_id = int(state["channel_id"])
            document: Document = {
                "campaign_id": campaign_id,
                "repair_id": repair_id,
                "channel_id": channel_id,
                "variant_id": variant_id,
                "variant_index": variant_index,
                "variant_revision": variant_revision,
                "message_ids": message_ids,
                "creative": creative,
                "status": "PENDING",
                "attempts": 0,
                "worker_id": None,
                "lease_until": None,
                "next_retry_at": None,
                "dispatch_rank": secrets.randbelow(2_000_000_000),
                "created_at": now,
                "updated_at": now,
            }
            operations.append(
                UpdateOne(
                    {"campaign_id": campaign_id, "repair_id": repair_id, "channel_id": channel_id},
                    {"$setOnInsert": document},
                    upsert=True,
                )
            )
        if not operations:
            return 0
        for offset in range(0, len(operations), 500):
            await self.db.live_text_repairs.bulk_write(operations[offset : offset + 500], ordered=False)
        return len(operations)

    async def claim_live_text_repair(self, worker_id: str, lease_seconds: int) -> Document | None:
        now = utcnow()
        return await self.db.live_text_repairs.find_one_and_update(
            {
                "$or": [
                    {"status": "PENDING"},
                    {"status": "RETRY_WAIT", "next_retry_at": {"$lte": now}},
                    {"status": "PROCESSING", "lease_until": {"$lte": now}},
                ]
            },
            {
                "$set": {
                    "status": "PROCESSING",
                    "worker_id": worker_id,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("dispatch_rank", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    async def complete_live_text_repair(self, repair_id: Any, status: str, **details: Any) -> None:
        terminal = status in {"SUCCEEDED", "SKIPPED", "FAILED"}
        await self.db.live_text_repairs.update_one(
            {"_id": repair_id},
            {
                "$set": {
                    "status": status,
                    "lease_until": None,
                    "updated_at": utcnow(),
                    **({"finished_at": utcnow()} if terminal else {}),
                    **details,
                }
            },
        )

    async def retry_live_text_repair(self, repair_id: Any, retry_after_seconds: float, **details: Any) -> None:
        now = utcnow()
        await self.db.live_text_repairs.update_one(
            {"_id": repair_id},
            {
                "$set": {
                    "status": "RETRY_WAIT",
                    "lease_until": None,
                    "next_retry_at": now + timedelta(seconds=retry_after_seconds),
                    "updated_at": now,
                    **details,
                }
            },
        )

    async def live_text_repair_summary(self, campaign_id: str, repair_id: str) -> Document:
        cursor = await self.db.live_text_repairs.aggregate(
            [
                {"$match": {"campaign_id": campaign_id, "repair_id": repair_id}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {str(row["_id"]): int(row["count"]) for row in rows}

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

    async def _history_rollup(self, campaign_id: str) -> Document:
        """Return compacted/offline-history counters, if this campaign has any."""
        campaigns = getattr(self.db, "campaigns", None)
        if campaigns is None or not hasattr(campaigns, "find_one"):
            return {}
        campaign = await campaigns.find_one({"campaign_id": campaign_id}, {"history_rollup": 1})
        return dict(campaign.get("history_rollup", {})) if campaign else {}

    @staticmethod
    def _merge_counts(current: Document, archived: Document) -> Document:
        merged = dict(current)
        for status, count in archived.items():
            merged[status] = int(merged.get(status, 0)) + int(count or 0)
        return merged

    async def campaign_delivery_totals(self, campaign_id: str) -> Document:
        cursor = await self.db.deliveries.aggregate(
            [
                {"$match": {"campaign_id": campaign_id}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        current = {item["_id"]: item["count"] for item in rows}
        rollup = await self._history_rollup(campaign_id)
        return self._merge_counts(
            current,
            self._merge_counts(rollup.get("delivery_totals", {}), rollup.get("cleanup_totals", {})),
        )

    async def cleanup_status_summary(self, campaign_id: str) -> Document:
        cursor = await self.db.deliveries.aggregate(
            [
                {"$match": {"campaign_id": campaign_id, "operation": "CLEANUP"}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        current = {item["_id"]: item["count"] for item in rows}
        return self._merge_counts(current, (await self._history_rollup(campaign_id)).get("cleanup_totals", {}))

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
        current = {item["_id"]: item["count"] for item in rows}
        return self._merge_counts(current, (await self._history_rollup(campaign_id)).get("cleanup_failure_totals", {}))

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
        current = rows[0] if rows else {"attempts": 0, "replaced_messages": 0, "cleaned_messages": 0}
        archived = (await self._history_rollup(campaign_id)).get("metrics", {})
        result = dict(current)
        for key in ("attempts", "replaced_messages", "cleaned_messages"):
            result[key] = int(current.get(key, 0) or 0) + int(archived.get(key, 0) or 0)
        first, last = archived.get("first_created_at"), archived.get("last_updated_at")
        if first and (not result.get("first_created_at") or first < result["first_created_at"]):
            result["first_created_at"] = first
        if last and (not result.get("last_updated_at") or last > result["last_updated_at"]):
            result["last_updated_at"] = last
        return result

    async def campaign_live_state_count(self, campaign_id: str) -> int:
        return await self.db.campaign_channel_state.count_documents({"campaign_id": campaign_id})

    async def campaign_join_count(self, campaign_id: str) -> int:
        current = await self.db.join_events.count_documents({"campaign_id": campaign_id})
        return current + int((await self._history_rollup(campaign_id)).get("join_count", 0) or 0)

    async def campaign_audience_summary(self, campaign: Document) -> Document:
        """Summarise the frozen campaign audience without exposing channel rows.

        Subscriber counts are channel subscriptions, not a deduplicated count
        of people.  That distinction is kept in the payload so client-facing
        pages never overstate a campaign's probable reach.
        """
        target_ids = {int(channel_id) for channel_id in campaign.get("target_snapshot", [])}
        if not target_ids:
            return {
                "target_channels": 0,
                "known_audience_channels": 0,
                "subscriber_slots": 0,
                "unknown_audience_channels": 0,
                "public_channels": 0,
            }
        rows = await self.db.channels.find(
            {"telegram_chat_id": {"$in": list(target_ids)}},
            {"telegram_chat_id": 1, "member_count": 1, "username": 1},
        ).to_list(None)
        known = 0
        subscriber_slots = 0
        public_channels = 0
        for row in rows:
            audience = row.get("member_count")
            if isinstance(audience, (int, float)) and not isinstance(audience, bool) and audience >= 0:
                known += 1
                subscriber_slots += int(audience)
            if isinstance(row.get("username"), str) and row["username"].strip():
                public_channels += 1
        return {
            "target_channels": len(target_ids),
            "known_audience_channels": known,
            "subscriber_slots": subscriber_slots,
            "unknown_audience_channels": max(0, len(target_ids) - known),
            "public_channels": public_channels,
        }

    async def network_audience_summary(self) -> Document:
        """Return the active network's public aggregate inventory only."""
        rows = await self.db.channels.find(
            self._active_channel_query(),
            {"member_count": 1, "username": 1},
        ).to_list(None)
        known = 0
        subscriber_slots = 0
        public_channels = 0
        for row in rows:
            audience = row.get("member_count")
            if isinstance(audience, (int, float)) and not isinstance(audience, bool) and audience >= 0:
                known += 1
                subscriber_slots += int(audience)
            if isinstance(row.get("username"), str) and row["username"].strip():
                public_channels += 1
        return {
            "active_channels": len(rows),
            "known_audience_channels": known,
            "subscriber_slots": subscriber_slots,
            "unknown_audience_channels": max(0, len(rows) - known),
            "public_channels": public_channels,
        }

    async def network_public_placements(self, *, page: int = 0, limit: int = 24) -> Document:
        """Return the active, publicly browseable part of the network.

        The public sales page may link only to channels that intentionally
        expose a username.  Private channels remain part of aggregate
        inventory figures but are never disclosed through this endpoint.
        """
        safe_limit = max(1, min(int(limit), 60))
        safe_page = max(0, int(page))
        query: Document = {
            "$and": [
                self._active_channel_query(),
                self._channel_visibility_query(True),
                {"username": {"$type": "string", "$ne": ""}},
            ]
        }
        total = await self.db.channels.count_documents(query)
        rows = await self.db.channels.find(
            query,
            {"title": 1, "username": 1, "member_count": 1},
        ).sort(
            [("member_count", -1), ("title", ASCENDING)]
        ).skip(safe_page * safe_limit).limit(safe_limit).to_list(safe_limit)
        items: list[Document] = []
        for row in rows:
            username = row.get("username")
            if not isinstance(username, str) or not username.strip():
                continue
            members = row.get("member_count")
            items.append(
                {
                    "label": str(row.get("title") or f"@{username}").strip()[:120],
                    "url": f"https://t.me/{username.strip()}",
                    "subscriber_slots": int(members)
                    if isinstance(members, (int, float)) and not isinstance(members, bool) and members >= 0
                    else None,
                }
            )
        return {"items": items, "total": total, "page": safe_page, "page_size": safe_limit}

    async def campaign_public_placements(self, campaign_id: str, *, page: int = 0, limit: int = 24) -> Document:
        """Provide inspectable links for public placements only.

        Private channels intentionally remain aggregate-only on bearer client
        links.  A public username and an exact, currently live message ID are
        both required before an external link is returned.
        """
        safe_limit = max(1, min(int(limit), 60))
        safe_page = max(0, int(page))
        states = await self.db.campaign_channel_state.find(
            {"campaign_id": campaign_id},
            {"channel_id": 1, "current_message_ids": 1, "posted_at": 1, "view_count": 1, "view_observed_at": 1},
        ).sort("updated_at", -1).to_list(None)
        channel_ids = {int(row["channel_id"]) for row in states if row.get("channel_id") is not None}
        if not channel_ids:
            return {"items": [], "total": 0, "page": safe_page, "page_size": safe_limit}
        channels = await self.db.channels.find(
            {"telegram_chat_id": {"$in": list(channel_ids)}},
            {"telegram_chat_id": 1, "title": 1, "username": 1, "member_count": 1},
        ).to_list(None)
        by_id = {int(channel["telegram_chat_id"]): channel for channel in channels}
        all_items: list[Document] = []
        for state in states:
            channel = by_id.get(int(state["channel_id"]))
            username = channel.get("username") if channel else None
            message_ids = [int(message_id) for message_id in state.get("current_message_ids", []) if int(message_id) > 0]
            if not isinstance(username, str) or not username.strip() or not message_ids:
                continue
            label = str(channel.get("title") or f"@{username}").strip()
            member_count = channel.get("member_count")
            views = state.get("view_count")
            posted_at = state.get("posted_at")
            observed_at = state.get("view_observed_at")
            mature_view = (
                int(views)
                if isinstance(views, (int, float))
                and not isinstance(views, bool)
                and views >= 0
                and isinstance(posted_at, datetime)
                and isinstance(observed_at, datetime)
                and as_utc(observed_at) - as_utc(posted_at) >= VIEW_MATURITY
                else None
            )
            all_items.append(
                {
                    "label": label[:120],
                    "url": f"https://t.me/{username.strip()}/{message_ids[0]}",
                    "subscriber_slots": int(member_count)
                    if isinstance(member_count, (int, float)) and not isinstance(member_count, bool) and member_count >= 0
                    else None,
                    # A post is a fair highlight only after it has had enough
                    # time to gather views.  Fresh posts are still browseable
                    # below, but never labelled as "best performing".
                    "verified_post_views": mature_view,
                    "view_observed_at": as_utc(observed_at).isoformat() if mature_view is not None else None,
                }
            )
        # Larger public placements appear first. Unknown subscriber counts are
        # intentionally placed after verified counts, never treated as zero.
        all_items.sort(
            key=lambda item: (
                item["subscriber_slots"] is None,
                -int(item["subscriber_slots"] or 0),
                str(item["label"]).casefold(),
            )
        )
        measured = [item for item in all_items if item["verified_post_views"] is not None]
        measured.sort(
            key=lambda item: (
                -int(item["verified_post_views"] or 0),
                item["subscriber_slots"] is None,
                -int(item["subscriber_slots"] or 0),
                str(item["label"]).casefold(),
            )
        )
        highlights = measured[:3] if measured else all_items[:3]
        start = safe_page * safe_limit
        return {
            "items": all_items[start : start + safe_limit],
            "total": len(all_items),
            "page": safe_page,
            "page_size": safe_limit,
            "highlights": {
                "items": highlights,
                "kind": "views" if measured else "audience",
                "available": len(measured),
            },
        }

    @staticmethod
    def _view_tracking_payload(tracking: Document | None) -> Document:
        values = dict(tracking or {})
        live_accumulator = any(
            key in values
            for key in (
                "current_views",
                "finalized_views",
                "current_sampled_placements",
                "finalized_placements",
            )
        )
        current_views = max(0, int(values.get("current_views") or 0))
        finalized_views = max(0, int(values.get("finalized_views") or 0))
        current_placements = max(0, int(values.get("current_sampled_placements") or 0))
        finalized_placements = max(0, int(values.get("finalized_placements") or 0))
        current_mature_views = max(0, int(values.get("current_mature_views") or 0))
        finalized_mature_views = max(0, int(values.get("finalized_mature_views") or 0))
        current_mature_placements = max(0, int(values.get("current_mature_placements") or 0))
        finalized_mature_placements = max(0, int(values.get("finalized_mature_placements") or 0))
        current_mature_hours = max(0.0, float(values.get("current_mature_placement_hours") or 0))
        finalized_mature_hours = max(0.0, float(values.get("finalized_mature_placement_hours") or 0))
        total_views = finalized_views + current_views if live_accumulator else max(0, int(values.get("total_views") or 0))
        sampled_placements = finalized_placements + current_placements if live_accumulator else max(0, int(values.get("sampled_placements") or 0))
        mature_views = finalized_mature_views + current_mature_views if live_accumulator else max(0, int(values.get("mature_views") or 0))
        mature_placements = (
            finalized_mature_placements + current_mature_placements
            if live_accumulator
            else max(0, int(values.get("mature_placements") or 0))
        )
        mature_hours = (
            finalized_mature_hours + current_mature_hours
            if live_accumulator
            else max(0.0, float(values.get("mature_placement_hours") or 0))
        )
        return {
            "total_views": total_views,
            "current_views": current_views,
            "sampled_placements": sampled_placements,
            "finalized_placements": finalized_placements,
            "mature_views": mature_views,
            "mature_placements": mature_placements,
            "mature_placement_hours": mature_hours,
            "sampled_campaigns": max(0, int(values.get("sampled_campaigns") or 0)),
            "views_per_placement": round(total_views / sampled_placements) if sampled_placements else 0,
            "views_per_placement_hour": round(mature_views / mature_hours, 3) if mature_hours else 0,
            "last_sample_at": values.get("last_sample_at"),
            "first_sample_at": values.get("first_sample_at"),
            "placement_hours": max(0.0, float(values.get("placement_hours") or 0)),
        }

    async def campaign_view_summary(self, campaign: Document) -> Document:
        """Return the compact view snapshot retained on the campaign itself."""
        return self._view_tracking_payload(campaign.get("view_tracking"))

    async def promotion_view_benchmarks(self, *, limit: int = 500) -> Document:
        """Build conservative historical benchmarks from verified view snapshots.

        No metric is returned as a forecast until at least one campaign has a
        MTProto view sample.  The hourly values are based on the campaign's
        selected start hour in its owner timezone, and are labelled with their
        sample counts by the web layer.
        """
        safe_limit = max(1, min(int(limit), 2_000))
        rows = await self.db.campaigns.find(
            {
                "$or": [
                    {"view_tracking.mature_placements": {"$gte": MIN_PUBLIC_VIEW_PLACEMENTS}},
                    {"view_tracking.current_mature_placements": {"$gte": 1}},
                    {"view_tracking.finalized_mature_placements": {"$gte": 1}},
                ]
            },
            {"view_tracking": 1, "start_at_utc": 1, "owner_timezone": 1},
        ).sort("updated_at", -1).limit(safe_limit).to_list(safe_limit)
        per_campaign: list[float] = []
        per_hour: dict[int, list[float]] = {hour: [] for hour in range(24)}
        sampled_placements = 0
        verified_views = 0
        for row in rows:
            tracking = self._view_tracking_payload(row.get("view_tracking"))
            placements = tracking["mature_placements"]
            views = tracking["mature_views"]
            if placements < MIN_PUBLIC_VIEW_PLACEMENTS or views < 0:
                continue
            per_campaign.append(views / placements)
            sampled_placements += placements
            verified_views += views
            start = row.get("start_at_utc")
            rate = tracking["views_per_placement_hour"]
            if not start or rate <= 0:
                continue
            try:
                timezone = ZoneInfo(str(row.get("owner_timezone") or "UTC"))
            except ZoneInfoNotFoundError:
                timezone = ZoneInfo("UTC")
            per_hour[as_utc(start).astimezone(timezone).hour].append(rate)
        hourly = [
            {
                "hour": hour,
                "sample_campaigns": len(values),
                "views_per_placement_hour": round(sum(values) / len(values), 2) if values else 0,
            }
            for hour, values in per_hour.items()
        ]
        return {
            "sampled_campaigns": len(per_campaign),
            "sampled_placements": sampled_placements,
            "verified_views": verified_views,
            "average_views_per_placement": round(sum(per_campaign) / len(per_campaign)) if per_campaign else 0,
            "hourly": hourly,
        }

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
        current = await self.db.deliveries.find(query).sort("updated_at", -1).limit(limit).to_list(limit)
        if cycle_number is not None or len(current) >= limit:
            return current
        samples = list((await self._history_rollup(campaign_id)).get("failure_samples", []))
        return [*current, *samples][:limit]

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

    async def set_clone_setup_session(self, owner_id: int, state: Document, *, ttl_minutes: int = 30) -> None:
        """Persist the short guided clone-management flow independently.

        It must not share ``owner_sessions`` with campaign setup: navigating
        around either workspace should never orphan or overwrite the other.
        """
        now = utcnow()
        await self.db.clone_setup_sessions.update_one(
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

    async def clone_setup_session(self, owner_id: int) -> Document | None:
        document = await self.db.clone_setup_sessions.find_one(
            {"owner_id": owner_id, "expires_at": {"$gt": utcnow()}}
        )
        return document.get("state") if document else None

    async def clear_clone_setup_session(self, owner_id: int) -> None:
        await self.db.clone_setup_sessions.delete_one({"owner_id": owner_id})

    async def create_bot_clone(self, document: Document) -> None:
        await self.db.bot_clones.insert_one(document)

    async def get_bot_clone(self, clone_id: str) -> Document | None:
        return await self.db.bot_clones.find_one({"clone_id": clone_id})

    async def list_bot_clones(self) -> list[Document]:
        return await self.db.bot_clones.find({}).sort("created_at", -1).to_list(None)

    async def active_bot_clones(self) -> list[Document]:
        return await self.db.bot_clones.find({"active": True}).sort("created_at", 1).to_list(None)

    async def update_bot_clone(self, clone_id: str, **fields: Any) -> None:
        await self.db.bot_clones.update_one(
            {"clone_id": clone_id},
            {"$set": {**fields, "updated_at": utcnow()}},
        )

    @staticmethod
    def _token_hash(token: str) -> str:
        return sha256(token.encode("utf-8")).hexdigest()

    async def create_campaign_portal_share(self, owner_id: int, campaign_id: str) -> tuple[Document, str]:
        """Create a new revocable, high-entropy client progress link."""
        if not await self.db.campaigns.count_documents({"campaign_id": campaign_id}, limit=1):
            raise ValueError("campaign no longer exists")
        now = utcnow()
        for _ in range(5):
            token = secrets.token_urlsafe(32)
            document: Document = {
                "share_id": f"portal_{secrets.token_hex(10)}",
                "campaign_id": campaign_id,
                "owner_id": owner_id,
                "token_hash": self._token_hash(token),
                "created_at": now,
                "updated_at": now,
                "last_opened_at": None,
                "revoked_at": None,
            }
            try:
                await self.db.campaign_portal_shares.insert_one(document)
                return document, token
            except DuplicateKeyError:
                continue
        raise RuntimeError("could not allocate a client portal link")

    async def campaign_portal_share(self, token: str) -> Document | None:
        now = utcnow()
        share = await self.db.campaign_portal_shares.find_one(
            {"token_hash": self._token_hash(token), "revoked_at": None}
        )
        if share:
            await self.db.campaign_portal_shares.update_one(
                {"_id": share["_id"]}, {"$set": {"last_opened_at": now, "updated_at": now}}
            )
        return share

    async def revoke_campaign_portal_shares(self, campaign_id: str, owner_id: int) -> int:
        result = await self.db.campaign_portal_shares.update_many(
            {"campaign_id": campaign_id, "owner_id": owner_id, "revoked_at": None},
            {"$set": {"revoked_at": utcnow(), "updated_at": utcnow()}},
        )
        return result.modified_count

    async def create_client_promotion_request(
        self,
        *,
        share: Document,
        request_type: str,
        client_name: str,
        details: str,
        desired_start_at_utc: Any | None,
        client_timezone: str,
    ) -> tuple[Document, str]:
        if request_type not in {"RERUN", "NEW"}:
            raise ValueError("invalid promotion request type")
        now = utcnow()
        request_id = f"r{secrets.token_hex(8)}"
        material_token = secrets.token_hex(12)
        document: Document = {
            "request_id": request_id,
            "share_id": share["share_id"],
            "campaign_id": share["campaign_id"],
            "owner_id": share["owner_id"],
            "request_type": request_type,
            "client_name": client_name.strip()[:100],
            "details": details.strip()[:3000],
            "desired_start_at_utc": desired_start_at_utc,
            "client_timezone": client_timezone,
            "status": "PENDING_APPROVAL" if request_type == "RERUN" else "AWAITING_MATERIALS",
            "materials": [],
            "material_token_hash": self._token_hash(material_token),
            "submitter_user_id": None,
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
            "rejected_at": None,
        }
        await self.db.client_promotion_requests.insert_one(document)
        return document, material_token

    async def get_client_promotion_request(self, request_id: str, owner_id: int | None = None) -> Document | None:
        query: Document = {"request_id": request_id}
        if owner_id is not None:
            query["owner_id"] = owner_id
        return await self.db.client_promotion_requests.find_one(query)

    async def list_client_promotion_requests(self, owner_id: int, *, skip: int = 0, limit: int = 8) -> list[Document]:
        return await self.db.client_promotion_requests.find(
            {"owner_id": owner_id, "status": {"$in": ["AWAITING_MATERIALS", "PENDING_APPROVAL"]}}
        ).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)

    async def client_promotion_request_counts(self, owner_id: int) -> Document:
        cursor = await self.db.client_promotion_requests.aggregate(
            [
                {"$match": {"owner_id": owner_id, "status": {"$in": ["AWAITING_MATERIALS", "PENDING_APPROVAL"]}}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ],
        )
        rows = await cursor.to_list(None)
        return {row["_id"]: row["count"] for row in rows}

    async def pending_client_promotion_request_count(self) -> int:
        return await self.db.client_promotion_requests.count_documents(
            {"status": {"$in": ["AWAITING_MATERIALS", "PENDING_APPROVAL"]}}
        )

    async def begin_client_material_session(self, request_id: str, material_token: str, user_id: int) -> Document | None:
        """Bind material uploads to the first Telegram user who opens its link."""
        now = utcnow()
        request = await self.db.client_promotion_requests.find_one_and_update(
            {
                "request_id": request_id,
                "material_token_hash": self._token_hash(material_token),
                "status": "AWAITING_MATERIALS",
                "$or": [{"submitter_user_id": None}, {"submitter_user_id": user_id}],
            },
            {"$set": {"submitter_user_id": user_id, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not request:
            return None
        await self.db.client_material_sessions.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "request_id": request_id,
                    "updated_at": now,
                    "expires_at": now + timedelta(hours=24),
                }
            },
            upsert=True,
        )
        return request

    async def client_material_session(self, user_id: int) -> Document | None:
        return await self.db.client_material_sessions.find_one({"user_id": user_id, "expires_at": {"$gt": utcnow()}})

    async def append_client_request_material(self, request_id: str, user_id: int, material: Document) -> Document | None:
        now = utcnow()
        return await self.db.client_promotion_requests.find_one_and_update(
            {
                "request_id": request_id,
                "submitter_user_id": user_id,
                "status": "AWAITING_MATERIALS",
                "$expr": {"$lt": [{"$size": {"$ifNull": ["$materials", []]}}, 20]},
            },
            {"$push": {"materials": material}, "$set": {"updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )

    async def finish_client_material_session(self, request_id: str, user_id: int) -> Document | None:
        request = await self.db.client_promotion_requests.find_one_and_update(
            {
                "request_id": request_id,
                "submitter_user_id": user_id,
                "status": "AWAITING_MATERIALS",
                "$expr": {"$gt": [{"$size": {"$ifNull": ["$materials", []]}}, 0]},
            },
            {"$set": {"status": "PENDING_APPROVAL", "materials_completed_at": utcnow(), "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        await self.db.client_material_sessions.delete_one({"user_id": user_id, "request_id": request_id})
        return request

    async def cancel_client_material_session(self, user_id: int) -> None:
        await self.db.client_material_sessions.delete_one({"user_id": user_id})

    async def resolve_client_promotion_request(
        self,
        request_id: str,
        owner_id: int,
        *,
        status: str,
        approved_campaign_id: str | None = None,
    ) -> bool:
        if status not in {"APPROVED", "REJECTED"}:
            raise ValueError("invalid request resolution")
        now = utcnow()
        pending_statuses = ["PENDING_APPROVAL"] if status == "APPROVED" else ["PENDING_APPROVAL", "AWAITING_MATERIALS"]
        result = await self.db.client_promotion_requests.update_one(
            {"request_id": request_id, "owner_id": owner_id, "status": {"$in": pending_statuses}},
            {
                "$set": {
                    "status": status,
                    "updated_at": now,
                    "approved_at": now if status == "APPROVED" else None,
                    "rejected_at": now if status == "REJECTED" else None,
                    "approved_campaign_id": approved_campaign_id,
                }
            },
        )
        return result.modified_count == 1

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
            await self.db.campaign_portal_shares.delete_many(campaign_query)
            await self.db.client_promotion_requests.delete_many(campaign_query)
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
            # Telegram webhook retries arrive within minutes, not weeks.  A
            # one-day deduplication window is ample and keeps high-volume
            # owner/channel updates from consuming the campaign database.
            await self.db.processed_updates.insert_one({"update_id": update_id, "received_at": now, "expires_at": now + timedelta(days=1)})
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
