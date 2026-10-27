from __future__ import annotations

from pathlib import Path

import pytest

from qqagent.core.events import MessageEvent
from qqagent.core.store import Store


def make_event(
    chat_id: str = "group:1",
    message_id: str = "100",
    text: str = "hello",
    user_id: str = "2",
    is_self: bool = False,
    ts: int = 1_000,
) -> MessageEvent:
    return MessageEvent(
        chat_id=chat_id,
        chat_type="group" if chat_id.startswith("group:") else "private",
        group_id=chat_id.split(":")[1] if chat_id.startswith("group:") else None,
        user_id=user_id,
        nickname="tester",
        self_id="1",
        message_id=message_id,
        text=text,
        segments=[{"type": "text", "data": {"text": text}}],
        reply_to=None,
        is_self=is_self,
        ts=ts,
    )


@pytest.fixture
async def store(tmp_path: Path):
    instance = Store(tmp_path / "test.db")
    await instance.init()
    yield instance
    await instance.close()


async def test_add_and_dedupe(store: Store) -> None:
    await store.upsert_chat("group:1", "group", "群一")
    row_id = await store.add_message(make_event())
    assert row_id is not None
    assert await store.add_message(make_event()) is None
    assert await store.unread_count("group:1") == 1


async def test_recent_and_unread(store: Store) -> None:
    await store.upsert_chat("group:1", "group")
    for index in range(5):
        await store.add_message(make_event(message_id=str(index), text=f"m{index}"))
    recent = await store.recent_messages("group:1", 3)
    assert [item["text"] for item in recent] == ["m2", "m3", "m4"]

    unread = await store.unread_messages("group:1")
    assert len(unread) == 5
    await store.mark_processed_until("group:1", unread[2]["id"])
    assert await store.unread_count("group:1") == 2


async def test_self_message_is_processed(store: Store) -> None:
    await store.upsert_chat("group:1", "group")
    await store.add_self_message("group:1", "reply", "500")
    assert await store.unread_count("group:1") == 0
    message = await store.get_message("group:1", "500")
    assert message is not None
    assert message["is_self"] is True


async def test_prune_only_processed(store: Store) -> None:
    await store.upsert_chat("group:1", "group")
    await store.add_message(make_event(message_id="old", ts=0))
    await store.add_message(make_event(message_id="new", ts=10**15))
    await store.mark_all_processed("group:1")
    removed = await store.prune(retention_days=1, now_ms=10**15)
    assert removed == 1
    assert await store.get_message("group:1", "old") is None
    assert await store.get_message("group:1", "new") is not None


async def test_extension_kv(store: Store) -> None:
    assert await store.kv_get("ext", "k") is None
    await store.kv_set("ext", "k", "v1")
    assert await store.kv_get("ext", "k") == "v1"
    await store.kv_set("ext", "k", "v2")
    assert await store.kv_get("ext", "k") == "v2"
    await store.kv_delete("ext", "k")
    assert await store.kv_get("ext", "k") is None
