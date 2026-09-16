import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import (
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedSticker,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
)

from app.campaigns.models import Button, Creative
from app.campaigns.sharing import creative_snapshot_hash, new_share_code, normalize_share_code
from app.telegram.handlers_owner import OwnerHandlers
from app.telegram.inline_sharing import (
    UnsupportedInlineCreative,
    button_layout_manifest,
    inline_result_for_share,
    inline_support_error,
)


def creative_data(kind: str = "TEXT", **overrides):
    base = {
        "id": "var_1",
        "kind": kind,
        "text": "Hello world" if kind == "TEXT" else None,
        "entities": [{"type": "bold", "offset": 0, "length": 5}] if kind == "TEXT" else [],
        "caption": "Caption" if kind != "TEXT" else None,
        "caption_entities": [{"type": "italic", "offset": 0, "length": 7}] if kind != "TEXT" else [],
        "media": [] if kind == "TEXT" else [{"field": kind.lower(), "file_id": "telegram-file-id"}],
        "buttons": [
            Button(id="one", text="First", url="https://example.com/one", row=0, position=0).model_dump(mode="json"),
            Button(id="two", text="Second", url="https://example.com/two", row=1, position=0).model_dump(mode="json"),
        ],
        "button_layout": "CUSTOM",
    }
    base.update(overrides)
    return Creative.model_validate(base).model_dump(mode="json")


def share(creative):
    return {
        "share_code": "HV-ABCD-EFGH",
        "campaign_name": "Launch",
        "variant_index": 1,
        "creative": creative,
    }


def test_share_codes_are_readable_normalized_and_snapshot_hashes_are_stable() -> None:
    assert re.fullmatch(r"HV-[A-Z2-9]{4}-[A-Z2-9]{4}", new_share_code())
    assert normalize_share_code("  hv-abcd-efgh  ") == "HV-ABCD-EFGH"
    first = creative_data()
    second = creative_data()
    assert creative_snapshot_hash(first) == creative_snapshot_hash(second)
    second["text"] = "Changed"
    assert creative_snapshot_hash(first) != creative_snapshot_hash(second)


def test_text_inline_result_preserves_entities_and_custom_cta_rows() -> None:
    result = inline_result_for_share(share(creative_data()))
    assert isinstance(result, InlineQueryResultArticle)
    assert result.input_message_content.message_text == "Hello world"
    assert str(result.input_message_content.entities[0].type) == "bold"
    assert [[button.text for button in row] for row in result.reply_markup.inline_keyboard] == [["First"], ["Second"]]


@pytest.mark.parametrize(
    ("kind", "extra_media", "expected"),
    [
        ("PHOTO", {}, InlineQueryResultCachedPhoto),
        ("VIDEO", {}, InlineQueryResultCachedVideo),
        ("ANIMATION", {"mime_type": "image/gif"}, InlineQueryResultCachedGif),
        ("ANIMATION", {"mime_type": "video/mp4"}, InlineQueryResultCachedMpeg4Gif),
        ("DOCUMENT", {}, InlineQueryResultCachedDocument),
        ("AUDIO", {}, InlineQueryResultCachedAudio),
        ("VOICE", {}, InlineQueryResultCachedVoice),
        ("STICKER", {}, InlineQueryResultCachedSticker),
    ],
)
def test_supported_media_snapshots_map_to_cached_inline_results(kind, extra_media, expected) -> None:
    media = {"field": kind.lower(), "file_id": "telegram-file-id", **extra_media}
    creative = creative_data(
        kind,
        media=[media],
        caption=None if kind == "STICKER" else "Caption",
        caption_entities=[] if kind == "STICKER" else [{"type": "italic", "offset": 0, "length": 7}],
    )
    result = inline_result_for_share(share(creative))
    assert isinstance(result, expected)
    assert result.model_dump(mode="json", exclude_none=True)["reply_markup"]["inline_keyboard"]


def test_inline_mode_rejects_multi_message_and_video_note_snapshots_cleanly() -> None:
    album = creative_data(
        "MEDIA_GROUP",
        media=[{"field": "photo", "file_id": "one"}, {"field": "photo", "file_id": "two"}],
    )
    video_note = creative_data("VIDEO_NOTE", caption=None, caption_entities=[])
    assert "album" in inline_support_error(album).lower()
    assert "video-note" in inline_support_error(video_note).lower()
    with pytest.raises(UnsupportedInlineCreative):
        inline_result_for_share(share(album))


def test_cta_fallback_preserves_labels_urls_and_rows() -> None:
    manifest = button_layout_manifest(creative_data())
    assert "Row 1\n1. First\nStyle: default\nhttps://example.com/one" in manifest
    assert "Row 2\n1. Second\nStyle: default\nhttps://example.com/two" in manifest


def test_inline_result_preserves_native_cta_color() -> None:
    creative = creative_data(
        buttons=[Button(id="one", text="Open", url="https://example.com", style="success").model_dump(mode="json")],
    )
    result = inline_result_for_share(share(creative))
    assert result.reply_markup.inline_keyboard[0][0].style == "success"


def test_owner_router_subscribes_to_inline_updates_for_webhook_registration() -> None:
    handlers = OwnerHandlers(owner_ids=frozenset({11}), repositories=None, campaigns=None, sender=None)
    assert "inline_query" in handlers.router.resolve_used_update_types()


@pytest.mark.asyncio
async def test_unauthorized_inline_queries_never_look_up_share_codes() -> None:
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.owner_ids = frozenset({11})
    handlers.repositories = SimpleNamespace(get_variant_share=AsyncMock())
    query = SimpleNamespace(from_user=SimpleNamespace(id=12), query="HV-ABCD-EFGH", answer=AsyncMock())

    await handlers.inline_query(query)

    handlers.repositories.get_variant_share.assert_not_awaited()
    query.answer.assert_awaited_once_with(results=[], cache_time=0, is_personal=True)


@pytest.mark.asyncio
async def test_inline_result_remains_successful_when_usage_telemetry_fails() -> None:
    creative = creative_data()
    frozen_share = {
        "share_code": "HV-ABCD-EFGH",
        "campaign_name": "Launch",
        "variant_index": 0,
        "creative": creative,
    }
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.owner_ids = frozenset({11})
    handlers.repositories = SimpleNamespace(
        get_variant_share=AsyncMock(return_value=frozen_share),
        record_variant_share_query=AsyncMock(side_effect=RuntimeError("telemetry unavailable")),
    )
    query = SimpleNamespace(from_user=SimpleNamespace(id=11), query="hv-abcd-efgh", answer=AsyncMock())

    await handlers.inline_query(query)

    answer = query.answer.await_args.kwargs
    assert answer["cache_time"] == 0
    assert answer["is_personal"] is True
    assert len(answer["results"]) == 1


@pytest.mark.asyncio
async def test_variant_share_screen_has_chat_picker_copy_fallback_and_revocation() -> None:
    creative = creative_data()
    frozen_share = {
        "share_code": "HV-ABCD-EFGH",
        "campaign_id": "cmp",
        "campaign_name": "Launch",
        "variant_id": "var_1",
        "variant_index": 0,
        "snapshot_hash": creative_snapshot_hash(creative),
        "creative": creative,
    }
    repositories = SimpleNamespace(
        get_or_create_variant_share=AsyncMock(return_value=frozen_share),
        active_variant_shares=AsyncMock(return_value=[frozen_share]),
    )
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="iHarvesterBot", supports_inline_queries=True))
    )
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.repositories = repositories
    handlers.sender = SimpleNamespace(bot=bot)
    handlers._render = AsyncMock()
    campaign = {"campaign_id": "cmp", "name": "Launch", "status": "DRAFT", "variants": [creative]}

    await handlers._show_variant_share(object(), 11, campaign, 0)

    _, text, markup = handlers._render.await_args.args
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert "@iHarvesterBot HV-ABCD-EFGH" in text
    assert {"Use in another bot or chat", "Copy inline command", "Copy CTA fallback", "Revoke this code", "Back", "Home"} <= {
        button.text for button in buttons
    }
    chooser = next(button for button in buttons if button.text == "Use in another bot or chat")
    assert chooser.switch_inline_query_chosen_chat.query == "HV-ABCD-EFGH"
    assert chooser.switch_inline_query_chosen_chat.allow_bot_chats
    assert all(not button.callback_data or len(button.callback_data.encode()) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_share_screen_stops_before_code_creation_when_botfather_inline_mode_is_off() -> None:
    repositories = SimpleNamespace(get_or_create_variant_share=AsyncMock())
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="iHarvesterBot", supports_inline_queries=None))
    )
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.repositories = repositories
    handlers.sender = SimpleNamespace(bot=bot)
    handlers._render = AsyncMock()

    await handlers._show_variant_share(
        object(),
        11,
        {"campaign_id": "cmp", "name": "Launch", "status": "DRAFT", "variants": [creative_data()]},
        0,
    )

    repositories.get_or_create_variant_share.assert_not_awaited()
    assert "/setinline" in handlers._render.await_args.args[1]
