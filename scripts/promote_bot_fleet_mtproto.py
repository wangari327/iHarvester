#!/usr/bin/env python3
"""Promote iHarvester's approved bot fleet using authorised human sessions.

This is intentionally a private recovery-VPS worker, never part of the Koyeb
image.  The main bot only creates a compact ``bot_fleet_jobs`` document after
its owner confirms a channel.  For every job this worker resolves the exact
stored channel ID with each existing human session (using the registered public
username when present), verifies that ID, and grants the approved bots *post
and edit* rights only through an account Telegram permits to add administrators.

It never searches channel history, signs in an account, exports a session, or
adds any bot not listed on the job.  Existing sessions remain on the VPS.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pymongo import MongoClient, ReturnDocument

try:
    from telethon import TelegramClient, errors, functions, types
except ImportError as error:  # pragma: no cover - operator environment only
    raise SystemExit(
        "Telethon is required. Run: python -m pip install -e . -r requirements-mtproto-recovery.txt"
    ) from error


logger = logging.getLogger("iharvester.bot_fleet")
SUCCESS_STATES = {"ADDED", "UPDATED", "ALREADY_ADMIN"}
TERMINAL_STATES = {*SUCCESS_STATES, "FAILED", "CANCELLED"}


@dataclass
class OwnerSession:
    """One authorised human account and its exact, verified channel peers."""

    path: Path
    client: TelegramClient
    label: str
    channels: dict[int, Any] = field(default_factory=dict)
    cooldown_until: float = 0.0


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _channel_id(entity: Any) -> int:
    return -1_000_000_000_000 - int(entity.id)


async def _resolve_channel(client: TelegramClient, channel_id: int, username: str | None) -> Any | None:
    """Resolve a known channel peer without reading its messages or history."""
    expected_id = -channel_id - 1_000_000_000_000
    if username:
        try:
            entity = await client.get_entity(username)
        except (errors.FloodWaitError, errors.RPCError, ValueError):
            entity = None
        if isinstance(entity, types.Channel) and int(entity.id) == expected_id:
            access_hash = getattr(entity, "access_hash", None)
            if isinstance(access_hash, int):
                return types.InputChannel(expected_id, access_hash)
    try:
        peer = await client.get_input_entity(channel_id)
    except (errors.FloodWaitError, errors.RPCError, ValueError):
        return None
    if getattr(peer, "channel_id", None) != expected_id:
        return None
    access_hash = getattr(peer, "access_hash", None)
    return types.InputChannel(expected_id, access_hash) if isinstance(access_hash, int) else None


async def _open_sessions(
    paths: list[Path],
    api_id: int,
    api_hash: str,
    *,
    channel_id: int,
    channel_username: str | None,
) -> list[OwnerSession]:
    """Open pre-authorised humans and resolve only the requested exact peer."""
    opened: list[OwnerSession] = []
    for position, path in enumerate(paths, start=1):
        client = TelegramClient(
            str(path),
            api_id,
            api_hash,
            receive_updates=False,
            flood_sleep_threshold=0,
            request_retries=2,
        )
        try:
            await client.start()
            identity = await client.get_me()
            if not identity or identity.bot:
                raise RuntimeError("not an authorised human account")
            peer = await _resolve_channel(client, channel_id, channel_username)
            channels = {channel_id: peer} if peer else {}
            label = f"authorised account {position}"
            logger.info("%s: exact channel resolved=%s", label, bool(peer))
            opened.append(OwnerSession(path=path, client=client, label=label, channels=channels))
        except errors.FloodWaitError as error:
            # The next timer pass can retry after Telegram's requested cooldown
            # while other accounts remain usable.
            await client.disconnect()
            logger.warning("%s: dialog directory cooling down for %ss", path.name, error.seconds)
        except Exception:
            await client.disconnect()
            raise
    return opened


def _claim_job(database: Any, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
    now = _utcnow()
    return database.bot_fleet_jobs.find_one_and_update(
        {
            "$or": [
                {
                    "status": "QUEUED",
                    "$or": [
                        {"next_attempt_at": {"$exists": False}},
                        {"next_attempt_at": None},
                        {"next_attempt_at": {"$lte": now}},
                    ],
                },
                {"status": "RUNNING", "lease_until": {"$lte": now}},
            ]
        },
        {
            "$set": {
                "status": "RUNNING",
                "worker_id": worker_id,
                "lease_until": now + timedelta(seconds=lease_seconds),
                "started_at": now,
                "updated_at": now,
                "next_attempt_at": None,
            }
        },
        sort=[("requested_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _save_result(
    database: Any,
    job_id: str,
    username: str,
    *,
    status: str,
    account_label: str | None = None,
    error: str | None = None,
) -> None:
    database.bot_fleet_jobs.update_one(
        {"job_id": job_id, "results.username": username},
        {
            "$set": {
                "results.$.status": status,
                "results.$.account_label": account_label,
                "results.$.error": error,
                "results.$.updated_at": _utcnow(),
                "updated_at": _utcnow(),
            }
        },
    )


def _heartbeat(database: Any, job_id: str, lease_seconds: int) -> None:
    now = _utcnow()
    database.bot_fleet_jobs.update_one(
        {"job_id": job_id, "status": "RUNNING"},
        {"$set": {"lease_until": now + timedelta(seconds=lease_seconds), "updated_at": now}},
    )


def _finish_job(database: Any, job_id: str, *, status: str) -> None:
    now = _utcnow()
    database.bot_fleet_jobs.update_one(
        {"job_id": job_id},
        {"$set": {"status": status, "completed_at": now, "lease_until": None, "updated_at": now}},
    )


def _cancel_remaining(database: Any, job_id: str) -> None:
    now = _utcnow()
    database.bot_fleet_jobs.update_one(
        {"job_id": job_id},
        {
            "$set": {
                "results.$[pending].status": "CANCELLED",
                "results.$[pending].updated_at": now,
                "results.$[pending].error": "cancelled by owner",
                "status": "CANCELLED",
                "completed_at": now,
                "lease_until": None,
                "updated_at": now,
            }
        },
        array_filters=[{"pending.status": "PENDING"}],
    )


def _defer_job(database: Any, job_id: str, wait_seconds: int, reason: str) -> None:
    now = _utcnow()
    database.bot_fleet_jobs.update_one(
        {"job_id": job_id, "status": "RUNNING"},
        {
            "$set": {
                "status": "QUEUED",
                "lease_until": None,
                "next_attempt_at": now + timedelta(seconds=max(5, wait_seconds)),
                "last_worker_note": reason,
                "updated_at": now,
            }
        },
    )


async def _bot_input(session: OwnerSession, username: str, cache: dict[tuple[str, str], Any]) -> Any:
    cache_key = (session.label, username)
    if cached := cache.get(cache_key):
        return cached
    entity = await session.client.get_entity(username)
    if not bool(getattr(entity, "bot", False)):
        raise ValueError(f"@{username} is not a Telegram bot")
    access_hash = getattr(entity, "access_hash", None)
    if not isinstance(access_hash, int):
        raise ValueError(f"Telegram did not provide an access hash for @{username}")
    input_user = types.InputUser(int(entity.id), access_hash)
    cache[cache_key] = input_user
    return input_user


async def _already_admin(session: OwnerSession, channel: Any, bot: Any) -> bool:
    """Avoid overwriting an existing fleet bot's deliberately chosen rights."""
    try:
        response = await session.client(
            functions.channels.GetParticipantRequest(
                channel=channel,
                participant=types.InputPeerUser(bot.user_id, bot.access_hash),
            )
        )
    except errors.UserNotParticipantError:
        return False
    participant = getattr(response, "participant", None)
    return isinstance(participant, (types.ChannelParticipantAdmin, types.ChannelParticipantCreator))


def _fleet_rights() -> Any:
    """Operational post/link rights, without channel control or delegation."""
    return types.ChatAdminRights(
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        invite_users=True,
    )


async def _promote_one(session: OwnerSession, channel: Any, username: str, cache: dict[tuple[str, str], Any]) -> str:
    bot = await _bot_input(session, username, cache)
    already_admin = await _already_admin(session, channel, bot)
    await session.client(
        functions.channels.EditAdminRequest(
            channel=channel,
            user_id=bot,
            admin_rights=_fleet_rights(),
            rank="",
        )
    )
    # EditAdmin is deliberately also sent for existing fleet admins: it brings
    # an old, restrictive grant up to this worker's current safe capability
    # profile (including Invite Users via Link) without granting add-admins.
    return "UPDATED" if already_admin else "ADDED"


def _pending_results(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [result for result in job.get("results", []) if result.get("status") == "PENDING"]


def _final_status(job: dict[str, Any]) -> str:
    states = Counter(str(result.get("status") or "PENDING") for result in job.get("results", []))
    if states["PENDING"]:
        return "RUNNING"
    if states["CANCELLED"] and not (states["ADDED"] or states["UPDATED"] or states["ALREADY_ADMIN"] or states["FAILED"]):
        return "CANCELLED"
    if states["FAILED"] and not (states["ADDED"] or states["UPDATED"] or states["ALREADY_ADMIN"]):
        return "FAILED"
    return "COMPLETED" if not states["FAILED"] and not states["CANCELLED"] else "PARTIAL"


async def _run_job(database: Any, job: dict[str, Any], sessions: list[OwnerSession], args: argparse.Namespace) -> None:
    job_id = str(job["job_id"])
    channel_id = int(job["channel_id"])
    candidates = [session for session in sessions if channel_id in session.channels]
    if not candidates:
        for result in _pending_results(job):
            _save_result(
                database,
                job_id,
                str(result["username"]),
                status="FAILED",
                error="no authorised human session could resolve this exact channel peer",
            )
        _finish_job(database, job_id, status="FAILED")
        logger.warning("%s: no authorised session could resolve channel %s", job_id, channel_id)
        return

    cache: dict[tuple[str, str], Any] = {}
    offset = 0
    for pending in _pending_results(job):
        current = database.bot_fleet_jobs.find_one({"job_id": job_id}, {"status": 1}) or {}
        if current.get("status") == "CANCEL_REQUESTED":
            _cancel_remaining(database, job_id)
            logger.info("%s: cancelled by owner", job_id)
            return
        _heartbeat(database, job_id, args.lease_seconds)
        username = str(pending["username"])
        usable = sorted(
            candidates,
            key=lambda session: (session.cooldown_until > asyncio.get_running_loop().time(), (candidates.index(session) - offset) % len(candidates)),
        )
        promoted = False
        cooldowns: list[float] = []
        permission_denied = 0
        last_error: str | None = None
        for session in usable:
            remaining = session.cooldown_until - asyncio.get_running_loop().time()
            if remaining > 0:
                cooldowns.append(remaining)
                continue
            try:
                outcome = await _promote_one(session, session.channels[channel_id], username, cache)
                _save_result(database, job_id, username, status=outcome, account_label=session.label)
                logger.info("%s: %s %s using %s", job_id, outcome.lower(), username, session.label)
                promoted = True
                offset = (candidates.index(session) + 1) % len(candidates)
                break
            except errors.FloodWaitError as error:
                wait_seconds = int(error.seconds) + 2
                session.cooldown_until = asyncio.get_running_loop().time() + wait_seconds
                cooldowns.append(wait_seconds)
                logger.warning("%s: %s cooling down for %ss", job_id, session.label, error.seconds)
            except (errors.ChatAdminRequiredError, errors.ChannelPrivateError):
                permission_denied += 1
                last_error = "authorised account lacks Add admins permission for this channel"
            except errors.UsernameNotOccupiedError:
                _save_result(database, job_id, username, status="FAILED", error="the configured Telegram bot username no longer exists")
                promoted = True
                break
            except ValueError as error:
                _save_result(database, job_id, username, status="FAILED", error=str(error))
                promoted = True
                break
            except errors.RPCError as error:
                last_error = f"Telegram refused the promotion: {error.__class__.__name__}"
                logger.warning("%s: %s via %s: %s", job_id, username, session.label, error)
            except Exception as error:  # a single configured bot must not block the rest
                last_error = f"unexpected worker error: {error.__class__.__name__}"
                logger.exception("%s: could not promote %s", job_id, username)
        if not promoted:
            if cooldowns and len(cooldowns) >= len(candidates):
                _defer_job(database, job_id, int(min(cooldowns)), "waiting for Telegram flood cooldown")
                logger.info("%s: deferred until a cooled-down authorised account is available", job_id)
                return
            if permission_denied == len(candidates):
                _save_result(
                    database,
                    job_id,
                    username,
                    status="FAILED",
                    error="none of the authorised accounts in this channel has Add admins permission",
                )
            else:
                _save_result(database, job_id, username, status="FAILED", error=last_error or "Telegram could not promote this bot")
        if args.delay:
            await asyncio.sleep(args.delay)

    finished = database.bot_fleet_jobs.find_one({"job_id": job_id})
    if not finished:
        return
    _finish_job(database, job_id, status=_final_status(finished))
    logger.info("%s: %s", job_id, _final_status(finished).lower())


async def run(args: argparse.Namespace) -> int:
    mongo_uri = _required_env("MONGODB_URI")
    database_name = os.environ.get("MONGODB_DB_NAME", "telegram_campaign_orchestrator")
    api_id = int(_required_env("TELEGRAM_API_ID"))
    api_hash = _required_env("TELEGRAM_API_HASH")
    session_paths = [Path(value).expanduser().resolve() for value in args.session]
    for path in session_paths:
        if not path.with_suffix(".session").is_file() and not path.is_file():
            raise SystemExit(f"Session file not found: {path} (expected a Telethon .session file)")
    mongo = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=20_000, connectTimeoutMS=20_000, socketTimeoutMS=60_000)
    sessions: list[OwnerSession] = []
    try:
        mongo.admin.command("ping")
        database = mongo[database_name]
        if args.dry_run:
            queued = list(
                database.bot_fleet_jobs.find(
                    {"status": {"$in": ["QUEUED", "RUNNING", "CANCEL_REQUESTED"]}},
                    {"job_id": 1, "channel_id": 1, "status": 1, "results": 1},
                ).limit(10)
            )
            if not queued:
                logger.info("No fleet jobs waiting.")
                return 0
            for job in queued:
                logger.info("Would process %s for channel %s (%s)", job["job_id"], job["channel_id"], job["status"])
            return 0
        worker_id = f"bot-fleet:{socket.gethostname()}:{os.getpid()}"
        job = _claim_job(database, worker_id, args.lease_seconds)
        if not job:
            logger.info("No fleet jobs waiting.")
            return 0
        try:
            sessions = await _open_sessions(
                session_paths,
                api_id,
                api_hash,
                channel_id=int(job["channel_id"]),
                channel_username=job.get("channel_username"),
            )
        except Exception:
            _defer_job(database, str(job["job_id"]), 300, "could not open authorised Telegram session directory")
            raise
        if not sessions:
            _defer_job(database, str(job["job_id"]), 300, "no authorised Telegram session directory is currently available")
            logger.warning("%s: no authorised session directory could be opened", job["job_id"])
            return 1
        completed = 0
        while True:
            await _run_job(database, job, sessions, args)
            completed += 1
            if args.once:
                return 0
            for session in sessions:
                await session.client.disconnect()
            sessions = []
            job = _claim_job(database, worker_id, args.lease_seconds)
            if not job:
                return 0
            sessions = await _open_sessions(
                session_paths,
                api_id,
                api_hash,
                channel_id=int(job["channel_id"]),
                channel_username=job.get("channel_username"),
            )
    finally:
        for session in sessions:
            await session.client.disconnect()
        mongo.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely promote iHarvester's approved bot fleet using existing human Telegram sessions.")
    parser.add_argument("--session", action="append", required=True, help="Authorised Telethon session path; repeat for every human account.")
    parser.add_argument("--once", action="store_true", help="Process at most one queued channel then exit (recommended for a systemd timer).")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between bot promotions (default: 1.0).")
    parser.add_argument("--lease-seconds", type=int, default=600, help="Mongo work lease duration (default: 600).")
    parser.add_argument("--dry-run", action="store_true", help="List pending jobs without opening Telegram sessions or changing MongoDB.")
    return parser.parse_args()


if __name__ == "__main__":  # pragma: no cover - operator entry point
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
