from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from qqagent.config import Config
from qqagent.core.llm import AgentLoop, ChatResponse, ToolCall
from qqagent.core.tools import Tool, ToolRegistry, ToolResult


class FakeLLM:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResponse:
        self.calls.append(messages)
        if not self.responses:
            return ChatResponse(content="", tool_calls=[])
        return self.responses.pop(0)


def make_context() -> SimpleNamespace:
    return SimpleNamespace(
        chat_id="group:1",
        event=None,
        store=None,
        onebot=None,
        sender=None,
        config=Config.model_validate({"llm": {"api_key": "x"}}),
    )


async def test_tool_loop_sends_and_stops() -> None:
    llm = FakeLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="send_message", arguments='{"text":"hi"}')],
                usage={"prompt_tokens": 10, "completion_tokens": 2},
            ),
            ChatResponse(content="", tool_calls=[], usage={"prompt_tokens": 5, "completion_tokens": 1}),
        ]
    )
    tools = ToolRegistry()

    async def handler(context, args):
        return ToolResult("已发送", data={"sent": True})

    tools.register(Tool("send_message", "send", {"type": "object"}, handler))
    loop = AgentLoop(llm, tools, max_rounds=3)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())
    assert result.sent
    assert result.rounds == 2
    assert len(result.tool_calls) == 1
    assert result.usage["prompt_tokens"] == 15
    assert result.usage["completion_tokens"] == 3


async def test_tool_loop_truncates_at_max_rounds() -> None:
    responses = [
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id=str(i), name="noop", arguments="{}")],
        )
        for i in range(3)
    ]
    llm = FakeLLM(responses)
    tools = ToolRegistry()

    async def handler(context, args):
        return "ok"

    tools.register(Tool("noop", "noop", {"type": "object"}, handler))
    loop = AgentLoop(llm, tools, max_rounds=2)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())
    assert result.truncated
    assert result.rounds == 2
    assert len(result.tool_calls) == 2


async def test_unknown_tool_returns_error() -> None:
    llm = FakeLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="missing", arguments="{}")],
            ),
            ChatResponse(content="", tool_calls=[]),
        ]
    )
    loop = AgentLoop(llm, ToolRegistry(), max_rounds=2)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())
    assert not result.sent
    assert "未知工具" in result.tool_calls[0]["result"]


async def test_fallback_send_text() -> None:
    llm = FakeLLM([ChatResponse(content="直接说", tool_calls=[])])
    tools = ToolRegistry()

    async def handler(context, args):
        return ToolResult("已发送", data={"sent": True})

    tools.register(Tool("send_message", "send", {"type": "object"}, handler))
    loop = AgentLoop(llm, tools, max_rounds=2, fallback_send_text=True)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())
    assert result.sent
    assert result.content == "直接说"


async def test_invalid_tool_arguments() -> None:
    llm = FakeLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="echo", arguments="not-json")],
            ),
            ChatResponse(content="", tool_calls=[]),
        ]
    )
    tools = ToolRegistry()

    async def handler(context, args):
        return json.dumps(args)

    tools.register(
        Tool(
            "echo",
            "echo",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            handler,
        )
    )
    loop = AgentLoop(llm, tools, max_rounds=2)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())
    assert "不是合法 JSON" in result.tool_calls[0]["result"]
