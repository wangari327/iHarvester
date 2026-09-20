from __future__ import annotations

from app.db.client import Database


async def ensure_indexes(database: Database) -> None:
    db = database.db
    await db.channels.create_index("telegram_chat_id", unique=True)
    await db.channels.create_index("status")
    await db.channels.create_index("tags")
    await db.channels.create_index("member_count")
    await db.channels.create_index("last_successful_post_at")
    await db.campaigns.create_index([("status", 1), ("start_at_utc", 1)])
    await db.campaign_cycles.create_index([("campaign_id", 1), ("cycle_number", 1)], unique=True)
    await db.deliveries.create_index([("campaign_id", 1), ("cycle_number", 1), ("channel_id", 1)], unique=True)
    await db.deliveries.create_index([("status", 1), ("next_retry_at", 1), ("lease_until", 1), ("dispatch_rank", 1)])
    await db.campaign_channel_state.create_index([("campaign_id", 1), ("channel_id", 1)], unique=True)
    await db.campaign_channel_state.create_index([("campaign_id", 1), ("updated_at", 1)])
    # In-place repairs are separate from delivery cycles so campaign send and
    # cleanup statistics remain exact.  A repair can safely resume after a
    # deployment because each channel/message snapshot is durable.
    await db.live_text_repairs.create_index([("campaign_id", 1), ("repair_id", 1), ("channel_id", 1)], unique=True)
    await db.live_text_repairs.create_index([("status", 1), ("next_retry_at", 1), ("lease_until", 1), ("dispatch_rank", 1)])
    await db.locks.create_index("lock_name", unique=True)
    await db.join_events.create_index([("campaign_id", 1), ("destination_id", 1), ("joined_at_utc", 1)])
    await db.processed_updates.create_index("update_id", unique=True)
    await db.processed_updates.create_index("expires_at", expireAfterSeconds=0)
    await db.owner_sessions.create_index("owner_id", unique=True)
    await db.owner_sessions.create_index("expires_at", expireAfterSeconds=0)
    await db.pending_restores.create_index("restore_id", unique=True)
    await db.pending_restores.create_index("expires_at", expireAfterSeconds=0)
    await db.variant_shares.create_index("share_code", unique=True)
    await db.variant_shares.create_index(
        [("owner_id", 1), ("campaign_id", 1), ("variant_id", 1), ("snapshot_hash", 1), ("revoked_at", 1)],
        unique=True,
    )
    await db.variant_shares.create_index([("owner_id", 1), ("campaign_id", 1), ("variant_id", 1), ("created_at", -1)])
    await db.variant_shares.create_index("purge_at", expireAfterSeconds=0)
    # Client-facing progress pages use a long opaque bearer token.  Only its
    # digest is stored, so exporting the database cannot reveal a live link.
    await db.campaign_portal_shares.create_index("token_hash", unique=True)
    await db.campaign_portal_shares.create_index([("campaign_id", 1), ("revoked_at", 1)])
    await db.client_promotion_requests.create_index("request_id", unique=True)
    await db.client_promotion_requests.create_index([("owner_id", 1), ("status", 1), ("created_at", -1)])
    await db.client_promotion_requests.create_index("material_token_hash", unique=True)
    await db.client_material_sessions.create_index("user_id", unique=True)
    await db.client_material_sessions.create_index("expires_at", expireAfterSeconds=0)
