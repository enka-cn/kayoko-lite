"""Extension-facing API.

Extensions only see this module. They never receive OneBot tokens, the LLM
API key, database handles, or raw network access. Every privileged action is a
capability call mediated by the core.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from qqagent.rpc import RPCChannel

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

HookHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]
ToolHandler = Callable[["ToolContext", dict[str, Any]], Awaitable[Any] | Any]


@dataclass(slots=True)
class HookRegistration:
    name: str
    handler: HookHandler
    priority: int = 100


@dataclass(slots=True)
class ToolRegistration:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    permissions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PromptFragment:
    text: str
    priority: int = 50


class ExtensionLogger:
    def __init__(self, extension_id: str, channel: RPCChannel) -> None:
        self.extension_id = extension_id
        self.channel = channel
        self._fallback = logging.getLogger(f"extension.{extension_id}")

    def _log(self, level: str, message: str, *args: Any) -> None:
        text = message % args if args else message
        payload = {"extension_id": self.extension_id, "level": level, "message": text}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            print(f"[{level}] {self.extension_id}: {text}", file=sys.stderr)
            return
        loop.create_task(self.channel.notify("log", payload))

    def debug(self, message: str, *args: Any) -> None:
        self._log("debug", message, *args)

    def info(self, message: str, *args: Any) -> None:
        self._log("info", message, *args)

    def warning(self, message: str, *args: Any) -> None:
        self._log("warning", message, *args)

    def error(self, message: str, *args: Any) -> None:
        self._log("error", message, *args)


class CapabilitiesAPI:
    def __init__(self, api: "ExtensionAPI") -> None:
        self._api = api

    async def call(
        self, name: str, arguments: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> Any:
        return await self._api._channel.request(
            "capability.call",
            {
                "extension_id": self._api.id,
                "capability": name,
                "arguments": arguments or {},
            },
            timeout=timeout,
        )


class StorageAPI:
    def __init__(self, api: "ExtensionAPI") -> None:
        self._api = api

    async def get(self, key: str, default: str | None = None) -> str | None:
        result = await self._api.capabilities.call("storage.get", {"key": str(key)})
        if isinstance(result, dict):
            value = result.get("value")
            return str(value) if value is not None else default
        return default

    async def set(self, key: str, value: Any) -> None:
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        await self._api.capabilities.call(
            "storage.set", {"key": str(key), "value": str(value)}
        )

    async def delete(self, key: str) -> None:
        await self._api.capabilities.call("storage.delete", {"key": str(key)})


class HooksAPI:
    def __init__(self, api: "ExtensionAPI") -> None:
        self._api = api

    def on(self, name: str, *, priority: int = 100) -> Callable[[HookHandler], HookHandler]:
        def decorator(handler: HookHandler) -> HookHandler:
            self._api._hooks.append(HookRegistration(name, handler, int(priority)))
            return handler

        return decorator


class ToolsAPI:
    def __init__(self, api: "ExtensionAPI") -> None:
        self._api = api

    def register(
        self,
        name: str,
        *,
        description: str,
        parameters: dict[str, Any] | None = None,
        permissions: list[str] | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        if not TOOL_NAME_RE.match(name):
            raise ValueError(
                f"invalid tool name {name!r}; use letters, digits, '_' or '-'"
            )
        schema = parameters or {"type": "object", "properties": {}}

        def decorator(handler: ToolHandler) -> ToolHandler:
            self._api._tools.append(
                ToolRegistration(
                    name=name,
                    description=description,
                    parameters=schema,
                    handler=handler,
                    permissions=list(permissions or []),
                )
            )
            return handler

        return decorator


class PromptsAPI:
    def __init__(self, api: "ExtensionAPI") -> None:
        self._api = api

    def add(self, text: str, *, priority: int = 50) -> None:
        if str(text).strip():
            self._api._prompts.append(PromptFragment(str(text), int(priority)))


class ExtensionAPI:
    """The object passed to an extension's ``setup(api)`` function."""

    def __init__(
        self,
        extension_id: str,
        config: dict[str, Any],
        channel: RPCChannel,
        permissions: dict[str, Any] | None = None,
    ) -> None:
        self.id = extension_id
        self.config = config
        self.permissions = permissions or {}
        self._channel = channel
        self._hooks: list[HookRegistration] = []
        self._tools: list[ToolRegistration] = []
        self._prompts: list[PromptFragment] = []

        self.log = ExtensionLogger(extension_id, channel)
        self.capabilities = CapabilitiesAPI(self)
        self.storage = StorageAPI(self)
        self.hooks = HooksAPI(self)
        self.tools = ToolsAPI(self)
        self.prompts = PromptsAPI(self)

    def registration_payload(self) -> dict[str, Any]:
        hooks = sorted(self._hooks, key=lambda item: (item.priority, item.name))
        prompts = sorted(self._prompts, key=lambda item: (item.priority, item.text))
        return {
            "id": self.id,
            "hooks": [{"name": item.name, "priority": item.priority} for item in hooks],
            "tools": [
                {
                    "name": item.name,
                    "description": item.description,
                    "parameters": item.parameters,
                }
                for item in self._tools
            ],
            "prompts": [{"text": item.text, "priority": item.priority} for item in prompts],
        }


@dataclass(slots=True)
class ToolContext:
    chat_id: str
    event: dict[str, Any]
    extension_id: str
    config: dict[str, Any]
    capabilities: CapabilitiesAPI
    storage: StorageAPI
    log: ExtensionLogger

    async def call(self, capability: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.capabilities.call(capability, arguments, **kwargs)
