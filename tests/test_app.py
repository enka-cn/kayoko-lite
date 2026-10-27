from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from qqagent.config import Config
from qqagent.core.app import App
from qqagent.core.llm import ChatResponse, ToolCall


class FakeOneBot:
    self_id = "10001"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[dict[str, Any]]]] = []
        self._next_id = 1

    async def send_group_msg(self, group_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        self._next_id += 1
        self.sent.append(("group", str(group_id), segments))
        return {"message_id": self._next_id}

    async def send_private_msg(self, user_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        self._next_id += 1
        self.sent.append(("private", str(user_id), segments))
        return {"message_id": self._next_id}


class FakeLLM:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResponse:
        self.calls += 1
        if not self.responses:
            return ChatResponse(content="", tool_calls=[])
        return self.responses.pop(0)


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw = {
        "agent": {"data_dir": str(tmp_path / "data")},
        "llm": {"api_key": "x"},
        "trigger": {"allow_all_groups": True, "default": "mention"},
        "limits": {"min_delay_ms": 0, "max_delay_ms": 0},
    }
    raw.update(overrides)
    return Config.model_validate(raw)


def group_message(text: str = "你好", at: bool = True, message_id: int = 1) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    if at:
        segments.append({"type": "at", "data": {"qq": "10001", "name": "bot"}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 10001,
        "group_id": 123,
        "user_id": 20002,
        "message_id": message_id,
        "time": 1_700_000_000,
        "sender": {"user_id": 20002, "nickname": "小明", "card": "明明"},
        "message": segments,
    }


async def wait_for(condition, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


async def test_app_replies_to_mention(tmp_path: Path) -> None:
    bot = FakeOneBot()
    llm = FakeLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="send_message", arguments='{"text":"你好呀"}')],
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            ),
            ChatResponse(content="", tool_calls=[]),
        ]
    )
    app = App(make_config(tmp_path), onebot=bot, llm=llm)
    await app.start()
    try:
        await app.handle_event(group_message())
        await wait_for(lambda: bool(bot.sent))
        assert bot.sent[0][0] == "group"
        assert bot.sent[0][1] == "123"
        assert bot.sent[0][2][-1] == {"type": "text", "data": {"text": "你好呀"}}
        assert llm.calls == 2
    finally:
        await app.stop()


async def test_app_ignores_non_mention(tmp_path: Path) -> None:
    bot = FakeOneBot()
    llm = FakeLLM([])
    app = App(make_config(tmp_path), onebot=bot, llm=llm)
    await app.start()
    try:
        await app.handle_event(group_message(at=False))
        await asyncio.sleep(0.1)
        assert not bot.sent
        assert llm.calls == 0
        unread = await app.store.unread_count("group:123")
        assert unread == 0
    finally:
        await app.stop()


async def test_app_silence_marks_processed(tmp_path: Path) -> None:
    bot = FakeOneBot()
    llm = FakeLLM([ChatResponse(content="", tool_calls=[])])
    app = App(make_config(tmp_path), onebot=bot, llm=llm)
    await app.start()
    try:
        await app.handle_event(group_message())
        await wait_for(lambda: llm.calls == 1)
        await asyncio.sleep(0.1)
        assert not bot.sent
        assert await app.store.unread_count("group:123") == 0
    finally:
        await app.stop()


async def test_app_deduplicates_events(tmp_path: Path) -> None:
    bot = FakeOneBot()
    llm = FakeLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="send_message", arguments='{"text":"一次"}')],
            ),
            ChatResponse(content="", tool_calls=[]),
        ]
    )
    app = App(make_config(tmp_path), onebot=bot, llm=llm)
    await app.start()
    try:
        await app.handle_event(group_message(message_id=7))
        await wait_for(lambda: bool(bot.sent))
        await app.handle_event(group_message(message_id=7))
        await asyncio.sleep(0.1)
        assert len(bot.sent) == 1
    finally:
        await app.stop()
