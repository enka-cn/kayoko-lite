from __future__ import annotations

from qqagent.core.events import (
    MessageEvent,
    NoticeEvent,
    is_at_all,
    is_at_user,
    normalize_event,
)


def test_group_message_normalization() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 10001,
        "group_id": 123456,
        "user_id": 20002,
        "message_id": 9001,
        "time": 1_700_000_000,
        "sender": {"user_id": 20002, "nickname": "小明", "card": "明明"},
        "message": [
            {"type": "at", "data": {"qq": "10001", "name": "小助手"}},
            {"type": "text", "data": {"text": " 你好"}},
            {"type": "image", "data": {"file": "a.jpg"}},
        ],
    }
    event = normalize_event(raw)
    assert isinstance(event, MessageEvent)
    assert event.chat_id == "group:123456"
    assert event.chat_type == "group"
    assert event.nickname == "明明"
    assert event.text == "@小助手 你好[图片]"
    assert not event.is_self
    assert is_at_user(event.segments, "10001")
    assert not is_at_all(event.segments)
    assert event.ts == 1_700_000_000_000


def test_message_sent_is_self() -> None:
    raw = {
        "post_type": "message_sent",
        "message_type": "group",
        "self_id": 10001,
        "group_id": 1,
        "user_id": 10001,
        "message_id": 5,
        "sender": {"nickname": "bot"},
        "message": [{"type": "text", "data": {"text": "hi"}}],
    }
    event = normalize_event(raw)
    assert isinstance(event, MessageEvent)
    assert event.is_self


def test_private_message_and_reply() -> None:
    raw = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 1,
        "user_id": 2,
        "message_id": 3,
        "sender": {"nickname": "A"},
        "message": [
            {"type": "reply", "data": {"id": "77"}},
            {"type": "text", "data": {"text": "hello"}},
        ],
    }
    event = normalize_event(raw)
    assert isinstance(event, MessageEvent)
    assert event.chat_id == "private:2"
    assert event.reply_to == "77"
    assert event.text == "hello"


def test_notice_poke() -> None:
    raw = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "self_id": 1,
        "group_id": 10,
        "user_id": 2,
        "target_id": 1,
        "time": 100,
    }
    event = normalize_event(raw)
    assert isinstance(event, NoticeEvent)
    assert event.chat_id == "group:10"
    assert event.notice_type == "notify"
    assert event.sub_type == "poke"
    assert event.target_id == "1"


def test_unknown_event_ignored() -> None:
    assert normalize_event({"post_type": "meta_event", "meta_event_type": "heartbeat"}) is None
