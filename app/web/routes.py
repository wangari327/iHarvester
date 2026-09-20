from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError

from app.web.client_portal import campaign_progress_payload, render_client_portal

logger = logging.getLogger(__name__)


class ClientPromotionRequest(BaseModel):
    request_type: str
    client_name: str = Field(min_length=1, max_length=100)
    details: str = Field(default="", max_length=3000)
    desired_start: str | None = Field(default=None, max_length=64)
    client_timezone: str = Field(default="UTC", max_length=64)


def _client_start_time(value: str | None, timezone: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise HTTPException(status_code=422, detail="Choose a valid date, time, and timezone.") from error
    result = parsed.astimezone(UTC)
    if result <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="Choose a future preferred start time, or leave it blank.")
    return result


async def _client_portal_payload(runtime: object, token: str) -> tuple[object, dict[str, object]]:
    share = await runtime.repositories.campaign_portal_share(token)
    if not share:
        raise HTTPException(status_code=404, detail="This client progress link is no longer available.")
    campaign = await runtime.repositories.get_campaign(share["campaign_id"])
    if not campaign:
        raise HTTPException(status_code=404, detail="This campaign is no longer available.")
    totals = await runtime.repositories.campaign_delivery_totals(campaign["campaign_id"])
    cycle_stats = await runtime.repositories.campaign_cycle_stats(campaign["campaign_id"])
    metrics = await runtime.repositories.campaign_delivery_metrics(campaign["campaign_id"])
    live_posts = await runtime.repositories.campaign_live_state_count(campaign["campaign_id"])
    joined = await runtime.repositories.campaign_join_count(campaign["campaign_id"])
    return share, campaign_progress_payload(campaign, totals, cycle_stats, metrics, live_posts=live_posts, joined=joined)


async def _notify_rerun_request(runtime: object, client_request: object) -> None:
    """A rerun has no material handoff, so notify its owner immediately."""
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Review client request", callback_data=f"req:{client_request['request_id']}:open")]
        ]
    )
    text = (
        "Client rerun request ready for approval\n\n"
        f"Client: {client_request.get('client_name') or 'not supplied'}\n"
        "Open it to review payment and approve the retained campaign setup."
    )
    for owner_id in runtime.settings.owner_ids:
        try:
            await runtime.bot.send_message(owner_id, text, reply_markup=markup)
        except Exception:
            logger.warning("Could not notify owner of client rerun request", extra={"request_id": client_request["request_id"]})


async def _notify_database_unavailable(runtime: object, update: Update) -> None:
    """Give callback users a useful answer while Telegram retries the update."""
    callback = update.callback_query
    if not callback:
        return
    try:
        await runtime.bot.answer_callback_query(
            callback.id,
            text="The campaign database is temporarily unreachable. Your action will retry automatically; tap again in a moment if needed.",
            show_alert=True,
            cache_time=0,
        )
    except Exception:
        # The webhook must still return a retryable response even if Telegram
        # has already expired this cosmetic callback acknowledgement.
        logger.warning("Could not notify owner about unavailable database", extra={"update_id": update.update_id})


def install_routes(app: object) -> None:
    # Deferred import keeps the tiny HTTP layer independent from application construction.
    @app.get("/")
    async def index() -> dict[str, str]:
        """Safe public ping target for a platform or external uptime monitor."""
        return {"status": "ok", "service": "iHarvester"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, str]:
        runtime = request.app.state.runtime
        if not runtime.ready:
            raise HTTPException(status_code=503, detail="startup checks incomplete")
        try:
            await runtime.database.ping()
        except Exception as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        return {"status": "ready"}

    @app.get("/client/c/{token}", response_class=HTMLResponse)
    async def client_campaign_page(token: str, request: Request) -> HTMLResponse:
        runtime = request.app.state.runtime
        _, payload = await _client_portal_payload(runtime, token)
        return HTMLResponse(render_client_portal(token, payload))

    @app.get("/client/c/{token}/progress")
    async def client_campaign_progress(token: str, request: Request) -> dict[str, object]:
        runtime = request.app.state.runtime
        _, payload = await _client_portal_payload(runtime, token)
        return payload

    @app.post("/client/c/{token}/requests")
    async def client_campaign_request(token: str, body: ClientPromotionRequest, request: Request) -> dict[str, str | None]:
        runtime = request.app.state.runtime
        share, _ = await _client_portal_payload(runtime, token)
        request_type = body.request_type.upper()
        if request_type not in {"RERUN", "NEW"}:
            raise HTTPException(status_code=422, detail="Choose either a rerun or a new promotion.")
        if request_type == "NEW" and not runtime.bot_username:
            raise HTTPException(status_code=503, detail="The Telegram material intake is temporarily unavailable. Please try again shortly.")
        desired_start = _client_start_time(body.desired_start, body.client_timezone)
        client_request, material_token = await runtime.repositories.create_client_promotion_request(
            share=share,
            request_type=request_type,
            client_name=body.client_name,
            details=body.details,
            desired_start_at_utc=desired_start,
            client_timezone=body.client_timezone,
        )
        material_url = None
        if request_type == "NEW":
            material_url = f"https://t.me/{runtime.bot_username}?start=promo_{client_request['request_id']}_{material_token}"
        if request_type == "RERUN":
            await _notify_rerun_request(runtime, client_request)
        return {"request_id": client_request["request_id"], "material_url": material_url}

    @app.post("/telegram/webhook/{path_secret}")
    async def telegram_webhook(path_secret: str, request: Request) -> Response:
        runtime = request.app.state.runtime
        settings = runtime.settings
        if settings.run_mode != "webhook" or not hmac.compare_digest(path_secret, settings.webhook_path_secret or ""):
            raise HTTPException(status_code=404, detail="not found")
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(provided, settings.webhook_secret_token or ""):
            raise HTTPException(status_code=401, detail="invalid Telegram webhook secret")
        update = Update.model_validate(await request.json(), context={"bot": runtime.bot})
        try:
            registered = await runtime.repositories.register_update(update.update_id)
        except PyMongoError:
            logger.warning("Deferring webhook update because MongoDB is unavailable", extra={"update_id": update.update_id})
            await _notify_database_unavailable(runtime, update)
            # A non-2xx response makes Telegram retain and retry this update;
            # returning 200 here would silently lose a Launch tap.
            return Response(status_code=503, headers={"Retry-After": "5"})
        if not registered:
            return Response(status_code=200)
        try:
            await runtime.dispatcher.feed_update(runtime.bot, update)
        except Exception:
            await runtime.repositories.unregister_update(update.update_id)
            raise
        return Response(status_code=200)
