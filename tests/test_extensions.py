from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from qqagent.config import Config
from qqagent.core.app import App
from qqagent.core.extensions import ExtensionManager
from qqagent.core.llm import ChatResponse, ToolCall
from qqagent.core.store import Store
from qqagent.core.tools import ToolContext
from qqagent.rpc import RPCError

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_DIR = REPO_ROOT / "extensions"


class FakeOneBot:
    self_id = "10001"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[dict[str, Any]]]] = []

    async def send_group_msg(self, group_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        self.sent.append(("group", str(group_id), segments))
        return {"message_id": len(self.sent)}


class FakeLLM:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResponse:
        return self.responses.pop(0) if self.responses else ChatResponse(content="", tool_calls=[])


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw = {
        "agent": {"data_dir": str(tmp_path / "data")},
        "llm": {"api_key": "test-key"},
        "trigger": {"allow_all_groups": True},
        "limits": {"min_delay_ms": 0, "max_delay_ms": 0},
        "extensions": {
            "paths": [str(EXTENSIONS_DIR)],
            "enabled": ["example"],
        },
    }
    raw.update(overrides)
    return Config.model_validate(raw)


@pytest.fixture
async def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QQAGENT_TEST_SECRET", "top-secret")
    config = make_config(tmp_path)
    store = Store(config.agent.data_dir / "qqagent.db")
    await store.init()
    mgr = ExtensionManager(config, store, None)
    await mgr.start()
    yield mgr, store, config
    await mgr.stop()
    await store.close()


def tool(mgr: ExtensionManager, name: str):
    for item in mgr.tools():
        if item.name == name:
            return item
    raise AssertionError(f"tool not found: {name}")


async def make_ctx(store: Store, config: Config) -> ToolContext:
    return ToolContext(
        chat_id="group:1",
        event=None,
        store=store,
        onebot=None,
        sender=None,
        config=config,
    )


async def test_example_registration(manager) -> None:
    mgr, _, _ = manager
    registered = mgr.registrations["example"]
    assert {item["name"] for item in registered.hooks} == {"message.received"}
    assert {item["name"] for item in registered.tools} == {"echo", "last_echo", "env_probe"}
    assert any("示例扩展" in item["text"] for item in registered.prompts)
    assert {item.name for item in mgr.tools()} == {
        "example__echo",
        "example__env_probe",
        "example__last_echo",
    }


async def test_hook_and_storage(manager) -> None:
    mgr, store, config = manager
    payload = {"text": "hello", "message_id": "1"}
    assert await mgr.invoke_hook("message.received", payload) == payload

    ctx = await make_ctx(store, config)
    result = await tool(mgr, "example__echo").handler(ctx, {"text": "hi"})
    assert result.ok
    assert result.text == "echo: hi"
    last = await tool(mgr, "example__last_echo").handler(ctx, {})
    assert last.text == "hi"


async def test_host_env_is_scrubbed(manager) -> None:
    mgr, store, config = manager
    ctx = await make_ctx(store, config)
    result = await tool(mgr, "example__env_probe").handler(ctx, {})
    data = json.loads(result.text)
    assert "QQAGENT_TEST_SECRET" not in data["keys"]
    assert "top-secret" not in result.text


async def test_capability_permissions(manager) -> None:
    mgr, _, _ = manager
    with pytest.raises(RPCError) as exc:
        await mgr._handle_capability(
            "capability.call",
            {
                "extension_id": "example",
                "capability": "message.send_text",
                "arguments": {"chat_id": "group:1", "text": "nope"},
            },
        )
    assert exc.value.code == -32011

    with pytest.raises(RPCError) as exc:
        await mgr._handle_capability(
            "capability.call",
            {
                "extension_id": "example",
                "capability": "onebot.call",
                "arguments": {"action": "send_group_msg", "params": {}},
            },
        )
    assert exc.value.code == -32008


async def test_app_uses_extension_tool(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    bot = FakeOneBot()
    llm = FakeLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="1", name="example__echo", arguments='{"text":"hi"}')
                ],
            ),
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="2", name="send_message", arguments='{"text":"完成"}')
                ],
            ),
            ChatResponse(content="", tool_calls=[]),
        ]
    )
    app = App(config, onebot=bot, llm=llm)
    await app.start()
    try:
        await app.handle_event(
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
                    {"type": "text", "data": {"text": "测试扩展"}},
                ],
            }
        )
        for _ in range(200):
            if bot.sent:
                break
            await asyncio.sleep(0.01)
        assert bot.sent
        assert bot.sent[0][2][-1]["data"]["text"] == "完成"
        assert await app.store.kv_get("example", "last_echo") == "hi"
    finally:
        await app.stop()


def write_crash_extension(root: Path) -> Path:
    ext = root / "crashy"
    ext.mkdir(parents=True)
    (ext / "extension.toml").write_text(
        """
[extension]
id = "crashy"
version = "0.1.0"
api_version = 1
kind = "plugin"
enabled = true
""",
        encoding="utf-8",
    )
    (ext / "__init__.py").write_text(
        """
import os
from qqagent.sdk import ExtensionAPI

def setup(api: ExtensionAPI):
    @api.tools.register(name="crash", description="crash", parameters={"type":"object","properties":{}})
    async def crash(ctx, args):
        os._exit(7)

    @api.tools.register(name="ping", description="ping", parameters={"type":"object","properties":{}})
    async def ping(ctx, args):
        return "pong"

    @api.tools.register(name="sleep", description="sleep", parameters={"type":"object","properties":{}})
    async def sleep(ctx, args):
        import asyncio
        await asyncio.sleep(10)
        return "woke"
""",
        encoding="utf-8",
    )
    return ext


async def test_host_crash_restarts(tmp_path: Path) -> None:
    root = tmp_path / "exts"
    write_crash_extension(root)
    config = make_config(
        tmp_path,
        extensions={
            "paths": [str(root)],
            "enabled": ["crashy"],
            "host": {"max_crashes": 3, "start_timeout_ms": 5000},
        },
    )
    store = Store(config.agent.data_dir / "qqagent.db")
    await store.init()
    mgr = ExtensionManager(config, store, None)
    await mgr.start()
    try:
        ctx = await make_ctx(store, config)
        result = await tool(mgr, "crashy__crash").handler(ctx, {})
        assert not result.ok

        ping = None
        for _ in range(300):
            try:
                candidate = await tool(mgr, "crashy__ping").handler(ctx, {})
                if candidate.ok and candidate.text == "pong":
                    ping = candidate
                    break
            except Exception:
                pass
            await asyncio.sleep(0.02)
        assert ping is not None
    finally:
        await mgr.stop()
        await store.close()


async def test_tool_timeout_restarts_host(tmp_path: Path) -> None:
    root = tmp_path / "exts"
    write_crash_extension(root)
    config = make_config(
        tmp_path,
        extensions={
            "paths": [str(root)],
            "enabled": ["crashy"],
            "host": {
                "max_crashes": 3,
                "start_timeout_ms": 5000,
                "tool_timeout_ms": 200,
                "hard_timeout_kill_host": True,
            },
        },
    )
    store = Store(config.agent.data_dir / "qqagent.db")
    await store.init()
    mgr = ExtensionManager(config, store, None)
    await mgr.start()
    try:
        ctx = await make_ctx(store, config)
        result = await tool(mgr, "crashy__sleep").handler(ctx, {})
        assert not result.ok
        assert "超时" in result.text

        ping = None
        for _ in range(300):
            try:
                candidate = await tool(mgr, "crashy__ping").handler(ctx, {})
                if candidate.ok and candidate.text == "pong":
                    ping = candidate
                    break
            except Exception:
                pass
            await asyncio.sleep(0.02)
        assert ping is not None
    finally:
        await mgr.stop()
        await store.close()
