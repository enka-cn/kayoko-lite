"""Tool registry and execution for the LLM tool loop."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from qqagent.config import Config

from .events import MessageEvent
from .store import Store

log = logging.getLogger(__name__)

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(slots=True)
class ToolContext:
    chat_id: str
    event: MessageEvent
    store: Store
    onebot: Any
    sender: Any
    config: Config
    extension_id: str | None = None


@dataclass(slots=True)
class ToolResult:
    text: str
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult | str | dict[str, Any]]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    extension_id: str | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not TOOL_NAME_RE.match(tool.name):
            raise ValueError(
                f"invalid tool name {tool.name!r}; use letters, digits, '_' or '-'"
            )
        if tool.name in self._tools:
            existing = self._tools[tool.name]
            raise ValueError(
                f"tool name conflict: {tool.name!r} "
                f"({existing.extension_id or 'core'} vs {tool.extension_id or 'core'})"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(
        self, name: str, arguments_json: str, context: ToolContext
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"错误：未知工具 {name}", ok=False)
        try:
            arguments = json.loads(arguments_json) if arguments_json.strip() else {}
        except ValueError as exc:
            return ToolResult(f"错误：工具参数不是合法 JSON：{exc}", ok=False)
        if not isinstance(arguments, dict):
            return ToolResult("错误：工具参数必须是 JSON 对象", ok=False)
        try:
            jsonschema.validate(arguments, tool.parameters)
        except jsonschema.ValidationError as exc:
            return ToolResult(f"错误：工具参数校验失败：{exc.message}", ok=False)
        try:
            result = await tool.handler(context, arguments)
        except Exception as exc:
            log.exception("tool %s failed", name)
            return ToolResult(f"错误：工具执行失败：{exc}", ok=False)
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, str):
            return ToolResult(result)
        if isinstance(result, dict):
            return ToolResult(json.dumps(result, ensure_ascii=False), data=result)
        return ToolResult(str(result))


SEND_MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
            "description": "消息正文。不要包含 @ 前缀，除非你确实想 @ 某人。",
        },
        "reply_to": {
            "type": "string",
            "description": "可选。要引用/回复的消息 id。",
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}

SEND_MESSAGE_DESCRIPTION = (
    "向当前 QQ 会话发送一条文本消息。调用一次发送一条；"
    "不调用表示保持沉默。只能发送到当前会话。"
)
