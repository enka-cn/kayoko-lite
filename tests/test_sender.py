from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from qqagent.config import Config
from qqagent.core.sender import (
    DuplicateMessageError,
    RateLimitError,
    SendQueue,
    split_text,
)
from qqagent.core.store import Store


class FakeOneBot:
    self_id = "1"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[dict[str, Any]]]] = []
        self._next_id = 100

    async def send_group_msg(self, group_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        self._next_id += 1
        self.sent.append(("group", str(group_id), segments))
        return {"message_id": self._next_id}

    async def send_private_msg(self, user_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        self._next_id += 1
        self.sent.append(("private", str(user_id), segments))
        return {"message_id": self._next_id}


def make_config(**limits: object) -> Config:
    return Config.model_validate(
        {
            "llm": {"api_key": "x"},
            "limits": {
                "min_delay_ms": 0,
                "max_delay_ms": 0,
                **limits,
            },
        }
    )


@pytest.fixture
async def store(tmp_path: Path):
    instance = Store(tmp_path / "test.db")
    await instance.init()
    await instance.upsert_chat("group:1", "group")
    yield instance
    await instance.close()


def test_split_text_prefers_sentences() -> None:
    text = "第一句。第二句！第三句？"
    assert split_text(text, 100) == [text]
    chunks = split_text(text, 5)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 5 for chunk in chunks)


def test_split_text_hard_cut() -> None:
    text = "x" * 25
    chunks = split_text(text, 10)
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]


async def test_send_text_and_record(store: Store) -> None:
    bot = FakeOneBot()
    queue = SendQueue(make_config(), bot, store)
    ids = await queue.send_text("group:1", "你好")
    assert len(ids) == 1
    assert bot.sent[0][0] == "group"
    assert bot.sent[0][2] == [{"type": "text", "data": {"text": "你好"}}]
    assert await store.unread_count("group:1") == 0
    self_message = await store.get_message("group:1", ids[0])
    assert self_message is not None
    assert self_message["is_self"]


async def test_send_multiple_chunks(store: Store) -> None:
    bot = FakeOneBot()
    queue = SendQueue(make_config(max_message_chars=4), bot, store)
    await queue.send_text("group:1", "一二三四五六七八")
    assert len(bot.sent) == 2


async def test_duplicate_rejected(store: Store) -> None:
    bot = FakeOneBot()
    queue = SendQueue(make_config(), bot, store)
    await queue.send_text("group:1", "重复")
    with pytest.raises(DuplicateMessageError):
        await queue.send_text("group:1", "重复")


async def test_rate_limit(store: Store) -> None:
    bot = FakeOneBot()
    queue = SendQueue(make_config(send_per_minute=1, send_per_hour=10), bot, store)
    await queue.send_text("group:1", "第一条")
    with pytest.raises(RateLimitError):
        await queue.send_text("group:1", "第二条")


async def test_reply_segment_first_chunk(store: Store) -> None:
    bot = FakeOneBot()
    queue = SendQueue(make_config(), bot, store)
    original = await store.add_self_message("group:1", "原消息", "55")
    assert original is not None
    await queue.send_text("group:1", "回复", reply_to="55")
    segments = bot.sent[0][2]
    assert segments[0] == {"type": "reply", "data": {"id": "55"}}
