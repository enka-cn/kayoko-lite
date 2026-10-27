from __future__ import annotations

from pathlib import Path

import pytest

from qqagent.config import Config
from qqagent.core.events import MessageEvent
from qqagent.core.store import Store
from qqagent.core.trigger import TriggerPolicy


def make_config(**trigger: object) -> Config:
    return Config.model_validate(
        {
            "llm": {"api_key": "x"},
            "trigger": {
                "allow_all_groups": False,
                "allowed_groups": ["100"],
                **trigger,
            },
        }
    )


def make_event(
    chat_id: str = "group:100",
    text: str = "hello",
    segments: list[dict] | None = None,
    reply_to: str | None = None,
    user_id: str = "2",
) -> MessageEvent:
    group_id = chat_id.split(":", 1)[1] if chat_id.startswith("group:") else None
    return MessageEvent(
        chat_id=chat_id,
        chat_type="group" if group_id else "private",
        group_id=group_id,
        user_id=user_id,
        nickname="tester",
        self_id="1",
        message_id="10",
        text=text,
        segments=segments or [{"type": "text", "data": {"text": text}}],
        reply_to=reply_to,
        is_self=False,
        ts=1,
    )


@pytest.fixture
async def store(tmp_path: Path):
    instance = Store(tmp_path / "test.db")
    await instance.init()
    yield instance
    await instance.close()


async def test_group_not_allowed(store: Store) -> None:
    policy = TriggerPolicy(make_config(), store)
    decision = await policy.should_respond(make_event(chat_id="group:999"))
    assert not decision.respond
    assert decision.reason == "group_not_allowed"


async def test_mention(store: Store) -> None:
    policy = TriggerPolicy(make_config(), store)
    event = make_event(
        segments=[
            {"type": "at", "data": {"qq": "1"}},
            {"type": "text", "data": {"text": " hi"}},
        ]
    )
    decision = await policy.should_respond(event)
    assert decision.respond
    assert decision.reason == "mention"


async def test_no_trigger(store: Store) -> None:
    policy = TriggerPolicy(make_config(), store)
    decision = await policy.should_respond(make_event())
    assert not decision.respond


async def test_keyword(store: Store) -> None:
    policy = TriggerPolicy(make_config(default="keyword", keywords=["小助手"]), store)
    decision = await policy.should_respond(make_event(text="小助手在吗"))
    assert decision.respond
    assert "keyword" in decision.reason


async def test_always(store: Store) -> None:
    policy = TriggerPolicy(make_config(default="always"), store)
    decision = await policy.should_respond(make_event())
    assert decision.respond
    assert decision.reason == "always"


async def test_reply_to_self(store: Store) -> None:
    await store.upsert_chat("group:100", "group")
    await store.add_self_message("group:100", "my message", "77")
    policy = TriggerPolicy(make_config(), store)
    decision = await policy.should_respond(make_event(reply_to="77"))
    assert decision.respond
    assert decision.reason == "reply_to_self"


async def test_cooldown(store: Store) -> None:
    policy = TriggerPolicy(make_config(default="always", cooldown_seconds=60), store)
    event = make_event()
    assert (await policy.should_respond(event)).respond
    policy.record_response(event.chat_id)
    decision = await policy.should_respond(event)
    assert not decision.respond
    assert decision.reason == "cooldown"


async def test_private_allowed(store: Store) -> None:
    policy = TriggerPolicy(make_config(allow_private=True), store)
    decision = await policy.should_respond(make_event(chat_id="private:2"))
    assert decision.respond
    assert decision.reason == "always"


async def test_group_override_disabled(store: Store) -> None:
    policy = TriggerPolicy(
        make_config(groups={"100": {"enabled": False}}),
        store,
    )
    decision = await policy.should_respond(make_event())
    assert not decision.respond
    assert decision.reason == "group_disabled"
