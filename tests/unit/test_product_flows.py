from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.backups.automatic import AutomaticBackupWorker
from app.campaigns.scheduler import Scheduler
from app.campaigns.service import CampaignService
from app.delivery.worker import DeliveryWorker
from app.telegram.sender import SendResult


class DefinitionRepositories:
    def __init__(self, campaign):
        self.campaign = campaign
        self.created = None

    async def get_campaign(self, campaign_id):
        if campaign_id == self.campaign["campaign_id"]:
            return self.campaign
        return self.created if self.created and campaign_id == self.created["campaign_id"] else None

    async def create_campaign(self, campaign):
        self.created = campaign

    async def recent_pending_rerun(self, source_campaign_id, owner_id, since):
        if (
            self.created
            and self.created.get("rerun_of_campaign_id") == source_campaign_id
            and self.created.get("created_by") == owner_id
            and self.created.get("status") == "DRAFT"
            and self.created.get("rerun_ready")
            and self.created.get("created_at") >= since
        ):
            return self.created
        return None

    async def return_scheduled_to_draft(self, campaign_id, update):
        if self.campaign["status"] != "SCHEDULED":
            return None
        self.campaign.update(update)
        return self.campaign


@pytest.mark.asyncio
async def test_run_again_preserves_definition_and_moves_same_duration_to_now(monkeypatch) -> None:
    previous_start = datetime(2026, 1, 1, tzinfo=UTC)
    original = {
        "campaign_id": "cmp_old",
        "name": "Weekly promotion",
        "status": "ARCHIVED",
        "mode": "ROTATE",
        "variants": [{"id": "v1", "kind": "TEXT", "text": "Hello", "buttons": [], "button_layout": "AUTO"}],
        "destinations": [{"display_name": "Home", "raw_url": "https://t.me/home"}],
        "target_selector": {"tags_any": ["movies"]},
        "start_at_utc": previous_start,
        "current_end_at_utc": previous_start + timedelta(days=7),
        "repost_offsets_seconds": [3600, 86_400],
        "repost_interval_seconds": None,
        "delete_on_repost": True,
        "delete_on_end": False,
        "owner_timezone": "Africa/Nairobi",
    }
    original_before = deepcopy(original)
    now = datetime(2026, 2, 1, 8, tzinfo=UTC)
    monkeypatch.setattr("app.campaigns.service.utcnow", lambda: now)
    repositories = DefinitionRepositories(original)

    rerun = await CampaignService(repositories, 20).prepare_rerun("cmp_old", owner_id=7)

    assert rerun["name"] == original["name"]
    assert rerun["status"] == "DRAFT"
    assert rerun["mode"] == "STANDARD"  # one creative cannot use rotation
    assert rerun["variants"] == original["variants"]
    assert rerun["destinations"] == original["destinations"]
    assert rerun["target_selector"] == original["target_selector"]
    assert rerun["repost_offsets_seconds"] == [3600, 86_400]
    assert rerun["delete_on_end"] is False
    assert rerun["start_at_utc"] == now
    assert rerun["current_end_at_utc"] == now + timedelta(days=7)
    assert rerun["preview_sent"] is True
    rerun["variants"][0]["text"] = "Edited"
    assert original == original_before


@pytest.mark.asyncio
async def test_repeated_rerun_taps_reuse_the_same_pending_draft(monkeypatch) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    original = {
        "campaign_id": "cmp_old",
        "name": "Promotion",
        "status": "ARCHIVED",
        "mode": "STANDARD",
        "variants": [{"id": "v1", "kind": "TEXT", "text": "Hello"}],
        "destinations": [],
        "target_selector": {},
        "start_at_utc": start,
        "current_end_at_utc": start + timedelta(hours=1),
    }
    now = datetime(2026, 2, 1, tzinfo=UTC)
    monkeypatch.setattr("app.campaigns.service.utcnow", lambda: now)
    repositories = DefinitionRepositories(original)
    service = CampaignService(repositories, 20)

    first = await service.prepare_rerun("cmp_old", owner_id=7)
    second = await service.prepare_rerun("cmp_old", owner_id=7)

    assert second["campaign_id"] == first["campaign_id"]


@pytest.mark.asyncio
async def test_scheduled_campaign_returns_to_draft_without_losing_its_plan() -> None:
    start = datetime(2026, 3, 1, 9, tzinfo=UTC)
    campaign = {
        "campaign_id": "cmp_scheduled",
        "status": "SCHEDULED",
        "start_at_utc": start,
        "current_end_at_utc": start + timedelta(days=2),
        "variants": [{"id": "v1"}],
        "target_snapshot": [-1001],
        "cohort_map": {"-1001": 0},
    }
    repositories = DefinitionRepositories(campaign)

    draft = await CampaignService(repositories, 20).return_to_draft("cmp_scheduled", owner_id=7)

    assert draft["status"] == "DRAFT"
    assert draft["start_at_utc"] == start
    assert draft["current_end_at_utc"] == start + timedelta(days=2)
    assert draft["variants"] == [{"id": "v1"}]
    assert draft["target_snapshot"] == []


class NoopLimiter:
    async def acquire(self):
        return None


class AlbumSender:
    def __init__(self):
        self.deleted = []

    async def delete_messages(self, channel_id, message_ids):
        self.deleted.append(list(message_ids))
        if message_ids == [10]:
            raise RuntimeError("message to delete not found")

    async def send_variant(self, channel_id, variant):
        return SendResult([20])


class WorkerRepositories:
    def __init__(self):
        self.campaign = {"campaign_id": "cmp", "status": "ACTIVE", "variants": [{"id": "v1"}]}
        self.completed = []
        self.saved = None
        self.cycle_finished = 0
        self.superseded_cleanup = 0

    async def get_campaign(self, campaign_id):
        return self.campaign

    async def get_channel(self, channel_id):
        return {"telegram_chat_id": channel_id, "status": "ACTIVE", "permissions": {"can_post_messages": True}}

    async def live_state(self, campaign_id, channel_id):
        return {"current_message_ids": [10, 11]}

    async def clear_live_state(self, campaign_id, channel_id):
        return None

    async def save_live_state(self, *args):
        self.saved = args

    async def complete_delivery(self, delivery_id, status, **details):
        self.completed.append((delivery_id, status.value, details))

    async def finish_cycle_if_complete(self, campaign_id, cycle_number):
        self.cycle_finished += 1

    async def set_channel_status(self, *args, **kwargs):
        return None

    async def materialize_superseded_cleanup(self, channel_id, campaign_id):
        self.superseded_cleanup += 1
        return 0


def test_worker_resolves_the_delivery_frozen_variant_revision() -> None:
    old = {"id": "v1", "kind": "TEXT", "text": "old"}
    new = {"id": "v1", "kind": "TEXT", "text": "new"}
    campaign = {
        "variants": [new],
        "variant_versions": {
            "v1": [
                {"revision": 1, "creative": old},
                {"revision": 2, "creative": new},
            ]
        },
    }

    assert DeliveryWorker._delivery_variant(
        campaign,
        {"variant_index": 0, "variant_id": "v1", "variant_revision": 1},
    ) == old
    assert DeliveryWorker._delivery_variant(
        campaign,
        {"variant_index": 0, "variant_id": "v1", "variant_revision": 2},
    ) == new
    # A legacy delivery created before revision fields existed consistently
    # resolves to the pre-edit revision instead of the new live payload.
    assert DeliveryWorker._delivery_variant(campaign, {"variant_index": 0}) == old


@pytest.mark.asyncio
async def test_repost_deletes_every_album_item_even_when_one_is_already_absent() -> None:
    repositories = WorkerRepositories()
    sender = AlbumSender()
    worker = DeliveryWorker(
        worker_id="worker",
        repositories=repositories,
        sender=sender,
        send_limiter=NoopLimiter(),
        mutation_limiter=NoopLimiter(),
        delivery_lease_seconds=30,
        max_transient_attempts=3,
    )
    delivery = {
        "_id": "delivery",
        "campaign_id": "cmp",
        "channel_id": -1001,
        "cycle_number": 1,
        "variant_index": 0,
        "attempts": 1,
    }

    await worker.process(delivery)

    assert sender.deleted == [[10], [11]]
    assert repositories.saved[-1] == [20]
    assert repositories.completed[-1][1] == "SENT"
    assert repositories.completed[-1][2]["replaced_message_count"] == 1
    assert repositories.cycle_finished == 1
    assert repositories.superseded_cleanup == 1


class EndingRepositories:
    def __init__(self, quiescent):
        self.quiescent = quiescent
        self.materialized = 0
        self.archived = 0
        self.cleanup_checked = 0

    async def cancel_pending_campaign_deliveries(self, campaign_id):
        return None

    async def finish_complete_cycles(self, campaign_id):
        return 0

    async def campaign_send_work_is_quiescent(self, campaign_id):
        return self.quiescent

    async def materialize_cleanup_deliveries(self, campaign_id):
        self.materialized += 1

    async def cleanup_is_complete(self, campaign_id):
        self.cleanup_checked += 1
        return True

    async def mark_campaign_archived(self, campaign_id, reason):
        self.archived += 1


@pytest.mark.asyncio
async def test_ending_waits_for_inflight_send_before_cleanup_and_archive() -> None:
    repositories = EndingRepositories(quiescent=False)
    scheduler = Scheduler(
        instance_id="instance",
        repositories=repositories,
        lease_manager=None,
        campaign_service=None,
        lease_seconds=30,
        tick_seconds=1,
    )
    campaign = {"campaign_id": "cmp", "status": "ENDING", "delete_on_end": True}

    await scheduler._finish_ending_campaign(campaign)
    assert (repositories.materialized, repositories.archived) == (0, 0)

    repositories.quiescent = True
    await scheduler._finish_ending_campaign(campaign)
    assert (repositories.materialized, repositories.archived) == (1, 1)


@pytest.mark.asyncio
async def test_retained_campaign_archives_without_waiting_for_its_intentional_live_posts() -> None:
    repositories = EndingRepositories(quiescent=True)
    scheduler = Scheduler(
        instance_id="instance",
        repositories=repositories,
        lease_manager=None,
        campaign_service=None,
        lease_seconds=30,
        tick_seconds=1,
    )

    await scheduler._finish_ending_campaign(
        {"campaign_id": "cmp", "status": "ENDING", "delete_on_end": False, "end_reason": "window_elapsed"}
    )

    assert repositories.materialized == 0
    assert repositories.cleanup_checked == 0
    assert repositories.archived == 1


@pytest.mark.asyncio
async def test_scheduler_repairs_a_partially_completed_resume_before_planning() -> None:
    campaign = {"campaign_id": "cmp", "status": "ACTIVE", "resume_recovery_needed": True}

    class RecoveryRepositories:
        def __init__(self):
            self.resumed = 0
            self.update = None

        async def recover_interrupted_cleanup_campaigns(self):
            return 0

        async def due_campaigns(self, now):
            return [campaign]

        async def resume_campaign_deliveries(self, campaign_id):
            self.resumed += 1

        async def advance_running_campaign(self, campaign_id, update):
            self.update = update
            return True

    class RecoveryLease:
        async def acquire_or_renew(self, *args):
            return True

    class RecoveryService:
        def __init__(self):
            self.planned = 0

        async def plan_due_cycle(self, item, now):
            self.planned += 1

    repositories = RecoveryRepositories()
    service = RecoveryService()
    scheduler = Scheduler(
        instance_id="instance",
        repositories=repositories,
        lease_manager=RecoveryLease(),
        campaign_service=service,
        lease_seconds=30,
        tick_seconds=1,
    )

    await scheduler.tick()

    assert repositories.resumed == 1
    assert repositories.update["resume_recovery_needed"] is False
    assert service.planned == 1


class BackupRepositories:
    def __init__(self):
        self.settings = {}
        self.channels = 3

    async def channel_count(self):
        return self.channels

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value

    async def export_collections(self, full):
        return {"channels": [{"telegram_chat_id": -1001}], "campaigns": [], "settings": []}


class BackupLease:
    async def acquire_or_renew(self, *args):
        return True


class BackupBot:
    def __init__(self):
        self.sent = []

    async def send_document(self, owner_id, document, caption):
        self.sent.append((owner_id, document.filename, caption))


@pytest.mark.asyncio
async def test_automatic_backup_coalesces_initial_time_and_growth_triggers(monkeypatch) -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC)
    monkeypatch.setattr("app.backups.automatic.utcnow", lambda: now)
    repositories = BackupRepositories()
    bot = BackupBot()
    worker = AutomaticBackupWorker(
        instance_id="instance",
        repositories=repositories,
        lease_manager=BackupLease(),
        bot=bot,
        owner_ids=frozenset({7}),
        every_new_channels=100,
        interval_hours=168,
    )

    assert await worker.tick()
    assert not await worker.tick()
    assert len(bot.sent) == 1
    assert repositories.settings["auto_backup_channel_count"] == 3


def test_long_extension_changes_cleanup_policy_before_message_age_becomes_unsafe() -> None:
    start = datetime(2026, 5, 1, tzinfo=UTC)
    campaign = {
        "start_at_utc": start,
        "repost_offsets_seconds": [24 * 3600],
        "repost_interval_seconds": None,
    }
    assert CampaignService._cleanup_safe_after_extension(campaign, start + timedelta(hours=47))
    assert not CampaignService._cleanup_safe_after_extension(campaign, start + timedelta(days=4))


def test_stored_error_summary_redacts_invite_links() -> None:
    summary = DeliveryWorker._safe_error_summary(RuntimeError("failed for https://t.me/+secret-token"))
    assert "secret-token" not in summary
    assert "[redacted-url]" in summary
