"""Background worker for full channel-registry refreshes."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiogram import Bot
from pymongo.errors import PyMongoError

from app.db.repositories import Repositories
from app.delivery.rate_limit import AsyncTokenBucket
from app.telegram.handlers_admin_updates import refresh_channel

logger = logging.getLogger(__name__)


class ChannelRefreshWorker:
    """Re-verify access and subscriber counts without blocking a webhook callback."""

    def __init__(
        self,
        *,
        worker_id: str,
        bot: Bot,
        repositories: Repositories,
        request_limiter: AsyncTokenBucket,
        lease_seconds: int,
        max_attempts: int,
    ) -> None:
        self.worker_id = worker_id
        self.bot = bot
        self.repositories = repositories
        self.request_limiter = request_limiter
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            job: dict[str, Any] | None = None
            try:
                job = await self.repositories.claim_network_refresh_job(self.worker_id, self.lease_seconds)
                if not job:
                    await asyncio.sleep(0.5)
                    continue
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except PyMongoError:
                logger.warning("Network refresh worker paused: MongoDB unavailable")
                await self._wait(stopping, 5)
            except Exception as error:
                logger.exception("Network refresh worker failed", extra={"job_id": str(job.get("_id")) if job else None})
                if job:
                    await self._retry_or_fail(job, error)
                await self._wait(stopping, 1)

    async def process(self, job: dict[str, Any]) -> None:
        try:
            active = await refresh_channel(
                self.bot,
                self.repositories,
                int(job["channel_id"]),
                request_limiter=self.request_limiter,
                preserve_manual_pause=True,
            )
            channel = await self.repositories.get_channel(int(job["channel_id"]))
            await self.repositories.complete_network_refresh_job(
                job["_id"],
                "COMPLETED",
                observed_status=channel.get("status") if channel else "MISSING",
                member_count=channel.get("member_count") if channel else None,
                access_verified=active,
            )
        except Exception as error:
            await self._retry_or_fail(job, error)

    async def _retry_or_fail(self, job: dict[str, Any], error: Exception) -> None:
        summary = self._safe_error_summary(error)
        if int(job.get("attempts", 0)) >= max(3, self.max_attempts):
            await self.repositories.complete_network_refresh_job(
                job["_id"],
                "FAILED",
                error_summary=summary,
            )
            return
        await self.repositories.retry_network_refresh_job(job["_id"], 5, error_summary=summary)

    @staticmethod
    async def _wait(stopping: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    @staticmethod
    def _safe_error_summary(error: Exception) -> str:
        return re.sub(r"https?://\S+", "[redacted-url]", f"{type(error).__name__}: {error}", flags=re.IGNORECASE)[:240]
