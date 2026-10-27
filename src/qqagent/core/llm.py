"""OpenAI-compatible chat client and tool-calling loop."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from openai import APIError, APITimeoutError

from qqagent.config import LLMConfig

from .tools import ToolContext, ToolRegistry

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ChatResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


@dataclass(slots=True)
class LoopResult:
    content: str
    tool_calls: list[dict[str, Any]]
    sent: bool
    usage: dict[str, int]
    rounds: int
    truncated: bool = False


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        api_key = config.api_key.get_secret_value()
        if not api_key:
            raise LLMError("llm.api_key is empty; set it in config or via ${ENV_VAR}")
        self.config = config
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=2,
        )

    async def close(self) -> None:
        await self._client.close()

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ChatResponse:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "timeout": self.config.timeout,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except (APIError, APITimeoutError) as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        if not response.choices:
            raise LLMError("LLM returned no choices")
        message = response.choices[0].message
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            arguments = call.function.arguments
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            calls.append(
                ToolCall(id=str(call.id), name=str(call.function.name), arguments=arguments)
            )
        usage: dict[str, int] = {}
        if response.usage is not None:
            usage = {
                "prompt_tokens": int(response.usage.prompt_tokens or 0),
                "completion_tokens": int(response.usage.completion_tokens or 0),
                "total_tokens": int(response.usage.total_tokens or 0),
            }
        return ChatResponse(
            content=str(message.content or ""),
            tool_calls=calls,
            usage=usage,
            raw=response,
        )


def _add_usage(total: dict[str, int], extra: dict[str, int]) -> None:
    for key, value in extra.items():
        total[key] = total.get(key, 0) + int(value)


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        max_rounds: int = 6,
        fallback_send_text: bool = False,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_rounds = max(1, max_rounds)
        self.fallback_send_text = fallback_send_text

    async def run(
        self, messages: list[dict[str, Any]], context: ToolContext
    ) -> LoopResult:
        conversation = list(messages)
        usage: dict[str, int] = {}
        executed: list[dict[str, Any]] = []
        sent = False
        last_content = ""
        truncated = False

        for round_no in range(1, self.max_rounds + 1):
            response = await self.llm.chat(conversation, self.tools.openai_tools())
            _add_usage(usage, response.usage)
            last_content = response.content
            if not response.tool_calls:
                if self.fallback_send_text and response.content and not sent:
                    result = await self.tools.execute(
                        "send_message",
                        json.dumps({"text": response.content}, ensure_ascii=False),
                        context,
                    )
                    sent = sent or bool(result.data.get("sent"))
                return LoopResult(
                    content=response.content,
                    tool_calls=executed,
                    sent=sent,
                    usage=usage,
                    rounds=round_no,
                    truncated=False,
                )

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in response.tool_calls
                ],
            }
            conversation.append(assistant_message)
            for call in response.tool_calls:
                result = await self.tools.execute(call.name, call.arguments, context)
                sent = sent or bool(result.data.get("sent"))
                executed.append(
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "ok": result.ok,
                        "result": result.text,
                    }
                )
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result.text,
                    }
                )
            if round_no == self.max_rounds:
                truncated = True

        log.warning("tool loop reached max_rounds=%s", self.max_rounds)
        return LoopResult(
            content=last_content,
            tool_calls=executed,
            sent=sent,
            usage=usage,
            rounds=self.max_rounds,
            truncated=truncated,
        )
