"""WS envelope regressions; binary ingress is tested through the HTTP listener."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import INBOUND_META_USER_SHELL
from nanobot.channels.websocket.runtime import (
    WebSocketChannel,
    WebSocketConfig,
)
from nanobot.command.builtin import USER_SHELL_COMMAND
from nanobot.runtime_context import RUNTIME_CONTEXT_INPUT_META
from nanobot.session import webui_turns as wth
from nanobot.session.manager import SessionManager
from nanobot.session.session_handles import SESSION_HANDLE_METADATA_KEY, SessionHandleResolver
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.session_access import WebuiSessionAccess
from nanobot.webui.transcript import read_transcript_lines


def _make_channel(session_manager: SessionManager | None = None) -> WebSocketChannel:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    cfg = {"enabled": True, "allowFrom": ["*"], "websocketRequiresToken": False}
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=session_manager,
        static_dist_path=None,
        workspace_path=Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(cfg, bus, gateway=gateway)
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    return channel


@pytest.fixture(autouse=True)
def isolate_websocket_turn_state() -> None:
    wth._WEBSOCKET_ACTIVE_TURNS.clear()
    wth._WEBSOCKET_TURN_WALL_STARTED_AT.clear()
    wth._WEBSOCKET_TURN_IDS.clear()
    wth._WEBSOCKET_TURN_OWNERS.clear()
    yield
    wth._WEBSOCKET_ACTIVE_TURNS.clear()
    wth._WEBSOCKET_TURN_WALL_STARTED_AT.clear()
    wth._WEBSOCKET_TURN_IDS.clear()
    wth._WEBSOCKET_TURN_OWNERS.clear()


def test_max_message_bytes_keeps_existing_transport_guard() -> None:
    """Moving attachments to HTTP does not disable the configurable WS guard."""
    from nanobot.channels.websocket.runtime import WebSocketConfig

    default = WebSocketConfig().max_message_bytes
    assert default == 36 * 1024 * 1024
    assert WebSocketConfig(max_message_bytes=1048576).max_message_bytes == 1048576
    # The existing upper bound remains 40 MB.
    with pytest.raises(Exception):
        WebSocketConfig(max_message_bytes=41_943_040 + 1)


@pytest.mark.asyncio
async def test_message_without_media_backward_compatible() -> None:
    """Existing clients that don't send ``media`` keep working unchanged."""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {"type": "message", "chat_id": "abc123", "content": "hello"}

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    call = channel._handle_message.call_args
    assert call.kwargs["chat_id"] == "abc123"
    assert call.kwargs["content"] == "hello"
    # When no media, we pass ``media=None`` so downstream treats it as absent.
    assert call.kwargs["media"] is None


@pytest.mark.asyncio
async def test_webui_message_acceptance_echoes_turn_id() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "hello",
        "webui": True,
        "turn_id": "turn-accepted",
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    assert json.loads(mock_conn.send.await_args.args[0]) == {
        "event": "message_accepted",
        "chat_id": "abc123",
        "turn_id": "turn-accepted",
        "starts_turn": True,
        "active_turn_id": "turn-accepted",
        "started_at": wth.websocket_turn_wall_started_at("abc123"),
    }


@pytest.mark.asyncio
async def test_message_text_policy_is_independent_from_transport_limit() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "你" * 22_000,
        "turn_id": "turn-text-policy",
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err == {
        "event": "error",
        "chat_id": "abc123",
        "detail": "message_rejected",
        "reason": "text_too_large",
        "turn_id": "turn-text-policy",
    }


@pytest.mark.asyncio
async def test_message_forwards_normalized_cli_app_attachments() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "please use @drawio",
        "webui": True,
        "cli_apps": [
            {
                "name": "DrawIO",
                "display_name": "Draw.io",
                "category": "diagram",
                "entry_point": "cli-anything-drawio",
                "logo_url": "https://example.invalid/drawio.svg",
                "brand_color": "#F08705",
            },
            {"name": "bad name", "entry_point": "nope"},
        ],
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    metadata = channel._handle_message.call_args.kwargs["metadata"]
    assert metadata["webui"] is True
    assert metadata["cli_apps"] == [{
        "name": "drawio",
        "display_name": "Draw.io",
        "category": "diagram",
        "entry_point": "cli-anything-drawio",
        "logo_url": "https://example.invalid/drawio.svg",
        "brand_color": "#F08705",
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize(("content", "send_legacy", "references_target"), [
    ("Use @bime", False, True),
    ("Use @BIME", True, True),
    ("Pasted text:\n(@bime), @bime", False, True),
    ("No reference remains in this text", True, False),
    ("Use @unknown", True, False),
], ids=["plain-text", "forged-metadata", "pasted-text", "removed-mention", "unknown-handle"])
async def test_webui_message_resolves_text_for_dispatch_echo_and_transcript(
    tmp_path, content, send_legacy, references_target,
) -> None:
    manager = SessionManager(tmp_path)
    for key, name, title in (
        ("websocket:pricing", "bime", "Pricing"),
        ("websocket:forged", "luma", "Not referenced in the text"),
    ):
        target = manager.get_or_create(key)
        target.metadata.update({"title": title, SESSION_HANDLE_METADATA_KEY: name})
        target.add_message("user", "Discuss cloud storage")
        manager.save(target)
    channel = _make_channel(manager)
    origin, peer = AsyncMock(), AsyncMock()
    channel._webui_connections.add(origin)
    channel._attach(peer, "current")
    envelope = {
        "type": "message",
        "chat_id": "current",
        "turn_id": "session-mention-turn",
        "content": content,
        "webui": True,
    }
    if send_legacy:
        envelope["session_mentions"] = [{
            "name": "bime",
            "session_key": "websocket:forged",
            "title": "Untrusted title",
        }]

    await channel._dispatch_envelope(origin, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    call = channel._handle_message.call_args
    assert call.kwargs["chat_id"] == "current"
    assert call.kwargs["content"] == content
    metadata = call.kwargs["metadata"]
    handle = await SessionHandleResolver(manager).ahandle_for_session("websocket:pricing")
    assert handle is not None
    expected = [{
        **handle.public_payload(), "session_key": "websocket:pricing", "title": "Pricing",
    }] if references_target else []
    assert metadata.get("session_mentions", []) == expected
    if references_target:
        [block] = metadata[RUNTIME_CONTEXT_INPUT_META]
        assert block.source == "session_mentions"
        assert "websocket:pricing" in block.content
        assert "websocket:forged" not in block.content
    else:
        assert RUNTIME_CONTEXT_INPUT_META not in metadata
    events = [json.loads(call.args[0]) for call in peer.send.await_args_list]
    [echo] = [event for event in events if event.get("event") == "user_message"]
    assert echo["text"] == content
    assert echo.get("session_mentions", []) == expected
    origin_events = [json.loads(call.args[0]) for call in origin.send.await_args_list]
    assert [event for event in origin_events if event.get("event") == "user_message"] == (
        [echo] if references_target else []
    )
    [persisted] = read_transcript_lines("websocket:current")
    assert persisted["text"] == content
    assert persisted.get("session_mentions", []) == expected
    await channel._cleanup_connection(origin)
    await channel._cleanup_connection(peer)
    await manager.state.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("webui", "registered"), [(False, False), (False, True), (True, False)])
async def test_only_trusted_webui_messages_resolve_session_mentions(
    tmp_path, monkeypatch, webui, registered,
) -> None:
    manager = SessionManager(tmp_path)
    target = manager.get_or_create("websocket:pricing")
    target.metadata.update({"title": "Pricing", SESSION_HANDLE_METADATA_KEY: "bime"})
    manager.save(target)
    resolve = AsyncMock(side_effect=AssertionError("Untrusted clients must not resolve handles"))
    monkeypatch.setattr(WebuiSessionAccess, "resolve_mentions", resolve)
    channel = _make_channel(manager)
    connection = AsyncMock()
    if registered:
        channel._webui_connections.add(connection)

    await channel._dispatch_envelope(connection, "client-1", {
        "type": "message", "chat_id": "current", "content": "Use @bime", "webui": webui,
        "session_mentions": [{"name": "bime", "session_key": "websocket:pricing"}],
    })

    resolve.assert_not_awaited()
    channel._handle_message.assert_awaited_once()
    metadata = channel._handle_message.call_args.kwargs["metadata"]
    assert "session_mentions" not in metadata
    assert RUNTIME_CONTEXT_INPUT_META not in metadata
    await channel._cleanup_connection(connection)
    await manager.state.aclose()


@pytest.mark.asyncio
async def test_user_shell_handle_arguments_do_not_become_session_mentions(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    names = ["bime", "luma", "nora", "tora", "riva", "sela", "vira", "zora", "pola"]
    for name in names:
        target = manager.get_or_create(f"websocket:{name}")
        target.metadata.update({"title": name, SESSION_HANDLE_METADATA_KEY: name})
        manager.save(target)
    channel = _make_channel(manager)
    connection = AsyncMock()
    channel._webui_connections.add(connection)
    content = "!echo " + " ".join(f"@{name}" for name in names)

    await channel._dispatch_envelope(connection, "client-1", {
        "type": "message", "chat_id": "current", "turn_id": "shell-mention-turn",
        "content": content, "webui": True, "user_shell": True,
    })

    channel._handle_message.assert_awaited_once()
    call = channel._handle_message.call_args
    assert call.kwargs["content"] == f"{USER_SHELL_COMMAND} {content[1:]}"
    metadata = call.kwargs["metadata"]
    assert metadata[INBOUND_META_USER_SHELL] is True
    assert "session_mentions" not in metadata
    assert RUNTIME_CONTEXT_INPUT_META not in metadata
    [persisted] = read_transcript_lines("websocket:current")
    assert persisted["text"] == content
    assert "session_mentions" not in persisted
    assert json.loads(connection.send.await_args.args[0])["event"] == "message_accepted"
    await channel._cleanup_connection(connection)
    await manager.state.aclose()


@pytest.mark.asyncio
async def test_ninth_session_mention_rejects_before_upload_commit_dispatch_or_transcript(
    tmp_path, monkeypatch,
) -> None:
    manager = SessionManager(tmp_path)
    names = ["bime", "luma", "nora", "tora", "riva", "sela", "vira", "zora", "pola"]
    for name in names:
        target = manager.get_or_create(f"websocket:{name}")
        target.metadata.update({"title": name, SESSION_HANDLE_METADATA_KEY: name})
        manager.save(target)
    channel = _make_channel(manager)
    connection = AsyncMock()
    channel._webui_connections.add(connection)
    owner = channel.gateway.uploads.issue(connection)["token"]
    store = channel.gateway.uploads.store

    async def body():
        yield b"file"

    reference = await store.upload(body(), owner=owner, mime="text/plain", size=4)
    commit = MagicMock(wraps=store.commit)
    monkeypatch.setattr(store, "commit", commit)
    envelope = {
        "type": "message", "chat_id": "current", "turn_id": "many-mentions-turn",
        "content": "@missing @BIME " + " ".join(f"@{name}" for name in names),
        "webui": True, "media": [{"reference": reference}],
    }

    await channel._dispatch_envelope(connection, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    commit.assert_not_called()
    assert read_transcript_lines("websocket:current") == []
    assert await manager.state.read_metadata("websocket:current") is None
    assert json.loads(connection.send.await_args.args[0]) == {
        "event": "error", "detail": "message_rejected", "reason": "too_many_session_mentions",
        "chat_id": "current", "turn_id": "many-mentions-turn",
    }
    assert Path(store.resolve([reference], owner=owner)[0]).read_bytes() == b"file"

    # Correcting the text can retry the same turn and still use the uncommitted upload.
    envelope["content"] = "@missing @BIME " + " ".join(f"@{name}" for name in names[:8])
    await channel._dispatch_envelope(connection, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    commit.assert_called_once_with([reference], owner=owner)
    mentions = channel._handle_message.call_args.kwargs["metadata"]["session_mentions"]
    assert [mention["name"] for mention in mentions] == names[:8]
    [persisted] = read_transcript_lines("websocket:current")
    assert persisted["session_mentions"] == mentions
    assert json.loads(connection.send.await_args.args[0])["event"] == "message_accepted"
    await channel._cleanup_connection(connection)
    await manager.state.aclose()


@pytest.mark.asyncio
async def test_message_rejected_when_media_item_shape_wrong(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "huh",
        # Not a dict — plain string at the top level.
        "media": ["data:image/png;base64,XXXX"],
    }

    with patch(
        "nanobot.webui.media_gateway.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["reason"] == "malformed"


@pytest.mark.asyncio
async def test_message_rejected_when_media_field_is_not_list() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "huh",
        "media": "not-a-list",
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "attachment_rejected"
    assert err["reason"] == "malformed"


@pytest.mark.asyncio
async def test_rejects_empty_text_without_media() -> None:
    """When no media is attached, whitespace-only content is still rejected
    (matches the existing behavior for backward compat)."""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "   ",
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "missing content"


@pytest.mark.asyncio
async def test_non_string_content_still_rejected() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": 42,
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "missing content"


@pytest.mark.asyncio
async def test_reference_attachments_are_projected_to_other_clients(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    channel = _make_channel()
    origin, peer = AsyncMock(), AsyncMock()
    owner = channel.gateway.uploads.issue(origin)["token"]
    channel._attach(origin, "reference-chat")
    channel._attach(peer, "reference-chat")
    channel._webui_connections.add(origin)

    async def body():
        yield b"image"

    reference = await channel.gateway.uploads.store.upload(
        body(), owner=owner, mime="image/png", size=5,
    )
    await channel._dispatch_envelope(origin, "client", {
        "type": "message", "chat_id": "reference-chat", "content": "",
        "webui": True, "turn_id": "reference-turn",
        "media": [{"reference": reference, "name": "shot.png"}],
    })
    channel._handle_message.assert_awaited_once()
    assert Path(channel._handle_message.call_args.kwargs["media"][0]).read_bytes() == b"image"
    payload = json.loads(peer.send.await_args.args[0])
    assert payload["event"] == "user_message"
    assert payload["media_urls"][0]["name"] == "shot.png"
    assert payload["media_urls"][0]["url"].startswith("/api/media/")
    assert reference not in str(payload)
