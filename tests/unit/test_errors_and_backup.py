from datetime import UTC, datetime

import pytest

from app.backups.export import make_backup
from app.backups.restore import parse_backup, restore_backup
from app.delivery.error_classification import ErrorKind, classify_telegram_error


def test_ambiguous_send_timeout_is_not_a_blind_retry() -> None:
    decision = classify_telegram_error(TimeoutError("network timeout"), operation="send")
    assert decision.kind == ErrorKind.AMBIGUOUS


def test_429_honors_retry_after() -> None:
    error = RuntimeError("too many requests")
    error.retry_after = 12
    decision = classify_telegram_error(error, operation="send")
    assert decision.kind == ErrorKind.RETRY
    assert decision.retry_after_seconds == 12


def test_manually_deleted_campaign_post_is_a_successful_cleanup_outcome() -> None:
    decision = classify_telegram_error(
        RuntimeError("Telegram server says - Bad Request: message to delete not found"),
        operation="delete",
    )
    assert decision.kind == ErrorKind.CLEAN_ABSENT


def test_telegram_deletion_policy_is_reported_as_a_real_cleanup_blocker() -> None:
    decision = classify_telegram_error(
        RuntimeError("Telegram server says - Bad Request: message can't be deleted"),
        operation="delete",
    )
    assert decision.kind == ErrorKind.PERMANENT
    assert decision.category == "DELETE_NOT_ALLOWED"


class FakeRepositories:
    async def export_collections(self, full: bool):
        return {"channels": [{"telegram_chat_id": -1001, "registered_at": datetime(2026, 1, 1, tzinfo=UTC)}], "campaigns": [], "settings": []}


@pytest.mark.asyncio
async def test_core_backup_has_integrity_validation() -> None:
    payload = await make_backup(FakeRepositories())
    backup = parse_backup(payload)
    assert backup["collections"]["channels"][0]["telegram_chat_id"] == -1001
    assert backup["collections"]["channels"][0]["registered_at"].tzinfo == UTC


@pytest.mark.asyncio
async def test_restore_never_resumes_any_live_campaign_state() -> None:
    class RestoreRepositories:
        def __init__(self):
            self.rows = {}

        async def restore_collection(self, name, documents):
            self.rows[name] = documents
            return len(documents), 0

    repositories = RestoreRepositories()
    backup = {
        "collections": {
            "channels": [],
            "settings": [],
            "campaigns": [{"campaign_id": status, "status": status} for status in ("ACTIVE", "SCHEDULED", "PAUSED", "ENDING", "DRAFT")],
        }
    }

    await restore_backup(repositories, backup)

    restored = {row["campaign_id"]: row for row in repositories.rows["campaigns"]}
    assert all(restored[status]["status"] == "ARCHIVED" for status in ("ACTIVE", "SCHEDULED", "PAUSED", "ENDING"))
    assert restored["DRAFT"]["status"] == "DRAFT"
