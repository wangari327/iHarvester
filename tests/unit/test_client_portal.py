from datetime import UTC, datetime, timedelta

from app.web.client_portal import campaign_progress_payload, render_client_portal


def test_client_portal_shows_aggregate_progress_without_network_identifiers() -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    payload = campaign_progress_payload(
        {
            "name": "Client premiere",
            "status": "ACTIVE",
            "mode": "ROTATE",
            "start_at_utc": now - timedelta(hours=1),
            "current_end_at_utc": now + timedelta(hours=1),
            "archived_at": now,
            "updated_at": now,
        },
        {"SENT": 20, "PENDING": 4, "FAILED_PERMANENT": 1},
        {"planned": 3, "completed": 1},
        {"last_updated_at": now},
        live_posts=20,
        joined=7,
    )

    assert payload["campaign"]["name"] == "Client premiere"
    assert payload["delivery"] == {
        "complete": 21,
        "total": 25,
        "percent": 84,
        "sent": 20,
        "pending": 4,
        "failed": 1,
        "unknown": 0,
        "cancelled": 0,
    }
    assert payload["timeline"]["percent"] > 0
    assert "channel" not in str(payload).lower()


def test_client_portal_auto_refreshes_and_has_approval_gated_request_copy() -> None:
    payload = {
        "campaign": {"name": "<Client promotion>", "status": "Active", "mode": "Standard", "start_at": None, "end_at": None, "updated_at": None},
        "delivery": {"complete": 0, "total": 0, "percent": 0, "sent": 0, "pending": 0, "failed": 0, "unknown": 0, "cancelled": 0},
        "timeline": {"percent": 0, "status": "ACTIVE"},
        "cycles": {"completed": 0, "planned": 0},
        "cleanup": {"deleted": 0, "failed": 0, "live_posts": 0},
        "engagement": {"tracked_joins": 0},
    }

    page = render_client_portal("secret-token", payload)

    assert "setInterval(refresh,10000)" in page
    assert "approves every request" in page
    assert "&lt;Client promotion&gt;" in page
    assert "/client/c/${encodeURIComponent(token)}/progress" in page
