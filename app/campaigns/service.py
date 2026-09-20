"""Campaign lifecycle: freeze targets once, preserve history, and never reactivate archives."""

from __future__ import annotations

import base64
import logging
import secrets
from copy import deepcopy
from datetime import timedelta
from math import ceil
from typing import Any

from app.campaigns.models import CampaignMode, CampaignStatus, Creative, Destination, Schedule
from app.campaigns.scheduling import can_create_cycle, fit_rotation_schedule, scheduled_cycle_count, scheduled_cycle_time
from app.campaigns.shuffle import cohort_map, dispatch_rank, variant_for
from app.campaigns.validation import SAFE_DELETE_WINDOW, protected_destination_ids, validate_launch
from app.db.repositories import Document, Repositories
from app.utils.ids import opaque_id
from app.utils.time import as_utc, utcnow

logger = logging.getLogger(__name__)

# Telegram's Bot API rejects deletion once a message reaches 48 hours. Leave
# enough time to replace posts across a large channel network and to recover
# from a short worker outage before that hard limit.
_CLEANUP_REFRESH_LEAD = timedelta(hours=2)


class CampaignService:
    def __init__(self, repositories: Repositories, send_rps: float) -> None:
        self.repositories = repositories
        self.send_rps = send_rps

    async def create_draft(self, owner_id: int, name: str) -> Document:
        now = utcnow()
        name = self._campaign_name(name)
        campaign = {
            "campaign_id": opaque_id("cmp"),
            "name": name,
            "status": CampaignStatus.DRAFT.value,
            "mode": CampaignMode.STANDARD.value,
            "mode_explicitly_selected": False,
            "variants": [],
            "destinations": [],
            "target_selector": {},
            "target_snapshot": [],
            "protected_destination_ids": [],
            "cohort_map": {},
            # This default is explicit so a choice made before scheduling is
            # not lost when the schedule is later saved.
            "delete_on_end": True,
            "delete_on_next_campaign": False,
            "created_by": owner_id,
            "created_at": now,
            "updated_at": now,
            "version": 1,
        }
        await self.repositories.create_campaign(campaign)
        return campaign

    async def rename_draft(self, campaign_id: str, name: str) -> Document:
        campaign = await self._draft(campaign_id)
        campaign["name"] = self._campaign_name(name)
        campaign["updated_at"] = utcnow()
        await self.repositories.update_campaign(campaign_id, {"name": campaign["name"], "updated_at": campaign["updated_at"]})
        return campaign

    @staticmethod
    def _campaign_name(value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Campaign name cannot be empty.")
        if len(name) > 100:
            raise ValueError("Campaign name must be 100 characters or fewer.")
        return name

    async def configure_draft(
        self,
        campaign_id: str,
        *,
        variants: list[Creative],
        destinations: list[Destination],
        selector: Document,
        mode: CampaignMode,
        schedule: Schedule,
        preview_sent: bool,
    ) -> list[str]:
        campaign = await self._draft(campaign_id)
        active_sources = await self.repositories.active_channels(selector)
        source_ids = {channel["telegram_chat_id"] for channel in active_sources}
        errors = validate_launch(
            variants=variants,
            destinations=destinations,
            source_ids=source_ids,
            mode=mode,
            schedule=schedule,
            preview_sent=preview_sent,
            send_rps=self.send_rps,
        )
        if errors:
            return errors
        await self.repositories.update_campaign(
            campaign["campaign_id"],
            {
                "variants": [variant.model_dump(mode="json") for variant in variants],
                "destinations": [destination.model_dump(mode="json") for destination in destinations],
                "target_selector": selector,
                "mode": mode.value,
                "start_at_utc": schedule.start_at_utc,
                "original_end_at_utc": schedule.end_at_utc,
                "current_end_at_utc": schedule.end_at_utc,
                "repost_interval_seconds": schedule.repost_interval_seconds,
                "repost_offsets_seconds": schedule.repost_offsets_seconds,
                "delete_on_repost": schedule.delete_on_repost,
                "delete_on_end": schedule.delete_on_end,
                "delete_on_next_campaign": False if schedule.delete_on_end else campaign.get("delete_on_next_campaign", False),
                "owner_timezone": schedule.owner_timezone,
                "preview_sent": preview_sent,
                "updated_at": utcnow(),
            },
        )
        return []

    async def activate(self, campaign_id: str) -> Document:
        campaign = await self._draft(campaign_id)
        now = utcnow()
        if campaign.get("rerun_ready"):
            duration = timedelta(seconds=max(60, int(campaign.get("rerun_duration_seconds", 3600))))
            campaign["start_at_utc"] = now
            campaign["original_end_at_utc"] = now + duration
            campaign["current_end_at_utc"] = now + duration
        variants = [Creative.model_validate(variant) for variant in campaign["variants"]]
        destinations = [Destination.model_validate(destination) for destination in campaign["destinations"]]
        schedule = Schedule(
            start_at_utc=as_utc(campaign["start_at_utc"]),
            end_at_utc=as_utc(campaign["current_end_at_utc"]),
            repost_interval_seconds=campaign.get("repost_interval_seconds"),
            repost_offsets_seconds=campaign.get("repost_offsets_seconds"),
            delete_on_repost=campaign.get("delete_on_repost", True),
            delete_on_end=campaign.get("delete_on_end", True),
            owner_timezone=campaign.get("owner_timezone", "UTC"),
        )
        sources = await self.repositories.active_channels(campaign.get("target_selector"))
        source_ids = {source["telegram_chat_id"] for source in sources}
        protected = protected_destination_ids(destinations)
        target_snapshot = sorted(source_ids - protected)
        campaign, adjustment_notes = await self._normalize_rotation_schedule(
            campaign,
            eligible_count=len(target_snapshot),
            persist=False,
        )
        schedule = Schedule(
            start_at_utc=as_utc(campaign["start_at_utc"]),
            end_at_utc=as_utc(campaign["current_end_at_utc"]),
            repost_interval_seconds=campaign.get("repost_interval_seconds"),
            repost_offsets_seconds=campaign.get("repost_offsets_seconds"),
            delete_on_repost=campaign.get("delete_on_repost", True),
            delete_on_end=campaign.get("delete_on_end", True),
            owner_timezone=campaign.get("owner_timezone", "UTC"),
        )
        errors = validate_launch(
            variants=variants,
            destinations=destinations,
            source_ids=source_ids,
            mode=CampaignMode(campaign["mode"]),
            schedule=schedule,
            preview_sent=bool(campaign.get("preview_sent")),
            send_rps=self.send_rps,
        )
        if schedule.end_at_utc <= now:
            errors.append("Campaign end time has already passed. Set a new schedule before launching.")
        if errors:
            raise ValueError(" ".join(errors))
        cohort_seed = secrets.token_bytes(32)
        shuffle_seed = secrets.token_bytes(32)
        cohorts = cohort_map(target_snapshot, len(variants), cohort_seed)
        variant_versions = {
            variant["id"]: [{"revision": 1, "creative": deepcopy(variant), "created_at": now}]
            for variant in campaign["variants"]
        }
        variant_current_revisions = {variant["id"]: 1 for variant in campaign["variants"]}
        status = CampaignStatus.SCHEDULED if schedule.start_at_utc > now else CampaignStatus.ACTIVE
        update = {
            "status": status.value,
            "target_snapshot": target_snapshot,
            "protected_destination_ids": sorted(protected),
            "cohort_map": {str(channel_id): cohort for channel_id, cohort in cohorts.items()},
            "cohort_seed": base64.urlsafe_b64encode(cohort_seed).decode(),
            "shuffle_seed": base64.urlsafe_b64encode(shuffle_seed).decode(),
            "activated_at": now,
            "next_cycle_number": 0,
            "next_cycle_at": schedule.start_at_utc,
            "start_at_utc": schedule.start_at_utc,
            "original_end_at_utc": schedule.end_at_utc,
            "current_end_at_utc": schedule.end_at_utc,
            "repost_interval_seconds": schedule.repost_interval_seconds,
            "repost_offsets_seconds": schedule.repost_offsets_seconds,
            "rotation_adjustment_notes": adjustment_notes or campaign.get("rotation_adjustment_notes", []),
            "variant_versions": variant_versions,
            "variant_current_revisions": variant_current_revisions,
            "rerun_ready": False,
            "updated_at": now,
        }
        activated = await self.repositories.activate_draft(campaign_id, update)
        if not activated:
            raise ValueError("This campaign was already launched or changed. Open it again to see its current state.")
        campaign = activated
        # Do not make the first post depend on the next scheduler tick. This
        # creates durable cycle-0 deliveries during the owner's confirmation;
        # background scheduling remains responsible for later reposts.
        if status == CampaignStatus.ACTIVE:
            try:
                await self.plan_due_cycle(campaign, now)
            except Exception:
                # Activation is already committed atomically.  The scheduler
                # can safely retry idempotent cycle materialization, so do not
                # tell the owner that a successfully activated run failed.
                logger.exception("Immediate first-cycle planning failed; scheduler will retry", extra={"campaign_id": campaign_id})
        return await self.repositories.get_campaign(campaign_id) or campaign

    async def editable_campaign(self, campaign_id: str) -> Document:
        """Return an editable draft for owner UI actions without duplicating status checks."""
        return await self._draft(campaign_id)

    async def launch_summary(self, campaign_id: str) -> tuple[Document, list[str], int, int, int]:
        """Validate a draft and expose exact owner-facing launch counts before confirmation."""
        campaign = await self._draft(campaign_id)
        required = ("start_at_utc", "current_end_at_utc")
        missing = [field for field in required if not campaign.get(field)]
        if missing:
            return campaign, ["Set a start and end time before launch."], 0, 0, 0
        variants = [Creative.model_validate(variant) for variant in campaign.get("variants", [])]
        destinations = [Destination.model_validate(destination) for destination in campaign.get("destinations", [])]
        sources = await self.repositories.active_channels(campaign.get("target_selector"))
        source_ids = {source["telegram_chat_id"] for source in sources}
        protected = protected_destination_ids(destinations)
        campaign, _ = await self._normalize_rotation_schedule(
            campaign,
            eligible_count=len(source_ids - protected),
            persist=True,
        )
        schedule = Schedule(
            start_at_utc=as_utc(campaign["start_at_utc"]),
            end_at_utc=as_utc(campaign["current_end_at_utc"]),
            repost_interval_seconds=campaign.get("repost_interval_seconds"),
            repost_offsets_seconds=campaign.get("repost_offsets_seconds"),
            delete_on_repost=campaign.get("delete_on_repost", True),
            delete_on_end=campaign.get("delete_on_end", True),
            owner_timezone=campaign.get("owner_timezone", "UTC"),
        )
        errors = validate_launch(
            variants=variants,
            destinations=destinations,
            source_ids=source_ids,
            mode=CampaignMode(campaign["mode"]),
            schedule=schedule,
            preview_sent=bool(campaign.get("preview_sent")),
            send_rps=self.send_rps,
        )
        if schedule.end_at_utc <= utcnow():
            errors.append("Campaign end time has already passed. Set a new schedule before launching.")
        return campaign, errors, len(source_ids), len(protected), len(source_ids - protected)

    async def normalize_draft_rotation_schedule(self, campaign_id: str) -> tuple[Document, list[str]]:
        """Keep a draft's current timing compatible as variants or mode change."""
        campaign = await self._draft(campaign_id)
        if not campaign.get("start_at_utc") or not campaign.get("current_end_at_utc"):
            return campaign, []
        sources = await self.repositories.active_channels(campaign.get("target_selector"))
        destinations = [Destination.model_validate(item) for item in campaign.get("destinations", [])]
        eligible_count = len({item["telegram_chat_id"] for item in sources} - protected_destination_ids(destinations))
        return await self._normalize_rotation_schedule(campaign, eligible_count=eligible_count, persist=True)

    async def _normalize_rotation_schedule(
        self,
        campaign: Document,
        *,
        eligible_count: int,
        persist: bool,
    ) -> tuple[Document, list[str]]:
        if not campaign.get("start_at_utc") or not campaign.get("current_end_at_utc"):
            return campaign, []
        minimum_cycle_seconds = max(60, ceil(max(0, eligible_count) / self.send_rps))
        fit = fit_rotation_schedule(
            start_at=as_utc(campaign["start_at_utc"]),
            end_at=as_utc(campaign["current_end_at_utc"]),
            interval_seconds=campaign.get("repost_interval_seconds"),
            repost_offsets_seconds=campaign.get("repost_offsets_seconds"),
            mode=campaign.get("mode", CampaignMode.STANDARD.value),
            variant_count=len(campaign.get("variants", [])),
            minimum_cycle_seconds=minimum_cycle_seconds,
        )
        changed = (
            as_utc(campaign["current_end_at_utc"]) != fit.end_at
            or campaign.get("repost_interval_seconds") != fit.interval_seconds
            or campaign.get("repost_offsets_seconds") != fit.offsets_seconds
        )
        if not changed:
            return campaign, []
        existing_notes = list(campaign.get("rotation_adjustment_notes", []))
        notes = list(dict.fromkeys([*existing_notes, *fit.notes]))
        update = {
            "original_end_at_utc": fit.end_at,
            "current_end_at_utc": fit.end_at,
            "repost_interval_seconds": fit.interval_seconds,
            "repost_offsets_seconds": fit.offsets_seconds,
            "rotation_adjustment_notes": notes,
            "rotation_adjusted_at": utcnow(),
            "updated_at": utcnow(),
        }
        campaign.update(update)
        if persist:
            await self.repositories.update_campaign(campaign["campaign_id"], update)
        return campaign, list(fit.notes)

    async def plan_due_cycle(self, campaign: Document, now: Any) -> bool:
        """Materialize the one due cycle. Its fixed HMAC ranks make restart ordering reproducible."""
        if campaign["status"] not in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value}:
            return False
        start = as_utc(campaign["start_at_utc"])
        end = as_utc(campaign["current_end_at_utc"])
        # A scale-to-zero host cannot tick at the requested moment.  If it
        # wakes only after the whole scheduled window elapsed and no first
        # cycle was ever materialized, preserve the owner's intent by giving
        # that untouched run an equivalent fresh window.  This is deliberately
        # limited to cycle zero; an interrupted active run uses the normal
        # durable delivery/repost recovery paths instead.
        if (
            campaign["status"] == CampaignStatus.SCHEDULED.value
            and now >= end
            and int(campaign.get("next_cycle_number", 0)) == 0
            and not await self.repositories.cycle_exists(campaign["campaign_id"], 0)
        ):
            duration = max(timedelta(minutes=1), end - start)
            recovered = await self.repositories.rebase_unstarted_scheduled_campaign(
                campaign["campaign_id"],
                campaign["start_at_utc"],
                {
                    "start_at_utc": now,
                    "original_end_at_utc": now + duration,
                    "current_end_at_utc": now + duration,
                    "schedule_recovered_at": now,
                    "schedule_delayed_by_seconds": max(0, int((now - start).total_seconds())),
                    "updated_at": now,
                },
            )
            if recovered:
                campaign = recovered
                start = as_utc(campaign["start_at_utc"])
                end = as_utc(campaign["current_end_at_utc"])
        if now < start:
            return False
        if now >= end:
            await self.repositories.mark_campaign_ending(campaign["campaign_id"], "schedule_complete")
            return False
        cycle_number = int(campaign.get("next_cycle_number", 0))
        repost_offsets = campaign.get("repost_offsets_seconds")
        safety_refresh = await self._cleanup_safety_refresh_due(campaign, now, end)
        # After a one-off or final specific repost, keep the campaign active
        # until its configured end so cleanup can run. There is simply no
        # further cycle to plan before then, unless a confirmed live post is
        # nearing Telegram's hard deletion age and must be refreshed first.
        if (repost_offsets is not None and cycle_number > len(repost_offsets)) or (
            repost_offsets is None and not campaign.get("repost_interval_seconds") and cycle_number > 0
        ):
            if not safety_refresh:
                return False
            expected = now
        else:
            expected = scheduled_cycle_time(start, cycle_number, campaign.get("repost_interval_seconds"), repost_offsets)
        if not safety_refresh and now < expected:
            return False
        if not safety_refresh and not can_create_cycle(start, end, cycle_number, campaign.get("repost_interval_seconds"), repost_offsets):
            await self.repositories.mark_campaign_ending(campaign["campaign_id"], "schedule_complete")
            return False
        if safety_refresh:
            expected = now
        seed = base64.urlsafe_b64decode(campaign["shuffle_seed"])
        variant_count = len(campaign["variants"])
        deliveries: list[Document] = []
        for channel_id in campaign["target_snapshot"]:
            cohort_index = int(campaign["cohort_map"][str(channel_id)])
            selected_variant_index = variant_for(campaign["mode"], cycle_number, cohort_index, variant_count)
            selected_variant = campaign["variants"][selected_variant_index]
            variant_id = selected_variant.get("id")
            revision = int(campaign.get("variant_current_revisions", {}).get(variant_id, 1))
            deliveries.append(
                {
                    "campaign_id": campaign["campaign_id"],
                    "cycle_number": cycle_number,
                    "channel_id": channel_id,
                    "cohort_index": cohort_index,
                    "variant_index": selected_variant_index,
                    "variant_id": variant_id,
                    "variant_revision": revision,
                    "dispatch_rank": dispatch_rank(seed, cycle_number, channel_id),
                    "status": "PENDING",
                    "previous_message_id": None,
                    "sent_message_ids": [],
                    "attempts": 0,
                    "worker_id": None,
                    "lease_until": None,
                    "next_retry_at": None,
                    "safety_refresh": safety_refresh,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        await self.repositories.create_cycle(
            {
                "campaign_id": campaign["campaign_id"],
                "cycle_number": cycle_number,
                "scheduled_at_utc": expected,
                "created_at": now,
                "started_at": now,
                "completed_at": None,
                "status": "RUNNING",
                "target_count": len(deliveries),
            },
            deliveries,
        )
        interval = campaign.get("repost_interval_seconds")
        if safety_refresh:
            next_cycle, next_cycle_at = self._next_cycle_after_now(
                start,
                end,
                cycle_number,
                interval,
                repost_offsets,
                now,
            )
            await self.repositories.advance_running_campaign(
                campaign["campaign_id"],
                {
                    "status": CampaignStatus.ACTIVE.value,
                    "next_cycle_number": next_cycle,
                    "next_cycle_at": next_cycle_at,
                    "cleanup_safety_refresh_count": int(campaign.get("cleanup_safety_refresh_count", 0)) + 1,
                    "cleanup_safety_last_refresh_at": now,
                    "updated_at": now,
                },
            )
        elif repost_offsets is not None:
            next_cycle = cycle_number + 1
            next_cycle_at = scheduled_cycle_time(start, next_cycle, interval, repost_offsets) if next_cycle <= len(repost_offsets) else end
            await self.repositories.advance_running_campaign(
                campaign["campaign_id"],
                {
                    "status": CampaignStatus.ACTIVE.value,
                    "next_cycle_number": next_cycle,
                    "next_cycle_at": next_cycle_at,
                    "updated_at": now,
                },
            )
        elif interval:
            await self.repositories.advance_running_campaign(
                campaign["campaign_id"],
                {
                    "status": CampaignStatus.ACTIVE.value,
                    "next_cycle_number": cycle_number + 1,
                    "next_cycle_at": expected + timedelta(seconds=interval),
                    "updated_at": now,
                },
            )
        else:
            # Single-post campaigns remain active until their end-time cleanup.
            await self.repositories.advance_running_campaign(
                campaign["campaign_id"],
                {
                    "status": CampaignStatus.ACTIVE.value,
                    "next_cycle_number": cycle_number + 1,
                    "next_cycle_at": end,
                    "updated_at": now,
                },
            )
        return True

    async def _cleanup_safety_refresh_due(self, campaign: Document, now: Any, end: Any) -> bool:
        """Return whether an ageing live post needs an early replacement.

        The normal schedule is preferred. This only intervenes if the planned
        campaign end lies beyond the oldest live post's safe deletion deadline
        and that deadline is within the two-hour dispatch buffer.
        """
        if not campaign.get("delete_on_end", True):
            return False
        oldest_live_at = await self.repositories.oldest_live_state_updated_at(campaign["campaign_id"])
        if not oldest_live_at:
            return False
        safe_deadline = as_utc(oldest_live_at) + SAFE_DELETE_WINDOW
        return as_utc(end) > safe_deadline and now >= safe_deadline - _CLEANUP_REFRESH_LEAD

    @staticmethod
    def _next_cycle_after_now(
        start: Any,
        end: Any,
        cycle_number: int,
        interval: int | None,
        repost_offsets: list[int] | None,
        now: Any,
    ) -> tuple[int, Any]:
        """Skip stale scheduled reposts after a safety refresh.

        Catching every missed cycle after a long outage would immediately
        replace a newly refreshed post several times and overload the channel
        network. Resume at the first planned cycle that is still in the future.
        """
        next_cycle = cycle_number + 1
        if repost_offsets is not None:
            while next_cycle <= len(repost_offsets):
                next_at = scheduled_cycle_time(start, next_cycle, interval, repost_offsets)
                if next_at > now:
                    return next_cycle, next_at
                next_cycle += 1
            return next_cycle, end
        if interval:
            elapsed = max(0, int((now - start).total_seconds()))
            next_cycle = max(next_cycle, elapsed // int(interval) + 1)
            next_at = scheduled_cycle_time(start, next_cycle, interval, None)
            return next_cycle, next_at if next_at < end else end
        return next_cycle, end

    async def replace_running_variant(
        self,
        campaign_id: str,
        index: int,
        replacement: Creative,
        owner_id: int,
    ) -> tuple[Document, int, bool]:
        """Replace one stable variant identity for future, not-yet-planned cycles."""
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign.get("status") not in {CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}:
            raise ValueError("Only active or paused campaigns support live variant replacement.")
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        if index < 0 or index >= len(variants):
            raise ValueError("That variant no longer exists.")
        original = variants[index]
        replacement.id = original.id
        replacement.buttons = original.buttons
        replacement.button_layout = original.button_layout

        current_revisions = dict(campaign.get("variant_current_revisions", {}))
        version_history = deepcopy(campaign.get("variant_versions", {}))
        if original.id not in current_revisions or original.id not in version_history:
            current_revisions[original.id] = 1
            version_history[original.id] = [
                {"revision": 1, "creative": original.model_dump(mode="json"), "created_at": campaign.get("activated_at") or utcnow()}
            ]
            initialized = {
                "variant_current_revisions": current_revisions,
                "variant_versions": version_history,
                "updated_at": utcnow(),
            }
            await self.repositories.update_campaign(campaign_id, initialized)
            campaign.update(initialized)

        current_revision = int(current_revisions[original.id])
        next_revision = current_revision + 1
        schedule_update, _refactor_note = self._ensure_future_rotation_pass(campaign)
        changed = await self.repositories.replace_running_variant(
            campaign_id=campaign_id,
            index=index,
            variant_id=original.id,
            expected_revision=current_revision,
            revision=next_revision,
            creative=replacement.model_dump(mode="json"),
            event={
                "at": utcnow(),
                "owner_id": owner_id,
                "variant_id": original.id,
                "variant_index": index,
                "from_revision": current_revision,
                "to_revision": next_revision,
                "applies_from_cycle": int(campaign.get("next_cycle_number", 0)),
                "schedule_refactored": bool(schedule_update),
            },
            schedule_update=schedule_update,
        )
        if not changed:
            raise ValueError("That variant changed while you were editing it. Open Variants and try again.")
        updated = await self.repositories.get_campaign(campaign_id)
        if not updated:
            raise ValueError("Campaign no longer exists.")
        applies_from_cycle = int(updated.get("next_cycle_number", 0))
        total_cycles = scheduled_cycle_count(
            as_utc(updated["start_at_utc"]),
            as_utc(updated["current_end_at_utc"]),
            updated.get("repost_interval_seconds"),
            updated.get("repost_offsets_seconds"),
        )
        return updated, applies_from_cycle, applies_from_cycle < total_cycles

    def _ensure_future_rotation_pass(self, campaign: Document) -> tuple[Document, str | None]:
        """Extend a live rotating run so an edited revision can finish one pass."""
        variants = campaign.get("variants", [])
        if campaign.get("mode") == CampaignMode.STANDARD.value or len(variants) < 2:
            return {}, None
        start = as_utc(campaign["start_at_utc"])
        end = as_utc(campaign["current_end_at_utc"])
        interval = campaign.get("repost_interval_seconds")
        offsets = deepcopy(campaign.get("repost_offsets_seconds"))
        next_cycle = int(campaign.get("next_cycle_number", 0))
        total_cycles = scheduled_cycle_count(start, end, interval, offsets)
        missing_cycles = len(variants) - max(0, total_cycles - next_cycle)
        if missing_cycles <= 0:
            return {}, None

        minimum_cycle_seconds = max(60, ceil(len(campaign.get("target_snapshot", [])) / self.send_rps))
        maximum_safe_gap = int(SAFE_DELETE_WINDOW.total_seconds())
        update: Document = {}
        if offsets is not None:
            points = [0, *offsets]
            if len(points) >= 2:
                step = points[-1] - points[-2]
            else:
                step = max(minimum_cycle_seconds, int((end - start).total_seconds()) // max(1, total_cycles))
            step = max(minimum_cycle_seconds, min(step, maximum_safe_gap))
            cursor = points[-1]
            for _ in range(missing_cycles):
                cursor += step
                offsets.append(cursor)
            new_end = max(end, start + timedelta(seconds=cursor + minimum_cycle_seconds))
            update.update({"repost_offsets_seconds": offsets, "current_end_at_utc": new_end})
        else:
            cadence = max(minimum_cycle_seconds, min(int(interval or minimum_cycle_seconds), maximum_safe_gap))
            required_end = start + timedelta(seconds=(next_cycle + len(variants) - 1) * cadence + minimum_cycle_seconds)
            update.update(
                {
                    "repost_interval_seconds": cadence,
                    "current_end_at_utc": max(end, required_end),
                }
            )

        note = (
            f"live Variant replacement added enough time for {len(variants)} future rotation cycles, "
            "so the new revision can reach every frozen target"
        )
        update["rotation_adjustment_notes"] = list(
            dict.fromkeys([*campaign.get("rotation_adjustment_notes", []), note])
        )
        update["rotation_adjusted_at"] = utcnow()
        return update, note

    async def extend(self, campaign_id: str, owner_id: int, seconds: int) -> Document:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] not in {"SCHEDULED", "ACTIVE"}:
            raise ValueError("Only scheduled or active campaigns can be extended.")
        if seconds <= 0:
            raise ValueError("Extension must be positive.")
        new_end = as_utc(campaign["current_end_at_utc"]) + timedelta(seconds=seconds)
        cleanup_still_safe = self._cleanup_safe_after_extension(campaign, new_end)
        retention_adjusted = bool(campaign.get("delete_on_end", True)) and not cleanup_still_safe
        event = {
            "at": utcnow(),
            "owner_id": owner_id,
            "seconds": seconds,
            "new_end_at_utc": new_end,
            "retention_adjusted": retention_adjusted,
        }
        update: Document = {"current_end_at_utc": new_end, "updated_at": utcnow()}
        if retention_adjusted:
            update.update({"delete_on_end": False, "delete_on_next_campaign": True})
        extended = await self.repositories.extend_running_campaign(campaign_id, update, event)
        if not extended:
            raise ValueError("The campaign changed state before it could be extended. Open it again to see its current state.")
        return extended

    @staticmethod
    def _cleanup_safe_after_extension(campaign: Document, new_end: Any) -> bool:
        start = as_utc(campaign["start_at_utc"])
        offsets = campaign.get("repost_offsets_seconds")
        if offsets is not None:
            last_post = start + timedelta(seconds=int(offsets[-1])) if offsets else start
            return as_utc(new_end) - last_post <= SAFE_DELETE_WINDOW
        interval = campaign.get("repost_interval_seconds")
        if interval:
            return timedelta(seconds=int(interval)) <= SAFE_DELETE_WINDOW
        return as_utc(new_end) - start <= SAFE_DELETE_WINDOW

    async def end_early(self, campaign_id: str) -> bool:
        # Owner-requested stop always means stop and delete this campaign's
        # known live messages, even if its normal end behavior was "keep".
        return await self.repositories.end_campaign_early(campaign_id)

    async def pause(self, campaign_id: str, owner_id: int) -> Document:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] not in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value}:
            raise ValueError("Only active or scheduled campaigns can be paused.")
        now = utcnow()
        # Hide the campaign from workers/scheduler first. Any delivery claimed
        # concurrently will then return itself to PAUSED instead of sending.
        paused = await self.repositories.pause_running_campaign(
            campaign_id,
            {
                "status": CampaignStatus.PAUSED.value,
                "paused_at": now,
                "paused_by": owner_id,
                "resume_recovery_needed": False,
                "updated_at": now,
            },
        )
        if not paused:
            raise ValueError("The campaign changed state before it could be paused. Open it again to see its current state.")
        await self.repositories.pause_campaign_deliveries(campaign_id)
        return paused

    async def resume(self, campaign_id: str, owner_id: int) -> Document:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] != CampaignStatus.PAUSED.value:
            raise ValueError("Only paused campaigns can be resumed.")
        now = utcnow()
        paused_at = as_utc(campaign["paused_at"])
        freeze_seconds = max(0, int((now - paused_at).total_seconds()))
        start = as_utc(campaign["start_at_utc"]) + timedelta(seconds=freeze_seconds)
        end = as_utc(campaign["current_end_at_utc"]) + timedelta(seconds=freeze_seconds)
        next_cycle_at = as_utc(campaign["next_cycle_at"]) + timedelta(seconds=freeze_seconds) if campaign.get("next_cycle_at") else None
        status = CampaignStatus.SCHEDULED if start > now else CampaignStatus.ACTIVE
        # Publish the resumed status before making queued jobs claimable. This
        # avoids a worker seeing PAUSED and immediately parking a resumed job.
        resumed = await self.repositories.resume_paused_campaign(
            campaign_id,
            {
                "status": status.value,
                "start_at_utc": start,
                "current_end_at_utc": end,
                "next_cycle_at": next_cycle_at,
                "paused_at": None,
                "last_resumed_by": owner_id,
                "last_freeze_seconds": freeze_seconds,
                "resume_recovery_needed": True,
                "updated_at": now,
            },
        )
        if not resumed:
            raise ValueError("The campaign was already resumed or changed. Open it again to see its current state.")
        await self.repositories.resume_campaign_deliveries(campaign_id)
        await self.repositories.advance_running_campaign(campaign_id, {"resume_recovery_needed": False, "updated_at": now})
        resumed["resume_recovery_needed"] = False
        return resumed

    async def return_to_draft(self, campaign_id: str, owner_id: int) -> Document:
        """Make a not-yet-started schedule editable again without losing its setup."""
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] != CampaignStatus.SCHEDULED.value:
            raise ValueError("Only a scheduled campaign that has not started can return to draft.")
        now = utcnow()
        update = {
            "status": CampaignStatus.DRAFT.value,
            "target_snapshot": [],
            "protected_destination_ids": [],
            "cohort_map": {},
            "cohort_seed": None,
            "shuffle_seed": None,
            "activated_at": None,
            "next_cycle_number": None,
            "next_cycle_at": None,
            "returned_to_draft_by": owner_id,
            "returned_to_draft_at": now,
            "updated_at": now,
        }
        result = await self.repositories.return_scheduled_to_draft(campaign_id, update)
        if not result:
            raise ValueError("The campaign started before it could return to draft. Open it again to see its state.")
        return result

    async def duplicate(self, campaign_id: str, owner_id: int) -> Document:
        """Create a fully configured editable successor of archived history."""
        original = await self.repositories.get_campaign(campaign_id)
        if not original or original["status"] != CampaignStatus.ARCHIVED.value:
            raise ValueError("Only archived campaigns can be duplicated.")
        copied = self._derived_draft(original, owner_id, name=f"{original['name']} (copy)")
        await self.repositories.create_campaign(copied)
        return copied

    async def prepare_rerun(self, campaign_id: str, owner_id: int) -> Document:
        """Prepare a one-confirmation rerun with the previous definition intact."""
        original = await self.repositories.get_campaign(campaign_id)
        if not original or original["status"] != CampaignStatus.ARCHIVED.value:
            raise ValueError("Only an archived campaign can be run again.")
        now = utcnow()
        pending = await self.repositories.recent_pending_rerun(campaign_id, owner_id, now - timedelta(minutes=10))
        if pending:
            return pending
        copied = self._derived_draft(original, owner_id, name=original["name"])
        copied["rerun_of_campaign_id"] = original["campaign_id"]
        copied["rerun_ready"] = True
        copied["rerun_duration_seconds"] = max(60, int((as_utc(copied["current_end_at_utc"]) - as_utc(copied["start_at_utc"])).total_seconds()))
        await self.repositories.create_campaign(copied)
        return copied

    async def fork_to_draft(self, campaign_id: str, owner_id: int) -> Document:
        """Create an editable successor without mutating a live campaign's history."""
        original = await self.repositories.get_campaign(campaign_id)
        if not original:
            raise ValueError("Campaign no longer exists.")
        copied = self._derived_draft(original, owner_id, name=f"{original['name']} (edited copy)")
        await self.repositories.create_campaign(copied)
        return copied

    @staticmethod
    def _fresh_window(original: Document, now: Any) -> tuple[Any, Any]:
        start_value = original.get("start_at_utc")
        end_value = original.get("current_end_at_utc") or original.get("original_end_at_utc")
        duration = timedelta(hours=1)
        if start_value and end_value:
            duration = max(timedelta(minutes=1), as_utc(end_value) - as_utc(start_value))
        return now, now + duration

    def _derived_draft(self, original: Document, owner_id: int, *, name: str) -> Document:
        now = utcnow()
        start, end = self._fresh_window(original, now)
        variants = deepcopy(original.get("variants", []))
        mode = original.get("mode", CampaignMode.STANDARD.value)
        if len(variants) < 2:
            mode = CampaignMode.STANDARD.value
        return {
            "campaign_id": opaque_id("cmp"),
            "name": name,
            "status": CampaignStatus.DRAFT.value,
            "mode": mode,
            "mode_explicitly_selected": bool(original.get("mode_explicitly_selected", mode != CampaignMode.STANDARD.value)),
            "variants": variants,
            "destinations": deepcopy(original.get("destinations", [])),
            "target_selector": deepcopy(original.get("target_selector", {})),
            "target_snapshot": [],
            "protected_destination_ids": [],
            "cohort_map": {},
            "start_at_utc": start,
            "original_end_at_utc": end,
            "current_end_at_utc": end,
            "repost_interval_seconds": original.get("repost_interval_seconds"),
            "repost_offsets_seconds": deepcopy(original.get("repost_offsets_seconds")),
            "delete_on_repost": original.get("delete_on_repost", True),
            "delete_on_end": original.get("delete_on_end", True),
            "delete_on_next_campaign": original.get("delete_on_next_campaign", False),
            "owner_timezone": original.get("owner_timezone", "UTC"),
            # The exact saved definition has already been rendered by the
            # source run. Any creative/CTA edit resets this flag.
            "preview_sent": bool(variants),
            "created_by": owner_id,
            "created_at": now,
            "updated_at": now,
            "derived_from_campaign_id": original["campaign_id"],
            "version": 1,
        }

    async def delete_draft(self, campaign_id: str) -> bool:
        return await self.repositories.delete_draft_campaign(campaign_id)

    async def _draft(self, campaign_id: str) -> Document:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] != CampaignStatus.DRAFT.value:
            raise ValueError("Campaign must be an editable draft.")
        return campaign
