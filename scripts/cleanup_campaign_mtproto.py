"""Recover campaign posts with an iHarvester bot or human-admin MTProto session.

Run this on the owner's workstation, never as part of the hosted service. It
does not search channel history or match post text: Mongo's campaign live-state
is the source of truth and supplies the exact channel and message IDs to erase.

The first run must be a dry run. A real run requires ``--confirm`` and targets
only campaigns that are already ENDING or ARCHIVED.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pymongo import MongoClient

try:
    from telethon import TelegramClient, errors, functions, types
except ImportError as error:  # pragma: no cover - exercised by the operator
    raise SystemExit(
        "Telethon is required for MTProto recovery. Run: python -m pip install -r requirements-mtproto-recovery.txt"
    ) from error


logger = logging.getLogger("iharvester.mtproto_cleanup")


@dataclass(frozen=True)
class CleanupTarget:
    channel_id: int
    message_ids: tuple[int, ...]
    username: str | None = None


class ChannelUnavailableError(RuntimeError):
    """The current user session has no MTProto access hash for this channel."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required. See docs/mtproto-recovery.md.")
    return value


def _targets(
    database: Any,
    campaign_id: str,
    limit: int | None,
    *,
    public_only: bool = False,
) -> list[CleanupTarget]:
    """Return exact live posts, including their stored public usernames.

    MTProto bot sessions cannot enumerate their dialogs, so bare Bot API chat
    IDs have no access hash to resolve. Public usernames discovered by the bot
    can be resolved one at a time without reading message history. Private
    channels remain untouched when ``public_only`` is requested.
    """

    rows = database.campaign_channel_state.aggregate(
        [
            {"$match": {"campaign_id": campaign_id}},
            {"$sort": {"channel_id": 1}},
            {
                "$lookup": {
                    "from": "channels",
                    "localField": "channel_id",
                    "foreignField": "telegram_chat_id",
                    "as": "channel",
                }
            },
            {"$unwind": {"path": "$channel", "preserveNullAndEmptyArrays": True}},
            {
                "$project": {
                    "_id": 0,
                    "channel_id": 1,
                    "current_message_ids": 1,
                    "username": "$channel.username",
                }
            },
        ]
    )
    targets: list[CleanupTarget] = []
    for row in rows:
        message_ids = tuple(sorted({int(value) for value in row.get("current_message_ids", []) if int(value) > 0}))
        username = row.get("username")
        if not isinstance(username, str) or not username.strip():
            username = None
        if message_ids and (username or not public_only):
            targets.append(
                CleanupTarget(
                    channel_id=int(row["channel_id"]),
                    message_ids=message_ids,
                    username=username,
                )
            )
            if limit is not None and len(targets) >= limit:
                break
    return targets


def _mark_cleaned(database: Any, campaign_id: str, target: CleanupTarget) -> None:
    now = datetime.now(UTC)
    database.campaign_channel_state.delete_one({"campaign_id": campaign_id, "channel_id": target.channel_id})
    database.deliveries.update_one(
        {
            "campaign_id": campaign_id,
            "cycle_number": -1,
            "channel_id": target.channel_id,
            "operation": "CLEANUP",
        },
        {
            "$set": {
                "status": "CLEANED",
                "cleaned_message_count": len(target.message_ids),
                "error_category": None,
                "error_summary": None,
                "mtproto_recovered_at": now,
                "updated_at": now,
            }
        },
    )


def _record_failure(database: Any, campaign_id: str, target: CleanupTarget, error: Exception) -> None:
    now = datetime.now(UTC)
    database.deliveries.update_one(
        {
            "campaign_id": campaign_id,
            "cycle_number": -1,
            "channel_id": target.channel_id,
            "operation": "CLEANUP",
        },
        {
            "$set": {
                "mtproto_last_error": f"{type(error).__name__}: {error}"[:500],
                "mtproto_last_attempt_at": now,
                "updated_at": now,
            }
        },
    )


async def _delete_target(
    client: TelegramClient,
    target: CleanupTarget,
    *,
    allow_public_username: bool,
) -> None:
    # ``delete_messages`` resolves to Telegram's channels.deleteMessages for
    # channels. It receives exact IDs from campaign state—there is no forward
    # or backward channel-history iteration and no risk of matching new posts.
    try:
        entity = await client.get_input_entity(target.channel_id)
    except ValueError:
        if not allow_public_username or not target.username:
            raise ChannelUnavailableError(
                "This session has no cached MTProto access hash for the channel."
            ) from None
        entity = await client.get_input_entity(target.username)
        expected_channel_id = -target.channel_id - 1_000_000_000_000
        if getattr(entity, "channel_id", None) != expected_channel_id:
            raise RuntimeError(
                f"@{target.username} resolved to a different channel; refusing to delete the tracked post."
            ) from None
    # Telethon's convenience helper chooses a generic deletion method for
    # some cached peer shapes.  This recovery is exclusively for channels, so
    # explicitly call the channel endpoint that accepts an InputChannel.
    channel = types.InputChannel(entity.channel_id, entity.access_hash)
    await client(functions.channels.DeleteMessagesRequest(channel, list(target.message_ids)))


async def run(args: argparse.Namespace) -> int:
    mongo_uri = _required_env("MONGODB_URI")
    database_name = os.environ.get("MONGODB_DB_NAME", "telegram_campaign_orchestrator")
    api_id = int(_required_env("TELEGRAM_API_ID"))
    api_hash = _required_env("TELEGRAM_API_HASH")
    bot_token = _required_env("BOT_TOKEN") if args.identity == "bot" else None
    mongo = MongoClient(mongo_uri, tz_aware=True)
    database = mongo[database_name]
    try:
        campaign = database.campaigns.find_one({"campaign_id": args.campaign}, {"status": 1, "name": 1})
        if not campaign:
            raise SystemExit("Campaign not found. Copy its campaign ID from the bot's report/export.")
        if campaign.get("status") not in {"ENDING", "ARCHIVED"}:
            raise SystemExit("MTProto recovery only accepts an ENDING or ARCHIVED campaign; stop it first.")
        targets = _targets(database, args.campaign, args.limit, public_only=args.public_only)
        message_total = sum(len(target.message_ids) for target in targets)
        logger.info(
            "Campaign %s (%s): %s channels, %s exact message IDs%s",
            args.campaign,
            campaign.get("name", "unnamed"),
            len(targets),
            message_total,
            " [DRY RUN]" if not args.confirm else "",
        )
        if not args.confirm:
            for target in targets[:20]:
                logger.info("Would delete channel %s message IDs %s", target.channel_id, list(target.message_ids))
            if len(targets) > 20:
                logger.info("… and %s more channels", len(targets) - 20)
            return 0
        if not targets:
            logger.info("No tracked live posts remain for this campaign.")
            return 0

        session = Path(args.session).expanduser().resolve()
        session.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(session),
            api_id,
            api_hash,
            receive_updates=False,
            flood_sleep_threshold=120,
            request_retries=5,
        )
        if args.identity == "bot":
            await client.start(bot_token=bot_token)
        else:
            # This must already be an authorized local session. Use the QR
            # helper, never phone codes or passwords in a deployment config.
            await client.start()
        identity = await client.get_me()
        if not identity:
            raise SystemExit("Could not determine the MTProto identity.")
        if args.identity == "bot" and not identity.bot:
            raise SystemExit("The configured BOT_TOKEN did not authenticate as a bot. Refusing to use a personal account.")
        if args.identity == "user" and identity.bot:
            raise SystemExit("The selected session is a bot. Use --identity bot or authorize a human admin account.")
        if args.identity == "user":
            logger.info("Loading this human admin's channel directory (no message history is scanned).")
            await client.get_dialogs(limit=None)
        logger.info(
            "Authenticated as @%s (%s). Starting exact-ID deletion at %.1f requests/sec%s.",
            identity.username or identity.id,
            args.identity,
            args.rps,
            " (public channels only)" if args.public_only else "",
        )

        deleted = 0
        failed = 0
        skipped = 0
        delay = 1 / args.rps
        try:
            for index, target in enumerate(targets, start=1):
                failure: Exception | None = None
                try:
                    await _delete_target(
                        client,
                        target,
                        allow_public_username=args.identity == "bot",
                    )
                except ChannelUnavailableError:
                    skipped += 1
                    logger.debug("%s/%s channel %s is not in this account's directory", index, len(targets), target.channel_id)
                    continue
                except errors.MsgIdInvalidError:
                    # A post deleted manually after Mongo recorded it is
                    # already clean. This is the MTProto equivalent of the
                    # Bot API's "message to delete not found" outcome.
                    logger.info("%s/%s channel %s was already absent", index, len(targets), target.channel_id)
                except errors.FloodWaitError as error:
                    logger.warning("Flood wait at channel %s; sleeping %s seconds", target.channel_id, error.seconds)
                    await asyncio.sleep(error.seconds)
                    try:
                        await _delete_target(
                            client,
                            target,
                            allow_public_username=args.identity == "bot",
                        )
                    except Exception as retry_error:
                        failure = retry_error
                except Exception as error:
                    failure = error
                if failure:
                    failed += 1
                    _record_failure(database, args.campaign, target, failure)
                    logger.error("%s/%s channel %s failed: %s", index, len(targets), target.channel_id, failure)
                else:
                    deleted += 1
                    _mark_cleaned(database, args.campaign, target)
                    logger.info("%s/%s channel %s cleaned", index, len(targets), target.channel_id)
                await asyncio.sleep(delay)
        finally:
            await client.disconnect()
        logger.info(
            "Finished: %s channels reconciled, %s failed, %s not in this session. Refresh the campaign dashboard.",
            deleted,
            failed,
            skipped,
        )
        return 0 if not failed else 2
    finally:
        mongo.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete iHarvester's exact tracked campaign posts with an MTProto identity.")
    parser.add_argument("--campaign", required=True, help="Campaign ID, for example cmp_xxxxx")
    parser.add_argument("--confirm", action="store_true", help="Perform deletion. Omit this flag for a dry run.")
    parser.add_argument("--limit", type=int, help="Process only the first N channels; use 10 for the pilot.")
    parser.add_argument("--rps", type=float, default=4, help="Maximum MTProto requests per second (default: 4).")
    parser.add_argument(
        "--identity",
        choices=("bot", "user"),
        default="bot",
        help="Use the iHarvester bot (default) or an already-authorized human admin session.",
    )
    parser.add_argument(
        "--public-only",
        action="store_true",
        help="Process only stored public usernames; private channels remain untouched.",
    )
    parser.add_argument(
        "--session",
        default="work/iharvester-mtproto-bot",
        help="Local Telethon session path. It is sensitive and ignored by Git.",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.rps <= 0 or args.rps > 10:
        parser.error("--rps must be greater than 0 and no more than 10")
    if args.public_only and args.identity != "bot":
        parser.error("--public-only is only available with --identity bot")
    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
