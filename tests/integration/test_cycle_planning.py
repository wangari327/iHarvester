import base64
from datetime import UTC, datetime, timedelta

import pytest

from app.campaigns.models import Button, Creative, Destination
from app.campaigns.scheduling import scheduled_cycle_count
from app.campaigns.service import CampaignService
from app.campaigns.shuffle import cohort_map


class MemoryRepositories:
    def __init__(self, campaign, sources):
        self.campaign = campaign
        self.sources = sources
        self.cycles = []
        self.deliveries = []
        self.ending = []
        self.paused_deliveries = 0
        self.resumed_deliveries = 0
        self.oldest_live_at = None

    async def active_channels(self, selector):
        return self.sources

    async def get_campaign(self, campaign_id):
        return self.campaign if campaign_id == self.campaign["campaign_id"] else None

    async def oldest_live_state_updated_at(self, campaign_id):
        assert campaign_id == self.campaign["campaign_id"]
        return self.oldest_live_at

    async def create_cycle(self, cycle, deliveries):
        if not any(existing["cycle_number"] == cycle["cycle_number"] for existing in self.cycles):
            self.cycles.append(cycle)
        existing = {(row["cycle_number"], row["channel_id"]) for row in self.deliveries}
        self.deliveries.extend(row for row in deliveries if (row["cycle_number"], row["channel_id"]) not in existing)
        return True

    async def cycle_exists(self, campaign_id, cycle_number):
        return any(cycle["cycle_number"] == cycle_number for cycle in self.cycles)

    async def update_campaign(self, campaign_id, update):
        self.campaign.update(update)
        return True

    async def advance_running_campaign(self, campaign_id, update):
        if self.campaign.get("status") not in {"SCHEDULED", "ACTIVE"}:
            return False
        self.campaign.update(update)
        return True

    async def activate_draft(self, campaign_id, update):
        if self.campaign.get("status") != "DRAFT":
            return None
        self.campaign.update(update)
        return self.campaign

    async def rebase_unstarted_scheduled_campaign(self, campaign_id, expected_start, update):
        if (
            self.campaign.get("status") != "SCHEDULED"
            or self.campaign.get("start_at_utc") != expected_start
            or self.campaign.get("next_cycle_number") != 0
        ):
            return None
        self.campaign.update(update)
        return self.campaign

    async def return_scheduled_to_draft(self, campaign_id, update):
        if self.campaign.get("status") != "SCHEDULED":
            return None
        self.campaign.update(update)
        return self.campaign

    async def pause_running_campaign(self, campaign_id, update):
        if self.campaign.get("status") not in {"SCHEDULED", "ACTIVE"}:
            return None
        self.campaign.update(update)
        return self.campaign

    async def resume_paused_campaign(self, campaign_id, update):
        if self.campaign.get("status") != "PAUSED":
            return None
        self.campaign.update(update)
        return self.campaign

    async def extend_running_campaign(self, campaign_id, update, event):
        if self.campaign.get("status") not in {"SCHEDULED", "ACTIVE"}:
            return None
        self.campaign.update(update)
        self.campaign.setdefault("extensions", []).append(event)
        return self.campaign

    async def replace_running_variant(
        self,
        *,
        campaign_id,
        index,
        variant_id,
        expected_revision,
        revision,
        creative,
        event,
        schedule_update=None,
    ):
        if self.campaign.get("status") not in {"ACTIVE", "PAUSED"}:
            return False
        if self.campaign["variants"][index]["id"] != variant_id:
            return False
        if self.campaign.get("variant_current_revisions", {}).get(variant_id) != expected_revision:
            return False
        self.campaign["variants"][index] = creative
        self.campaign["variant_current_revisions"][variant_id] = revision
        self.campaign["variant_versions"][variant_id].append(
            {"revision": revision, "creative": creative, "created_at": event["at"]}
        )
        self.campaign.setdefault("variant_edit_events", []).append(event)
        self.campaign.update(schedule_update or {})
        return True

    async def end_campaign_early(self, campaign_id):
        if self.campaign.get("status") not in {"SCHEDULED", "ACTIVE", "PAUSED"}:
            return False
        self.campaign.update({"status": "ENDING", "delete_on_end": True, "delete_on_next_campaign": False})
        return True

    async def mark_campaign_ending(self, campaign_id, reason):
        self.ending.append(reason)
        return True

    async def pause_campaign_deliveries(self, campaign_id):
        self.paused_deliveries += 1
        return 0

    async def resume_campaign_deliveries(self, campaign_id):
        self.resumed_deliveries += 1
        return 0


@pytest.mark.asyncio
async def test_2000_channel_cycle_is_bounded_excludes_destination_and_changes_order() -> None:
    now = datetime.now(UTC)
    source_ids = list(range(-100000, -98000))
    protected = source_ids[0]
    target_ids = source_ids[1:]
    seed = b"x" * 32
    cohorts = cohort_map(target_ids, 3, b"c" * 32)
    campaign = {
        "campaign_id": "cmp_test",
        "status": "ACTIVE",
        "mode": "MIX_ROTATE",
        "start_at_utc": now - timedelta(seconds=1),
        "current_end_at_utc": now + timedelta(hours=3),
        "repost_interval_seconds": 3600,
        "target_snapshot": target_ids,
        "cohort_map": {str(key): value for key, value in cohorts.items()},
        "shuffle_seed": base64.urlsafe_b64encode(seed).decode(),
        "variants": [{}, {}, {}],
        "next_cycle_number": 0,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": channel_id} for channel_id in source_ids])
    service = CampaignService(repositories, send_rps=20)
    assert await service.plan_due_cycle(campaign, now)
    assert len(repositories.deliveries) == 1_999
    assert protected not in {delivery["channel_id"] for delivery in repositories.deliveries}
    first_order = [item["channel_id"] for item in sorted(repositories.deliveries, key=lambda item: item["dispatch_rank"])]
    assert await service.plan_due_cycle(campaign, now + timedelta(hours=1, seconds=1))
    second = [item for item in repositories.deliveries if item["cycle_number"] == 1]
    second_order = [item["channel_id"] for item in sorted(second, key=lambda item: item["dispatch_rank"])]
    assert first_order != second_order
    assert max(delivery["cohort_index"] for delivery in second) == 2


@pytest.mark.asyncio
async def test_scheduler_refreshes_an_ageing_post_before_telegram_cleanup_expires() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=45)
    campaign = {
        "campaign_id": "cmp_cleanup_safety",
        "status": "ACTIVE",
        "mode": "STANDARD",
        "start_at_utc": start,
        "current_end_at_utc": now + timedelta(hours=4),
        "repost_interval_seconds": 47 * 60 * 60,
        "target_snapshot": [-1001],
        "cohort_map": {"-1001": 0},
        "shuffle_seed": base64.urlsafe_b64encode(b"x" * 32).decode(),
        "variants": [{"id": "var_1", "kind": "TEXT", "text": "Fresh post"}],
        "next_cycle_number": 1,
        "delete_on_end": True,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": -1001}])
    repositories.oldest_live_at = now - timedelta(hours=45)

    assert await CampaignService(repositories, send_rps=20).plan_due_cycle(campaign, now)

    assert repositories.deliveries[0]["cycle_number"] == 1
    assert repositories.deliveries[0]["safety_refresh"] is True
    assert repositories.cycles[0]["scheduled_at_utc"] == now
    assert campaign["cleanup_safety_refresh_count"] == 1
    # The previous scheduled cycle was deliberately consumed by the fresh
    # safety post, so stale missed reposts are not dumped into the network.
    assert campaign["next_cycle_number"] == 2
    assert campaign["next_cycle_at"] == campaign["current_end_at_utc"]


@pytest.mark.asyncio
async def test_launch_summary_shows_exact_source_and_protected_destination_counts() -> None:
    now = datetime.now(UTC)
    creative = Creative(
        id="var_1",
        kind="TEXT",
        text="Hello",
        buttons=[Button(id="btn_1", text="Join", url="https://t.me/example")],
    )
    campaign = {
        "campaign_id": "cmp_summary",
        "status": "DRAFT",
        "mode": "STANDARD",
        "variants": [creative.model_dump(mode="json")],
        "destinations": [Destination(display_name="Promoted", telegram_chat_id=-1002).model_dump(mode="json")],
        "target_selector": {},
        "start_at_utc": now + timedelta(minutes=1),
        "current_end_at_utc": now + timedelta(hours=2),
        "repost_interval_seconds": None,
        "delete_on_repost": True,
        "delete_on_end": True,
        "owner_timezone": "UTC",
        "preview_sent": True,
    }
    repositories = MemoryRepositories(
        campaign,
        [
            {"telegram_chat_id": -1001},
            {"telegram_chat_id": -1002},
            {"telegram_chat_id": -1003},
        ],
    )
    _, errors, source_count, protected_count, eligible_count = await CampaignService(repositories, send_rps=20).launch_summary("cmp_summary")
    assert not errors
    assert (source_count, protected_count, eligible_count) == (3, 1, 2)


@pytest.mark.asyncio
async def test_launch_summary_auto_fits_rotation_to_cover_every_variant() -> None:
    now = datetime.now(UTC)
    variants = [Creative(id=f"var_{index}", kind="TEXT", text=f"Variant {index}").model_dump(mode="json") for index in range(3)]
    campaign = {
        "campaign_id": "cmp_fit_rotation",
        "status": "DRAFT",
        "mode": "MIX_ROTATE",
        "variants": variants,
        "destinations": [],
        "target_selector": {},
        "start_at_utc": now + timedelta(minutes=1),
        "current_end_at_utc": now + timedelta(minutes=16),
        "repost_interval_seconds": 10 * 60,
        "repost_offsets_seconds": None,
        "delete_on_repost": True,
        "delete_on_end": True,
        "preview_sent": True,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": -1001}])

    fitted, errors, *_ = await CampaignService(repositories, send_rps=20).launch_summary(campaign["campaign_id"])

    assert not errors
    assert fitted["repost_interval_seconds"] == 5 * 60
    assert fitted["rotation_adjustment_notes"]


@pytest.mark.asyncio
async def test_planned_deliveries_freeze_variant_revision_across_live_replacement() -> None:
    now = datetime.now(UTC)
    old = Creative(id="var_1", kind="TEXT", text="Old post").model_dump(mode="json")
    other = Creative(id="var_2", kind="TEXT", text="Other").model_dump(mode="json")
    campaign = {
        "campaign_id": "cmp_live_variant",
        "status": "ACTIVE",
        "mode": "ROTATE",
        "variants": [old, other],
        "start_at_utc": now - timedelta(seconds=1),
        "current_end_at_utc": now + timedelta(minutes=9),
        "repost_interval_seconds": 5 * 60,
        "repost_offsets_seconds": None,
        "target_snapshot": [-1001],
        "cohort_map": {"-1001": 0},
        "shuffle_seed": base64.urlsafe_b64encode(b"x" * 32).decode(),
        "variant_versions": {
            "var_1": [{"revision": 1, "creative": old, "created_at": now}],
            "var_2": [{"revision": 1, "creative": other, "created_at": now}],
        },
        "variant_current_revisions": {"var_1": 1, "var_2": 1},
        "next_cycle_number": 0,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": -1001}])
    service = CampaignService(repositories, send_rps=20)

    assert await service.plan_due_cycle(campaign, now)
    first_delivery = repositories.deliveries[0]
    updated, applies_from, has_future = await service.replace_running_variant(
        campaign["campaign_id"],
        0,
        Creative(id="temporary", kind="TEXT", text="New post"),
        owner_id=7,
    )

    assert first_delivery["variant_id"] == "var_1"
    assert first_delivery["variant_revision"] == 1
    assert updated["variant_current_revisions"]["var_1"] == 2
    assert updated["variants"][0]["id"] == "var_1"
    assert updated["variants"][0]["text"] == "New post"
    assert updated["current_end_at_utc"] > now + timedelta(minutes=9)
    assert any("future rotation cycles" in note for note in updated["rotation_adjustment_notes"])
    assert applies_from == 1
    assert has_future
    assert (
        scheduled_cycle_count(
            updated["start_at_utc"],
            updated["current_end_at_utc"],
            updated["repost_interval_seconds"],
            updated["repost_offsets_seconds"],
        )
        - applies_from
        >= len(updated["variants"])
    )


@pytest.mark.asyncio
async def test_activate_accepts_legacy_naive_mongo_timestamps() -> None:
    now = datetime.now(UTC)
    creative = Creative(id="var_1", kind="TEXT", text="Hello")
    campaign = {
        "campaign_id": "cmp_naive_dates",
        "status": "DRAFT",
        "mode": "STANDARD",
        "variants": [creative.model_dump(mode="json")],
        "destinations": [],
        "target_selector": {},
        # PyMongo returned this form before tz_aware=True was configured.
        "start_at_utc": (now - timedelta(minutes=1)).replace(tzinfo=None),
        "current_end_at_utc": (now + timedelta(hours=1)).replace(tzinfo=None),
        "repost_interval_seconds": None,
        "delete_on_repost": True,
        "delete_on_end": True,
        "preview_sent": True,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": -1001}])
    activated = await CampaignService(repositories, send_rps=20).activate("cmp_naive_dates")
    assert activated["status"] == "ACTIVE"
    assert activated["target_snapshot"] == [-1001]
    assert len(repositories.cycles) == 1
    assert len(repositories.deliveries) == 1
    with pytest.raises(ValueError, match="editable draft"):
        await CampaignService(repositories, send_rps=20).activate("cmp_naive_dates")
    assert len(repositories.cycles) == 1


@pytest.mark.asyncio
async def test_specific_repost_times_create_only_the_requested_cycles() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(seconds=1)
    campaign = {
        "campaign_id": "cmp_specific_times",
        "status": "ACTIVE",
        "mode": "STANDARD",
        "start_at_utc": start,
        "current_end_at_utc": start + timedelta(days=7),
        "repost_interval_seconds": None,
        "repost_offsets_seconds": [3600, 4 * 24 * 3600],
        "target_snapshot": [-1001],
        "cohort_map": {"-1001": 0},
        "shuffle_seed": base64.urlsafe_b64encode(b"x" * 32).decode(),
        "variants": [{}],
        "next_cycle_number": 0,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": -1001}])
    service = CampaignService(repositories, send_rps=20)
    assert await service.plan_due_cycle(campaign, now)
    assert await service.plan_due_cycle(campaign, start + timedelta(hours=1, seconds=1))
    assert await service.plan_due_cycle(campaign, start + timedelta(days=4, seconds=1))
    assert [cycle["cycle_number"] for cycle in repositories.cycles] == [0, 1, 2]
    assert not await service.plan_due_cycle(campaign, start + timedelta(days=5))


@pytest.mark.asyncio
async def test_pause_and_resume_hold_work_and_extend_the_campaign_window(monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    campaign = {
        "campaign_id": "cmp_pause",
        "status": "ACTIVE",
        "start_at_utc": now - timedelta(hours=1),
        "current_end_at_utc": now + timedelta(hours=1),
        "next_cycle_at": now + timedelta(minutes=15),
    }
    repositories = MemoryRepositories(campaign, [])
    service = CampaignService(repositories, send_rps=20)
    clock = iter([now, now + timedelta(minutes=30)])
    monkeypatch.setattr("app.campaigns.service.utcnow", lambda: next(clock))

    paused = await service.pause("cmp_pause", owner_id=7)
    assert paused["status"] == "PAUSED"
    resumed = await service.resume("cmp_pause", owner_id=7)

    assert resumed["status"] == "ACTIVE"
    assert resumed["current_end_at_utc"] == now + timedelta(hours=1, minutes=30)
    assert resumed["next_cycle_at"] == now + timedelta(minutes=45)
    assert repositories.paused_deliveries == repositories.resumed_deliveries == 1


@pytest.mark.asyncio
async def test_extend_preserves_cadence_and_records_audit_event() -> None:
    now = datetime.now(UTC)
    campaign = {
        "campaign_id": "cmp_extend",
        "status": "ACTIVE",
        "start_at_utc": now,
        "current_end_at_utc": now + timedelta(hours=2),
        "next_cycle_at": now + timedelta(minutes=30),
        "repost_interval_seconds": 1800,
        "delete_on_end": True,
    }
    repositories = MemoryRepositories(campaign, [])

    extended = await CampaignService(repositories, 20).extend("cmp_extend", owner_id=7, seconds=6 * 3600)

    assert extended["current_end_at_utc"] == now + timedelta(hours=8)
    assert extended["next_cycle_at"] == now + timedelta(minutes=30)
    assert extended["extensions"][0]["owner_id"] == 7
    assert extended["extensions"][0]["seconds"] == 6 * 3600


@pytest.mark.asyncio
async def test_end_early_is_atomic_and_always_selects_cleanup() -> None:
    campaign = {
        "campaign_id": "cmp_end",
        "status": "ACTIVE",
        "delete_on_end": False,
        "delete_on_next_campaign": True,
    }
    repositories = MemoryRepositories(campaign, [])
    service = CampaignService(repositories, 20)

    assert await service.end_early("cmp_end")
    assert campaign["status"] == "ENDING"
    assert campaign["delete_on_end"] is True
    assert campaign["delete_on_next_campaign"] is False
    assert not await service.end_early("cmp_end")


@pytest.mark.asyncio
async def test_cycle_advance_cannot_revive_a_concurrently_paused_campaign() -> None:
    now = datetime.now(UTC)
    campaign = {
        "campaign_id": "cmp_pause_race",
        "status": "ACTIVE",
        "mode": "STANDARD",
        "start_at_utc": now - timedelta(seconds=1),
        "current_end_at_utc": now + timedelta(hours=1),
        "repost_interval_seconds": None,
        "target_snapshot": [-1001],
        "cohort_map": {"-1001": 0},
        "shuffle_seed": base64.urlsafe_b64encode(b"x" * 32).decode(),
        "variants": [{}],
        "next_cycle_number": 0,
    }

    class PausingRepositories(MemoryRepositories):
        async def create_cycle(self, cycle, deliveries):
            created = await super().create_cycle(cycle, deliveries)
            self.campaign["status"] = "PAUSED"
            return created

    repositories = PausingRepositories(campaign, [])
    assert await CampaignService(repositories, 20).plan_due_cycle(campaign, now)
    assert repositories.campaign["status"] == "PAUSED"
    assert repositories.campaign["next_cycle_number"] == 0


@pytest.mark.asyncio
async def test_transient_immediate_cycle_failure_does_not_report_atomic_activation_as_failed() -> None:
    now = datetime.now(UTC)
    creative = Creative(id="var_1", kind="TEXT", text="Hello")
    campaign = {
        "campaign_id": "cmp_activation_recovery",
        "status": "DRAFT",
        "mode": "STANDARD",
        "variants": [creative.model_dump(mode="json")],
        "destinations": [],
        "target_selector": {},
        "start_at_utc": now - timedelta(seconds=1),
        "current_end_at_utc": now + timedelta(hours=1),
        "repost_interval_seconds": None,
        "delete_on_repost": True,
        "delete_on_end": True,
        "preview_sent": True,
    }

    class FailingCycleRepositories(MemoryRepositories):
        async def create_cycle(self, cycle, deliveries):
            raise RuntimeError("temporary MongoDB interruption")

    repositories = FailingCycleRepositories(campaign, [{"telegram_chat_id": -1001}])
    activated = await CampaignService(repositories, send_rps=20).activate(campaign["campaign_id"])

    assert activated["status"] == "ACTIVE"
    assert activated["next_cycle_number"] == 0


@pytest.mark.asyncio
async def test_unstarted_schedule_is_rebased_after_the_host_misses_its_entire_window() -> None:
    original_start = datetime(2026, 9, 1, 8, tzinfo=UTC)
    original_end = original_start + timedelta(hours=2)
    wake_time = original_end + timedelta(hours=3)
    campaign = {
        "campaign_id": "cmp_sleep_recovery",
        "status": "SCHEDULED",
        "mode": "STANDARD",
        "variants": [{"id": "var_1", "kind": "TEXT", "text": "Recovered"}],
        "start_at_utc": original_start,
        "original_end_at_utc": original_end,
        "current_end_at_utc": original_end,
        "repost_interval_seconds": None,
        "repost_offsets_seconds": None,
        "delete_on_end": False,
        "target_snapshot": [-1001],
        "cohort_map": {"-1001": 0},
        "shuffle_seed": base64.urlsafe_b64encode(b"r" * 32).decode(),
        "next_cycle_number": 0,
    }
    repositories = MemoryRepositories(campaign, [])

    planned = await CampaignService(repositories, 20).plan_due_cycle(campaign, wake_time)

    assert planned is True
    assert repositories.campaign["start_at_utc"] == wake_time
    assert repositories.campaign["current_end_at_utc"] == wake_time + timedelta(hours=2)
    assert repositories.campaign["schedule_delayed_by_seconds"] == 5 * 3600
    assert len(repositories.cycles) == 1
    assert len(repositories.deliveries) == 1
