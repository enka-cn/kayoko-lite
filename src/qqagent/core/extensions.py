"""Extension manifest discovery and shared-host process management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import qqagent
from qqagent.config import Config, HostConfig
from qqagent.rpc import RPCChannel, RPCError, RPCTimeout

from .store import Store
from .tools import TOOL_NAME_RE, Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]

EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
READ_ONLY_ACTIONS = {
    "get_login_info",
    "get_group_info",
    "get_group_member_info",
    "get_group_member_list",
    "get_group_list",
    "get_friend_list",
    "get_msg",
    "get_forward_msg",
}


class ExtensionError(RuntimeError):
    pass


@dataclass(slots=True)
class Permissions:
    onebot: list[str] = field(default_factory=list)
    message_send: bool = False
    storage: bool = False
    network: list[str] = field(default_factory=list)
    llm: bool = False
    scheduler: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "onebot": list(self.onebot),
            "message_send": self.message_send,
            "storage": self.storage,
            "network": list(self.network),
            "llm": self.llm,
            "scheduler": self.scheduler,
        }


@dataclass(slots=True)
class ExtensionManifest:
    id: str
    path: Path
    version: str
    api_version: int
    kind: str
    enabled: bool
    priority: int
    description: str
    permissions: Permissions
    config: dict[str, Any]


@dataclass(slots=True)
class RegisteredExtension:
    manifest: ExtensionManifest
    hooks: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)

    def hook_priority(self, name: str) -> int | None:
        priorities = [int(item.get("priority", 100)) for item in self.hooks if item.get("name") == name]
        return min(priorities) if priorities else None


def _read_manifest(extension_dir: Path) -> ExtensionManifest:
    path = extension_dir / "extension.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ExtensionError(f"missing extension.toml: {extension_dir}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ExtensionError(f"invalid TOML in {path}: {exc}") from exc

    ext = data.get("extension")
    if not isinstance(ext, dict):
        raise ExtensionError(f"{path}: missing [extension] table")
    ext_id = str(ext.get("id", "")).strip()
    if not EXTENSION_ID_RE.match(ext_id):
        raise ExtensionError(f"{path}: invalid extension id {ext_id!r}")
    api_version = int(ext.get("api_version", 1))
    if api_version != 1:
        raise ExtensionError(f"{path}: unsupported api_version {api_version}")
    kind = str(ext.get("kind", "plugin"))
    if kind not in {"plugin", "skill"}:
        raise ExtensionError(f"{path}: kind must be 'plugin' or 'skill'")

    permissions_raw = data.get("permissions") or {}
    if not isinstance(permissions_raw, dict):
        raise ExtensionError(f"{path}: [permissions] must be a table")
    onebot = permissions_raw.get("onebot") or []
    network = permissions_raw.get("network") or []
    if not isinstance(onebot, list):
        raise ExtensionError(f"{path}: permissions.onebot must be a list")
    if not isinstance(network, list):
        raise ExtensionError(f"{path}: permissions.network must be a list")
    permissions = Permissions(
        onebot=[str(item) for item in onebot],
        message_send=bool(permissions_raw.get("message_send", False)),
        storage=bool(permissions_raw.get("storage", False)),
        network=[str(item) for item in network],
        llm=bool(permissions_raw.get("llm", False)),
        scheduler=bool(permissions_raw.get("scheduler", False)),
    )
    config = data.get("config") or {}
    if not isinstance(config, dict):
        raise ExtensionError(f"{path}: [config] must be a table")

    return ExtensionManifest(
        id=ext_id,
        path=extension_dir.resolve(),
        version=str(ext.get("version", "0.0.0")),
        api_version=api_version,
        kind=kind,
        enabled=bool(ext.get("enabled", True)),
        priority=int(ext.get("priority", 100)),
        description=str(ext.get("description", "")),
        permissions=permissions,
        config=dict(config),
    )


def discover_manifests(
    paths: list[Path], enabled_ids: list[str]
) -> list[ExtensionManifest]:
    candidates: list[Path] = []
    for path in paths:
        path = path.resolve()
        if (path / "extension.toml").is_file():
            candidates.append(path)
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_dir() and (child / "extension.toml").is_file():
                    candidates.append(child)
        else:
            log.warning("extension path does not exist: %s", path)

    manifests: dict[str, ExtensionManifest] = {}
    for candidate in candidates:
        manifest = _read_manifest(candidate)
        if manifest.id in manifests:
            raise ExtensionError(
                f"duplicate extension id {manifest.id!r}: "
                f"{manifests[manifest.id].path} and {manifest.path}"
            )
        manifests[manifest.id] = manifest

    if enabled_ids:
        missing = sorted(set(enabled_ids) - set(manifests))
        for ext_id in missing:
            log.warning("enabled extension not found: %s", ext_id)
        selected = [manifests[ext_id] for ext_id in enabled_ids if ext_id in manifests]
    else:
        selected = [manifest for manifest in manifests.values() if manifest.enabled]

    return sorted(selected, key=lambda manifest: (manifest.priority, manifest.id))


class ExtensionManager:
    def __init__(self, config: Config, store: Store, onebot: Any) -> None:
        self.config = config
        self.store = store
        self.onebot = onebot
        self.sender: Any | None = None
        self._manifests: list[ExtensionManifest] = []
        self._registered: dict[str, RegisteredExtension] = {}
        self._channel: RPCChannel | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._disabled = False
        self._crash_times: list[float] = []
        self._start_lock = asyncio.Lock()
        self._host_dir = config.agent.data_dir / "extension_host"

    @property
    def enabled(self) -> bool:
        return bool(self._manifests) and not self._disabled

    @property
    def registrations(self) -> dict[str, RegisteredExtension]:
        return dict(self._registered)

    def bind_sender(self, sender: Any) -> None:
        self.sender = sender

    async def start(self) -> None:
        self._manifests = discover_manifests(
            self.config.extensions.paths, self.config.extensions.enabled
        )
        if not self._manifests:
            log.info("no extensions enabled")
            return
        await self._start_host()

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._channel is not None:
            try:
                await self._channel.request("shutdown", timeout=2.0)
            except Exception:
                pass
        await self._terminate_host()
        if self._monitor_task is not None:
            try:
                await asyncio.wait_for(self._monitor_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._monitor_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None
        self._registered.clear()

    # ------------------------------------------------------------------ host
    async def _start_host(self) -> None:
        async with self._start_lock:
            if self._stopping or self._disabled:
                return
            if self._proc is not None and self._proc.returncode is None:
                return
            self._host_dir.mkdir(parents=True, exist_ok=True)
            (self._host_dir / "tmp").mkdir(parents=True, exist_ok=True)
            env = self._host_env()
            preexec = self._make_preexec(self.config.extensions.host)
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "qqagent.host.main",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self._host_dir),
                    env=env,
                    start_new_session=True,
                    preexec_fn=preexec if resource is not None else None,
                )
            except Exception as exc:
                self._proc = None
                raise ExtensionError(f"failed to spawn extension host: {exc}") from exc

            assert self._proc.stdout is not None and self._proc.stdin is not None
            channel = RPCChannel(
                self._proc.stdout,
                self._proc.stdin,
                request_handler=self._handle_capability,
                notification_handler=self._handle_notification,
            )
            channel.start()
            self._channel = channel
            self._stderr_task = asyncio.create_task(
                self._read_stderr(self._proc), name="extension-stderr"
            )

            params = {
                "protocol_version": 1,
                "max_concurrency": self.config.extensions.host.max_concurrency_per_extension,
                "extensions": [
                    {
                        "id": manifest.id,
                        "path": str(manifest.path),
                        "config": manifest.config,
                        "permissions": manifest.permissions.as_dict(),
                    }
                    for manifest in self._manifests
                ],
            }
            try:
                result = await channel.request(
                    "initialize",
                    params,
                    timeout=self.config.extensions.host.start_timeout_ms / 1000,
                )
            except Exception:
                await self._terminate_host()
                raise
            self._apply_registrations(result)
            self._monitor_task = asyncio.create_task(
                self._monitor_host(self._proc, channel), name="extension-monitor"
            )
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(self._proc), name="extension-watchdog"
            )
            log.info(
                "extension host started: loaded=%s",
                ", ".join(sorted(self._registered)) or "(none)",
            )

    def _apply_registrations(self, result: Any) -> None:
        if not isinstance(result, dict):
            raise ExtensionError("extension host returned an invalid initialize response")
        failed = result.get("failed") or []
        for item in failed:
            log.error(
                "extension failed to load: %s: %s",
                item.get("id", "?"),
                item.get("error", "unknown error"),
            )
        loaded = set(result.get("loaded") or [])
        self._registered.clear()
        for registration in result.get("registrations") or []:
            ext_id = str(registration.get("id", ""))
            if ext_id not in loaded:
                continue
            manifest = next(
                (item for item in self._manifests if item.id == ext_id), None
            )
            if manifest is None:
                continue
            self._registered[ext_id] = RegisteredExtension(
                manifest=manifest,
                hooks=list(registration.get("hooks") or []),
                tools=list(registration.get("tools") or []),
                prompts=list(registration.get("prompts") or []),
            )

    async def _monitor_host(
        self, proc: asyncio.subprocess.Process, channel: RPCChannel
    ) -> None:
        code = await proc.wait()
        if self._stopping:
            return
        log.warning("extension host exited with code %s", code)
        await channel.close()
        self._channel = None
        self._proc = None
        self._registered.clear()
        now = time.monotonic()
        self._crash_times = [
            item
            for item in self._crash_times
            if now - item < self.config.extensions.host.crash_window_seconds
        ]
        self._crash_times.append(now)
        if len(self._crash_times) >= self.config.extensions.host.max_crashes:
            self._disabled = True
            log.error(
                "extension host crashed %s times in %ss; disabling extensions",
                len(self._crash_times),
                self.config.extensions.host.crash_window_seconds,
            )
            return
        await asyncio.sleep(1.0)
        if self._stopping:
            return
        try:
            await self._start_host()
        except Exception:
            log.exception("failed to restart extension host")

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                log.info("host: %s", text)

    async def _watchdog_loop(self, proc: asyncio.subprocess.Process) -> None:
        if sys.platform != "linux":
            return
        limit_kb = self.config.extensions.host.memory_limit_mb * 1024
        interval = self.config.extensions.host.watchdog_interval_seconds
        while proc.returncode is None:
            await asyncio.sleep(max(0.5, interval))
            if proc.returncode is not None:
                return
            try:
                status = Path(f"/proc/{proc.pid}/status").read_text()
            except OSError:
                return
            rss_kb = 0
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    try:
                        rss_kb = int(line.split()[1])
                    except (IndexError, ValueError):
                        pass
                    break
            if limit_kb and rss_kb > limit_kb:
                log.error(
                    "extension host RSS %s KiB exceeds limit %s KiB; killing it",
                    rss_kb,
                    limit_kb,
                )
                proc.kill()
                return

    async def _terminate_host(self) -> None:
        channel, proc = self._channel, self._proc
        self._channel = None
        self._proc = None
        if channel is not None:
            await channel.close()
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

    def _host_env(self) -> dict[str, str]:
        allowed = ("PATH", "LANG", "LC_ALL", "VIRTUAL_ENV", "PYTHONPATH")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        package_src = str(Path(qqagent.__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = package_src + (os.pathsep + existing if existing else "")
        env["HOME"] = str(self._host_dir)
        env["TMPDIR"] = str(self._host_dir / "tmp")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _make_preexec(self, settings: HostConfig):
        def preexec() -> None:  # pragma: no cover - executed in the child process
            if resource is None:
                return
            if settings.memory_limit_mb > 0:
                limit = settings.memory_limit_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            if settings.nofile > 0:
                resource.setrlimit(resource.RLIMIT_NOFILE, (settings.nofile, settings.nofile))
            if settings.nproc > 0:
                resource.setrlimit(resource.RLIMIT_NPROC, (settings.nproc, settings.nproc))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        return preexec

    # ------------------------------------------------------------ capability
    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "log":
            ext_id = str(params.get("extension_id", "?"))
            level = str(params.get("level", "info")).upper()
            message = str(params.get("message", ""))
            logging.getLogger(f"extension.{ext_id}").log(
                getattr(logging, level, logging.INFO), message
            )
            return

    async def _handle_capability(self, method: str, params: dict[str, Any]) -> Any:
        if method != "capability.call":
            raise RPCError(-32601, f"method not found: {method}")
        ext_id = str(params.get("extension_id", ""))
        capability = str(params.get("capability", ""))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RPCError(-32602, "capability arguments must be an object")
        registered = self._registered.get(ext_id)
        if registered is None:
            raise RPCError(-32004, f"unknown extension: {ext_id}")
        permissions = registered.manifest.permissions

        if capability == "storage.get":
            self._require(permissions.storage, "storage")
            key = str(arguments.get("key", ""))
            return {"value": await self.store.kv_get(ext_id, key)}
        if capability == "storage.set":
            self._require(permissions.storage, "storage")
            key = str(arguments.get("key", ""))
            value = arguments.get("value")
            await self.store.kv_set(ext_id, key, str(value))
            return {"ok": True}
        if capability == "storage.delete":
            self._require(permissions.storage, "storage")
            await self.store.kv_delete(ext_id, str(arguments.get("key", "")))
            return {"ok": True}

        if capability == "message.send_text":
            self._require(permissions.message_send, "message_send")
            if self.sender is None:
                raise RPCError(-32006, "sender is not bound")
            chat_id = str(arguments.get("chat_id", ""))
            text = str(arguments.get("text", ""))
            reply_to = arguments.get("reply_to")
            try:
                ids = await self.sender.send_text(
                    chat_id, text, str(reply_to) if reply_to else None
                )
            except Exception as exc:
                raise RPCError(-32007, f"send failed: {exc}") from exc
            return {"message_ids": ids}

        if capability == "onebot.call":
            action = str(arguments.get("action", ""))
            if action not in permissions.onebot:
                raise RPCError(-32008, f"permission denied for onebot action: {action}")
            if action not in READ_ONLY_ACTIONS:
                raise RPCError(-32009, f"onebot action is not read-only: {action}")
            call_params = arguments.get("params") or {}
            if not isinstance(call_params, dict):
                raise RPCError(-32602, "onebot.call params must be an object")
            return await self.onebot.call(action, call_params)

        raise RPCError(-32010, f"capability not available: {capability}")

    @staticmethod
    def _require(condition: bool, name: str) -> None:
        if not condition:
            raise RPCError(-32011, f"permission denied: {name} required")

    # ----------------------------------------------------------- public API
    async def invoke_hook(
        self, name: str, payload: dict[str, Any], timeout_ms: int | None = None
    ) -> dict[str, Any] | None:
        channel = self._channel
        if channel is None or not self._registered:
            return payload
        candidates: list[tuple[int, str]] = []
        for ext_id, registered in self._registered.items():
            priority = registered.hook_priority(name)
            if priority is not None:
                candidates.append((priority, ext_id))
        candidates.sort()

        timeout = (timeout_ms or self.config.extensions.host.hook_timeout_ms) / 1000
        current: dict[str, Any] | None = payload
        for _, ext_id in candidates:
            try:
                result = await channel.request(
                    "invoke_hook",
                    {"extension_id": ext_id, "hook": name, "payload": current or {}},
                    timeout=timeout,
                )
            except RPCTimeout:
                log.error("hook %s timed out in extension %s", name, ext_id)
                await self._hard_timeout_kill()
                return payload
            except RPCError as exc:
                log.error("hook %s failed in extension %s: %s", name, ext_id, exc)
                continue
            if result is None:
                return None
            if not isinstance(result, dict):
                log.error("hook %s in %s returned a non-object", name, ext_id)
                continue
            current = result
        return current

    async def _hard_timeout_kill(self) -> None:
        if self.config.extensions.host.hard_timeout_kill_host and self._proc is not None:
            if self._proc.returncode is None:
                log.error("killing extension host after hard timeout")
                self._proc.kill()

    def tools(self) -> list[Tool]:
        tools: list[Tool] = []
        for ext_id, registered in sorted(self._registered.items()):
            for item in registered.tools:
                tool_name = str(item.get("name", ""))
                if not tool_name:
                    continue
                public_name = f"{ext_id}__{tool_name}"
                if not TOOL_NAME_RE.match(public_name):
                    log.error("skipping invalid extension tool name: %s", public_name)
                    continue

                async def handler(
                    context: ToolContext,
                    arguments: dict[str, Any],
                    *,
                    _ext_id: str = ext_id,
                    _tool_name: str = tool_name,
                ) -> ToolResult:
                    channel = self._channel
                    if channel is None:
                        return ToolResult("扩展 host 未运行", ok=False)
                    try:
                        result = await channel.request(
                            "invoke_tool",
                            {
                                "extension_id": _ext_id,
                                "tool": _tool_name,
                                "arguments": arguments,
                                "context": {
                                    "chat_id": context.chat_id,
                                    "event": context.event.to_hook_dict()
                                    if context.event is not None
                                    else {},
                                },
                            },
                            timeout=self.config.extensions.host.tool_timeout_ms / 1000,
                        )
                    except RPCTimeout:
                        log.error("extension tool timed out: %s", public_name)
                        await self._hard_timeout_kill()
                        return ToolResult("扩展工具执行超时", ok=False)
                    except RPCError as exc:
                        return ToolResult(f"扩展工具失败：{exc}", ok=False)
                    if isinstance(result, dict) and result.get("ok") is False:
                        return ToolResult(
                            f"扩展工具失败：{result.get('error', 'unknown error')}",
                            ok=False,
                        )
                    value = result.get("result") if isinstance(result, dict) else result
                    if isinstance(value, str):
                        return ToolResult(value)
                    if value is None:
                        return ToolResult("")
                    return ToolResult(json.dumps(value, ensure_ascii=False))

                tools.append(
                    Tool(
                        name=public_name,
                        description=str(item.get("description", "")),
                        parameters=item.get("parameters")
                        or {"type": "object", "properties": {}},
                        handler=handler,
                        extension_id=ext_id,
                    )
                )
        return tools

    def prompt_fragments(self) -> list[str]:
        fragments: list[tuple[int, str, str]] = []
        for ext_id, registered in self._registered.items():
            for item in registered.prompts:
                text = str(item.get("text", "")).strip()
                if text:
                    fragments.append(
                        (int(item.get("priority", 50)), ext_id, text)
                    )
        fragments.sort()
        return [text for _, _, text in fragments]
