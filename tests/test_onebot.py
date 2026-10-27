from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from websockets.asyncio.server import serve as ws_serve

from qqagent.config import OneBotConfig
from qqagent.core.onebot import OneBotClient, OneBotError


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any], str]] = []
        self.fail_actions: set[str] = set()

    async def handle(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        body = await request.json()
        self.requests.append((action, body, request.headers.get("Authorization", "")))
        if action in self.fail_actions:
            return web.json_response(
                {"status": "failed", "retcode": 100, "message": "boom", "data": None}
            )
        if action == "get_login_info":
            data = {"user_id": 10001, "nickname": "bot"}
        else:
            data = {"message_id": 9001}
        return web.json_response({"status": "ok", "retcode": 0, "data": data})


@pytest.fixture
async def fake_api():
    api = FakeApi()
    app = web.Application()
    app.router.add_post("/{action}", api.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield api, port
    await runner.cleanup()


@pytest.fixture
async def ws_server():
    connections: list[Any] = []
    sent: list[dict[str, Any]] = []

    async def handler(websocket) -> None:
        connections.append(websocket)
        event = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 10001,
            "group_id": 123,
            "user_id": 20002,
            "message_id": 1,
            "sender": {"nickname": "小明"},
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        sent.append(event)
        await websocket.send(json.dumps(event))
        try:
            await websocket.wait_closed()
        except (asyncio.CancelledError, Exception):
            pass

    server = await ws_serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield sent, port
    server.close()
    await server.wait_closed()


async def test_onebot_receives_event_and_sends_http(fake_api, ws_server) -> None:
    api, http_port = fake_api
    events, ws_port = ws_server
    received: list[dict[str, Any]] = []

    async def on_event(raw: dict[str, Any]) -> None:
        received.append(raw)

    config = OneBotConfig(
        ws_url=f"ws://127.0.0.1:{ws_port}",
        http_url=f"http://127.0.0.1:{http_port}",
        ws_token="ws-token",
        http_token="http-token",
    )
    client = OneBotClient(config, on_event)
    await client.start()
    try:
        assert await client.wait_connected(3)
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received
        assert client.self_id == "10001"
        assert client.nickname == "bot"

        data = await client.send_group_msg("123", [{"type": "text", "data": {"text": "hello"}}])
        assert data["message_id"] == 9001
        action, body, auth = api.requests[-1]
        assert action == "send_group_msg"
        assert body["group_id"] == 123
        assert auth == "Bearer http-token"
    finally:
        await client.stop()


async def test_onebot_http_error(fake_api, ws_server) -> None:
    api, http_port = fake_api
    _, ws_port = ws_server
    api.fail_actions.add("get_group_info")

    async def on_event(raw: dict[str, Any]) -> None:
        return None

    config = OneBotConfig(
        ws_url=f"ws://127.0.0.1:{ws_port}",
        http_url=f"http://127.0.0.1:{http_port}",
    )
    client = OneBotClient(config, on_event)
    await client.start()
    try:
        assert await client.wait_connected(3)
        with pytest.raises(OneBotError):
            await client.get_group_info("123")
    finally:
        await client.stop()
