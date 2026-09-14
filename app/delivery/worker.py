from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

from pymongo.errors import PyMongoError

from app.campaigns.models import ChannelStatus, DeliveryStatus
from app.db.repositories import Repositories
from app.delivery.error_classification import ErrorKind, classify_telegram_error
from app.delivery.rate_limit import AsyncTokenBucket
from app.telegram.sender import TelegramSender
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class DeliveryWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        repositories: Repositories,
        sender: TelegramSender,
        send_limiter: AsyncTokenBucket,
        mutation_limiter: AsyncTokenBucket,
        delivery_lease_seconds: int,
        max_transient_attempts: int,
    ) -> None:
        self.worker_id = worker_id
        self.repositories = repositories
        self.sender = sender
        self.send_limiter = send_limiter
        self.mutation_limiter = mutation_limiter
        self.delivery_lease_seconds = delivery_lease_seconds
        self.max_transient_attempts = max_transient_attempts

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            delivery: dict[str, Any] | None = None
            try:
                delivery = await self.repositories.claim_delivery(self.worker_id, self.delivery_lease_seconds)
                if not delivery:
                    await asyncio.sleep(0.25)
                    continue
                await self.process(delivery)
            except asyncio.CancelledError:
                raise
            except PyMongoError:
                # All workers share one database.  During an Atlas/Koyeb
                # connection interruption, retrying every second creates an
                # avoidable connection and log storm without making progress.
                logger.warning("Delivery worker paused: MongoDB unavailable")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=5)
                except TimeoutError:
                    pass
            except Exception as error:
                logger.exception(
                    "Delivery worker iteration failed",
                    extra={"delivery_id": str(delivery.get("_id")) if delivery else None},
                )
                if delivery:
                    try:
                        await self.repositories.retry_delivery(
                            delivery["_id"],
                            5,
                            error_category="WORKER_EXCEPTION",
                            error_summary=self._safe_error_summary(error),
                        )
                    except Exception:
                        logger.exception("Could not persist worker retry state")
                await asyncio.sleep(1)

    async def process(self, delivery: dict[str, Any]) -> None:
        if delivery.get("operation") == "CLEANUP":
            await self._cleanup(delivery)
            return
        campaign = await self.repositories.get_campaign(delivery["campaign_id"])
        if not campaign or campaign["status"] != "ACTIVE":
            await self._complete(delivery, delivery["_id"], DeliveryStatus.PAUSED if campaign and campaign["status"] == "PAUSED" else DeliveryStatus.CANCELLED)
            return
        channel = await self.repositories.get_channel(delivery["channel_id"])
        if not channel or channel.get("status") != ChannelStatus.ACTIVE.value or not channel.get("permissions", {}).get("can_post_messages"):
            await self._permanent(
                delivery,
                channel or {"telegram_chat_id": delivery["channel_id"]},
                "CHANNEL_NOT_POSTABLE",
                "Channel is unavailable or the bot cannot post there.",
            )
            return
        replaced_message_count = 0
        if delivery["cycle_number"] > 0:
            state = await self.repositories.live_state(delivery["campaign_id"], delivery["channel_id"])
            if state and state.get("current_message_ids"):
                try:
                    replaced_message_count = await self._delete_message_ids(delivery["channel_id"], state["current_message_ids"])
                    await self.repositories.clear_live_state(delivery["campaign_id"], delivery["channel_id"])
                except Exception as error:
                    decision = classify_telegram_error(error, operation="delete")
                    if decision.kind == ErrorKind.CLEAN_ABSENT:
                        await self.repositories.clear_live_state(delivery["campaign_id"], delivery["channel_id"])
                    elif decision.kind == ErrorKind.PERMANENT:
                        await self._permanent(delivery, channel, "DELETE_FAILED_NO_REPLACEMENT", self._safe_error_summary(error))
                        return
                    else:
                        await self._retry_or_fail(delivery, decision, error=error)
                        return
        # A campaign may enter ENDING while a worker was waiting for a limiter token.
        campaign = await self.repositories.get_campaign(delivery["campaign_id"])
        if not campaign or campaign["status"] != "ACTIVE":
            await self._complete(delivery, delivery["_id"], DeliveryStatus.PAUSED if campaign and campaign["status"] == "PAUSED" else DeliveryStatus.CANCELLED)
            return
        try:
            await self.send_limiter.acquire()
            await self.mutation_limiter.acquire()
            result = await self.sender.send_variant(delivery["channel_id"], self._delivery_variant(campaign, delivery))
        except Exception as error:
            decision = classify_telegram_error(error, operation="send")
            if decision.kind == ErrorKind.PERMANENT:
                await self._permanent(delivery, channel, decision.category, self._safe_error_summary(error))
            elif decision.kind == ErrorKind.AMBIGUOUS:
                await self._complete(
                    delivery,
                    delivery["_id"],
                    DeliveryStatus.UNKNOWN_SEND_STATE,
                    error_category=decision.category,
                    error_summary=self._safe_error_summary(error),
                )
            else:
                await self._retry_or_fail(delivery, decision, error=error)
            return
        # Persist every confirmed Telegram send before ending can archive. The
        # scheduler waits for PROCESSING work and then materializes cleanup
        # from this campaign-scoped live-state pointer.
        await self.repositories.save_live_state(
            delivery["campaign_id"],
            delivery["channel_id"],
            delivery["cycle_number"],
            delivery["variant_index"],
            result.message_ids,
        )
        await self._complete(
            delivery,
            delivery["_id"],
            DeliveryStatus.SENT,
            sent_message_ids=result.message_ids,
            sent_at=utcnow(),
            replaced_message_count=replaced_message_count,
        )
        await self.repositories.set_channel_status(delivery["channel_id"], ChannelStatus.ACTIVE, last_successful_post_at=utcnow(), last_error_code=None)
        try:
            await self.repositories.materialize_superseded_cleanup(delivery["channel_id"], delivery["campaign_id"])
        except Exception:
            # The new send is already durably complete. Never retry it merely
            # because optional cleanup of older retained history was delayed.
            logger.exception(
                "Could not queue superseded retained-post cleanup",
                extra={"campaign_id": delivery["campaign_id"], "channel_id": delivery["channel_id"]},
            )

    async def _cleanup(self, delivery: dict[str, Any]) -> None:
        cleaned_count = 0
        try:
            cleaned_count = await self._delete_message_ids(delivery["channel_id"], delivery["message_ids"])
        except Exception as error:
            decision = classify_telegram_error(error, operation="delete")
            if decision.kind == ErrorKind.CLEAN_ABSENT:
                pass
            elif decision.kind == ErrorKind.PERMANENT:
                await self._complete(
                    delivery,
                    delivery["_id"],
                    DeliveryStatus.CLEANUP_FAILED,
                    error_category=decision.category,
                    error_summary=self._safe_error_summary(error),
                )
                if decision.category == "ACCESS_OR_PERMISSION":
                    # Future posts are likely to fail too.  Keep this channel
                    # discoverable in the existing one-click attention refresh
                    # flow, rather than silently leaving cleanup blocked.
                    await self.repositories.set_channel_status(
                        delivery["channel_id"],
                        ChannelStatus.NEEDS_ATTENTION,
                        last_error_code="CLEANUP_ACCESS_OR_PERMISSION",
                        last_error_at=utcnow(),
                    )
                return
            else:
                await self._retry_or_fail(delivery, decision, cleanup=True, error=error)
                return
        await self.repositories.clear_live_state(delivery["campaign_id"], delivery["channel_id"])
        await self._complete(delivery, delivery["_id"], DeliveryStatus.CLEANED, cleaned_message_count=cleaned_count)

    async def _retry_or_fail(self, delivery: dict[str, Any], decision: Any, cleanup: bool = False, error: Exception | None = None) -> None:
        summary = self._safe_error_summary(error) if error else decision.category
        # Cleanup is idempotent, unlike sends.  Give it a larger bounded retry
        # budget so a brief Telegram/Atlas disruption cannot leave an otherwise
        # healthy campaign at ENDING after only a few seconds.
        max_attempts = max(self.max_transient_attempts, 6) if cleanup else self.max_transient_attempts
        if delivery["attempts"] >= max_attempts:
            status = DeliveryStatus.CLEANUP_FAILED if cleanup else DeliveryStatus.FAILED_PERMANENT
            details: dict[str, Any] = {
                "error_category": "RETRY_EXHAUSTED",
                "error_summary": summary,
            }
            if cleanup:
                # The scheduler can make a bounded, delayed recovery attempt
                # without turning a persistent error into a hot loop.
                details["cleanup_auto_retry_at"] = utcnow() + timedelta(minutes=5)
            await self._complete(delivery, delivery["_id"], status, **details)
            return
        # Spread transient cleanup retries out.  This matters when hundreds of
        # channels hit a rate limit or a short Telegram outage together.
        delay = decision.retry_after_seconds or 5
        if cleanup:
            delay = min(300, max(delay, 5) * (2 ** min(max(0, int(delivery["attempts"]) - 1), 5)))
        await self.repositories.retry_delivery(
            delivery["_id"],
            delay,
            error_category=decision.category,
            error_summary=summary,
        )

    async def _permanent(self, delivery: dict[str, Any], channel: dict[str, Any], category: str, summary: str) -> None:
        await self._complete(
            delivery,
            delivery["_id"],
            DeliveryStatus.FAILED_PERMANENT,
            error_category=category,
            error_summary=summary,
        )
        if category in {"ACCESS_OR_PERMISSION", "CHANNEL_NOT_POSTABLE", "DELETE_FAILED_NO_REPLACEMENT"}:
            status = ChannelStatus.UNAVAILABLE if category == "ACCESS_OR_PERMISSION" else ChannelStatus.NEEDS_ATTENTION
            await self.repositories.set_channel_status(channel["telegram_chat_id"], status, last_error_code=category, last_error_at=utcnow())

    async def _delete_message_ids(self, channel_id: int, message_ids: list[int]) -> int:
        """Delete albums safely: one missing item must not skip later items."""
        deleted = 0
        for message_id in message_ids:
            try:
                await self.mutation_limiter.acquire()
                await self.sender.delete_messages(channel_id, [message_id])
                deleted += 1
            except Exception as error:
                if classify_telegram_error(error, operation="delete").kind == ErrorKind.CLEAN_ABSENT:
                    continue
                raise
        return deleted

    async def _complete(self, delivery: dict[str, Any], delivery_id: Any, status: DeliveryStatus, **details: Any) -> None:
        await self.repositories.complete_delivery(delivery_id, status, **details)
        if delivery.get("operation") != "CLEANUP":
            await self.repositories.finish_cycle_if_complete(delivery["campaign_id"], int(delivery.get("cycle_number", -1)))

    @staticmethod
    def _delivery_variant(campaign: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
        """Resolve the exact revision frozen when a cycle was materialized.

        Legacy deliveries do not carry a revision. Once a live edit creates a
        history for them, revision 1 is the pre-edit payload and keeps their
        already-running cycle internally consistent.
        """
        current = campaign["variants"][int(delivery["variant_index"])]
        variant_id = delivery.get("variant_id") or current.get("id")
        revision = int(delivery.get("variant_revision", 1))
        history = campaign.get("variant_versions", {}).get(variant_id, [])
        for item in history:
            if int(item.get("revision", 0)) == revision:
                return item["creative"]
        return current

    @staticmethod
    def _safe_error_summary(error: Exception) -> str:
        text = re.sub(r"https?://\S+", "[redacted-url]", str(error), flags=re.IGNORECASE)
        return f"{type(error).__name__}: {text}"[:240]
