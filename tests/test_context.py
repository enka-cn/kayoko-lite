from __future__ import annotations

from pathlib import Path

import pytest

from qqagent.config import Config
from qqagent.core.context import ContextBuilder
from qqagent.core.events import MessageEvent
from qqagent.core.store import Store


def make_config(max_chars: int = 24000, max_messages: int = 40) -> Config:
    return Config.model_validate(
        {
            "llm": {"api_key": "x"},
            "context": {"max_chars": max_chars, "max_messages": max_messages},
        }
    )


def make_event(text: str, message_id: str, user_id: str = "2", nickname: str = "小明") -> MessageEvent:
    return MessageEvent(
        chat_id="group:1",
        chat_type="group",
        group_id="1",
        user_id=user_id,
        nickname=nickname,
        self_id="1",
        message_id=message_id,
        text=text,
        segments=[{"type": "text", "data": {"text": text}}],
        reply_to=None,
        is_self=False,
        ts=1_700_000_000_000,
    )


@pytest.fixture
async def store(tmp_path: Path):
    instance = Store(tmp_path / "test.db")
    await instance.init()
    await instance.upsert_chat("group:1", "group")
    yield instance
    await instance.close()


async def test_context_format(store: Store) -> None:
    await store.add_message(make_event("第一句", "1"))
    await store.add_message(make_event("第二句", "2"))
    builder = ContextBuilder(make_config(), store)
    result = await builder.build("group:1")
    assert "小明: 第一句" in result.text
    assert "小明: 第二句" in result.text
    assert result.last_row_id is not None
    assert result.unread_count == 2


async def test_context_trimming(store: Store) -> None:
    for index in range(10):
        await store.add_message(make_event(f"消息{index}" + "x" * 50, str(index)))
    builder = ContextBuilder(make_config(max_chars=200), store)
    result = await builder.build("group:1")
    assert len(result.text) <= 200 + 100
    assert "消息9" in result.text
    assert "消息0" not in result.text


async def test_context_quote_preview(store: Store) -> None:
    quoted = make_event("原来的话", "1")
    await store.add_message(quoted)
    reply = make_event("回复", "2")
    reply.reply_to = "1"
    await store.add_message(reply)
    builder = ContextBuilder(make_config(), store)
    result = await builder.build("group:1")
    assert "↳ 小明: 原来的话" in result.text


async def test_empty_context(store: Store) -> None:
    builder = ContextBuilder(make_config(), store)
    result = await builder.build("group:1")
    assert result.text == ""
    assert result.last_row_id is None
