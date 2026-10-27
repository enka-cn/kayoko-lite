from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from websockets.asyncio.server import serve as ws_serve

from qqagent.config import Config
from qqagent.core.app import App
from qqagent.core.llm import ChatResponse, ToolCall


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def handle(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        body = await request.json()
        self.requests.append((action, body))
        if action == "get_login_info":
            data = {"user_id": 10001, "nickname": "bot"}
        else:
            data = {"message_id": 12345}
        return web.json_response({"status": "ok", "retcode": 0, "data": data})


class FakeLLM:
    def __init__(self) -> None:
        self.responses = [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="1", name="send_message", arguments='{"text":"收到"}')
                ],
            ),
            ChatResponse(content="", tool_calls=[]),
        ]

    async def chat(self, messages, tools=None):
        return self.responses.pop(0) if self.responses else ChatResponse("", [])


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
    async def handler(websocket) -> None:
        await websocket.send(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "self_id": 10001,
                    "group_id": 123,
                    "user_id": 20002,
                    "message_id": 1,
                    "sender": {"nickname": "小明"},
                    "message": [
                        {"type": "at", "data": {"qq": "10001"}},
                        {"type": "text", "data": {"text": "在吗"}},
                    ],
                }
            )
        )
        try:
            await websocket.wait_closed()
        except (asyncio.CancelledError, Exception):
            pass

    server = await ws_serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port
    server.close()
    await server.wait_closed()


async def test_full_pipeline_with_real_onebot_client(
    tmp_path: Path, fake_api, ws_server
) -> None:
    api, http_port = fake_api
    ws_port = ws_server
    config = Config.model_validate(
        {
            "agent": {"data_dir": str(tmp_path / "data")},
            "onebot": {
                "ws_url": f"ws://127.0.0.1:{ws_port}",
                "http_url": f"http://127.0.0.1:{http_port}",
            },
            "llm": {"api_key": "x"},
            "trigger": {"allow_all_groups": True},
            "limits": {"min_delay_ms": 0, "max_delay_ms": 0},
        }
    )
    app = App(config, llm=FakeLLM())
    await app.start()
    try:
        sent = None
        for _ in range(300):
            for action, body in api.requests:
                if action == "send_group_msg":
                    sent = body
                    break
            if sent:
                break
            await asyncio.sleep(0.01)
        assert sent is not None
        assert sent["group_id"] == 123
        assert sent["message"][-1] == {"type": "text", "data": {"text": "收到"}}
        assert app.onebot.self_id == "10001"
    finally:
        await app.stop()
