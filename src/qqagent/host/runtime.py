"""Load extensions and dispatch hooks/tools inside the host process."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qqagent.rpc import RPCChannel, RPCError
from qqagent.sdk import ExtensionAPI, ToolContext, ToolRegistration

log = logging.getLogger(__name__)

EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(slots=True)
class ExtensionRuntime:
    id: str
    path: Path
    config: dict[str, Any]
    permissions: dict[str, Any]
    module: Any
    api: ExtensionAPI
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class HostRuntime:
    def __init__(self) -> None:
        self.channel: RPCChannel | None = None
        self.extensions: dict[str, ExtensionRuntime] = {}
        self.shutdown_event = asyncio.Event()

    def _channel(self) -> RPCChannel:
        if self.channel is None:
            raise RPCError(-32603, "RPC channel is not initialized")
        return self.channel

    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if int(params.get("protocol_version", 0)) != 1:
            raise RPCError(-32600, "unsupported protocol version")
        max_concurrency = max(1, int(params.get("max_concurrency", 4)))
        loaded: list[str] = []
        failed: list[dict[str, str]] = []
        registrations: list[dict[str, Any]] = []

        for spec in params.get("extensions", []):
            try:
                runtime = await self._load_extension(spec, max_concurrency)
                self.extensions[runtime.id] = runtime
                loaded.append(runtime.id)
                registrations.append(runtime.api.registration_payload())
            except Exception as exc:
                ext_id = str(spec.get("id", "<unknown>"))
                log.exception("failed to load extension %s", ext_id)
                failed.append({"id": ext_id, "error": str(exc)})

        return {"loaded": loaded, "failed": failed, "registrations": registrations}

    async def _load_extension(
        self, spec: dict[str, Any], max_concurrency: int
    ) -> ExtensionRuntime:
        ext_id = str(spec.get("id", "")).strip()
        if not EXTENSION_ID_RE.match(ext_id):
            raise ValueError(f"invalid extension id: {ext_id!r}")
        if ext_id in self.extensions:
            raise ValueError(f"duplicate extension id: {ext_id}")
        path = Path(str(spec.get("path", ""))).resolve()
        init_file = path / "__init__.py"
        if not init_file.is_file():
            raise FileNotFoundError(f"extension entrypoint not found: {init_file}")

        module_name = f"qqagent_ext_{ext_id.replace('-', '_')}"
        module_spec = importlib.util.spec_from_file_location(
            module_name,
            init_file,
            submodule_search_locations=[str(path)],
        )
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"cannot load extension module: {init_file}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module

        # Extensions should use relative imports. This path insertion only
        # helps simple single-file extensions; it is removed immediately.
        sys.path.insert(0, str(path))
        try:
            module_spec.loader.exec_module(module)
        finally:
            try:
                sys.path.remove(str(path))
            except ValueError:
                pass

        api = ExtensionAPI(
            ext_id,
            dict(spec.get("config") or {}),
            self._channel(),
            dict(spec.get("permissions") or {}),
        )
        setup = getattr(module, "setup", None)
        if not callable(setup):
            raise AttributeError("extension must define setup(api)")
        await _maybe_await(setup(api))

        self._validate_registrations(api)
        return ExtensionRuntime(
            id=ext_id,
            path=path,
            config=dict(spec.get("config") or {}),
            permissions=dict(spec.get("permissions") or {}),
            module=module,
            api=api,
            semaphore=asyncio.Semaphore(max_concurrency),
        )

    @staticmethod
    def _validate_registrations(api: ExtensionAPI) -> None:
        seen_tools: set[str] = set()
        for tool in api._tools:
            if tool.name in seen_tools:
                raise ValueError(f"duplicate tool name in extension: {tool.name}")
            seen_tools.add(tool.name)
            if not isinstance(tool.parameters, dict):
                raise ValueError(f"tool {tool.name}: parameters must be a JSON schema object")
        for hook in api._hooks:
            if not callable(hook.handler):
                raise ValueError(f"hook {hook.name}: handler is not callable")

    def _extension(self, extension_id: str) -> ExtensionRuntime:
        runtime = self.extensions.get(extension_id)
        if runtime is None:
            raise RPCError(-32004, f"unknown extension: {extension_id}")
        return runtime

    async def invoke_hook(
        self, extension_id: str, hook_name: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        runtime = self._extension(extension_id)
        handlers = [
            item
            for item in sorted(
                runtime.api._hooks, key=lambda item: (item.priority, item.name)
            )
            if item.name == hook_name
        ]
        if not handlers:
            return payload
        current: dict[str, Any] | None = payload
        for registration in handlers:
            async with runtime.semaphore:
                result = await _maybe_await(registration.handler(current or {}))
            if result is None:
                return None
            if not isinstance(result, dict):
                raise RPCError(
                    -32603,
                    f"hook {hook_name} in {extension_id} returned "
                    f"{type(result).__name__}; expected dict or None",
                )
            current = result
        return current

    async def invoke_tool(
        self,
        extension_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        runtime = self._extension(extension_id)
        registration: ToolRegistration | None = None
        for item in runtime.api._tools:
            if item.name == tool_name:
                registration = item
                break
        if registration is None:
            raise RPCError(-32005, f"unknown tool: {extension_id}.{tool_name}")

        tool_context = ToolContext(
            chat_id=str(context.get("chat_id") or ""),
            event=dict(context.get("event") or {}),
            extension_id=extension_id,
            config=runtime.config,
            capabilities=runtime.api.capabilities,
            storage=runtime.api.storage,
            log=runtime.api.log,
        )
        try:
            async with runtime.semaphore:
                result = await _maybe_await(
                    registration.handler(tool_context, dict(arguments))
                )
        except Exception as exc:
            log.exception("extension tool failed: %s.%s", extension_id, tool_name)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": _jsonable(result)}

    async def handle_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return await self.initialize(params)
        if method == "invoke_hook":
            return await self.invoke_hook(
                str(params.get("extension_id", "")),
                str(params.get("hook", "")),
                dict(params.get("payload") or {}),
            )
        if method == "invoke_tool":
            return await self.invoke_tool(
                str(params.get("extension_id", "")),
                str(params.get("tool", "")),
                dict(params.get("arguments") or {}),
                dict(params.get("context") or {}),
            )
        if method == "ping":
            return {"pong": True}
        if method == "shutdown":
            self.shutdown_event.set()
            return {"ok": True}
        raise RPCError(-32601, f"method not found: {method}")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)
