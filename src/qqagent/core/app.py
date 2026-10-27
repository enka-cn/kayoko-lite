"""Application orchestration: events -> store -> trigger -> LLM -> sender."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from qqagent.config import Config

from .context import ContextBuilder
from .events import MessageEvent, NoticeEvent, normalize_event
from .extensions import ExtensionManager
from .llm import AgentLoop, LLMClient, LLMError
from .onebot import OneBotClient
from .prompt import PromptBuilder
from .sender import SendQueue, SenderError
from .store import Store
from .tools import (
    SEND_MESSAGE_DESCRIPTION,
    SEND_MESSAGE_SCHEMA,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
)
from .trigger import TriggerPolicy

log = logging.getLogger(__name__)

MAX_TURNS_PER_WAKE = 4


class NullExtensions:
    """No-op extension manager used when no extensions are configured."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def invoke_hook(
        self, name: str, payload: dict[str, Any], timeout_ms: int | None = None
    ) -> dict[str, Any] | None:
        return payload

    def tools(self) -> list[Tool]:
        return []

    def prompt_fragments(self) -> list[str]:
        return []


class ChatWorker:
    def __init__(self, app: "App", chat_id: str) -> None:
        self.app = app
        self.chat_id = chat_id
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        if not self.app._started:
            return
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"chat-{self.chat_id}")

    async def _run(self) -> None:
        turns = 0
        cancelled = False
        try:
            while True:
                if await self.app.store.unread_count(self.chat_id) == 0:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                    except TimeoutError:
                        return
                    self._wake.clear()
                    continue
                self._wake.clear()
                sent = await self._process_once()
                turns += 1
                if not sent:
                    await self.app.store.mark_all_processed(self.chat_id)
                    return
                if turns >= MAX_TURNS_PER_WAKE:
                    log.warning("chat %s reached max turns per wake", self.chat_id)
                    await self.app.store.mark_all_processed(self.chat_id)
                    return
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            log.exception("chat worker failed for %s", self.chat_id)
            try:
                await self.app.store.mark_all_processed(self.chat_id)
            except Exception:
                pass
        finally:
            self._task = None
            if not cancelled and self.app._started:
                try:
                    if await self.app.store.unread_count(self.chat_id) > 0:
                        self.wake()
                except Exception:
                    pass

    async def _latest_event(self) -> MessageEvent | None:
        unread = await self.app.store.unread_messages(self.chat_id)
        rows = unread or await self.app.store.recent_messages(self.chat_id, 1)
        if not rows:
            return None
        row = rows[-1]
        chat_type = "group" if self.chat_id.startswith("group:") else "private"
        return MessageEvent(
            chat_id=self.chat_id,
            chat_type=chat_type,  # type: ignore[arg-type]
            group_id=self.chat_id.split(":", 1)[1] if chat_type == "group" else None,
            user_id=str(row.get("user_id") or ""),
            nickname=str(row.get("nickname") or ""),
            self_id=str(getattr(self.app.onebot, "self_id", "") or ""),
            message_id=str(row["message_id"]) if row.get("message_id") is not None else None,
            text=str(row.get("text") or ""),
            segments=row.get("segments") or [],
            reply_to=str(row["reply_to"]) if row.get("reply_to") is not None else None,
            is_self=bool(row.get("is_self")),
            ts=int(row.get("ts") or 0),
            raw={},
        )

    async def _process_once(self) -> bool:
        context = await self.app.context_builder.build(self.chat_id)
        if not context.messages or context.last_row_id is None:
            return False
        event = await self._latest_event()
        if event is None:
            return False

        fragments = list(self.app.extensions.prompt_fragments())
        try:
            payload = await self.app.extensions.invoke_hook(
                "prompt.build",
                {"event": event.to_hook_dict(), "fragments": fragments},
                timeout_ms=self.app.config.extensions.host.hook_timeout_ms,
            )
            if isinstance(payload, dict) and isinstance(payload.get("fragments"), list):
                fragments = [str(item) for item in payload["fragments"]]
        except Exception:
            log.exception("prompt.build hook failed")

        messages = self.app.prompt_builder.build(event, context.text, fragments)
        tool_context = ToolContext(
            chat_id=self.chat_id,
            event=event,
            store=self.app.store,
            onebot=self.app.onebot,
            sender=self.app.sender,
            config=self.app.config,
        )
        before_sends = self.app.sender.count(self.chat_id)
        try:
            result = await self.app.agent_loop.run(messages, tool_context)
        except LLMError as exc:
            log.error("LLM failed for %s: %s", self.chat_id, exc)
            await self.app.store.mark_all_processed(self.chat_id)
            return False
        except Exception:
            log.exception("agent loop failed for %s", self.chat_id)
            await self.app.store.mark_all_processed(self.chat_id)
            return False

        sent = result.sent or self.app.sender.count(self.chat_id) > before_sends
        await self.app.store.mark_processed_until(self.chat_id, context.last_row_id)
        if sent:
            self.app.trigger.record_response(self.chat_id)
        if result.usage:
            log.info(
                "chat=%s rounds=%s sent=%s prompt_tokens=%s completion_tokens=%s",
                self.chat_id,
                result.rounds,
                sent,
                result.usage.get("prompt_tokens", 0),
                result.usage.get("completion_tokens", 0),
            )
        return sent


class App:
    def __init__(
        self,
        config: Config,
        *,
        onebot: Any | None = None,
        llm: Any | None = None,
        store: Store | None = None,
        extensions: Any | None = None,
        sender: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store(config.agent.data_dir / "qqagent.db")
        self.onebot = onebot or OneBotClient(config.onebot, self.handle_event)
        self.llm = llm or LLMClient(config.llm)
        self.sender = sender or SendQueue(config, self.onebot, self.store)
        if extensions is None:
            has_extension_path = any(
                path.expanduser().exists() for path in config.extensions.paths
            )
            extensions = (
                ExtensionManager(config, self.store, self.onebot)
                if config.extensions.enabled or has_extension_path
                else NullExtensions()
            )
        self.extensions = extensions
        if hasattr(self.extensions, "bind_sender"):
            self.extensions.bind_sender(self.sender)
        self.trigger = TriggerPolicy(config, self.store)
        self.context_builder = ContextBuilder(config, self.store)
        self.prompt_builder = PromptBuilder(config)
        self.tools = ToolRegistry()
        self.agent_loop = AgentLoop(
            self.llm,
            self.tools,
            max_rounds=config.llm.max_tool_rounds,
            fallback_send_text=config.llm.fallback_send_text,
        )
        self._workers: dict[str, ChatWorker] = {}
        self._started = False
        self._maintenance_task: asyncio.Task[None] | None = None

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._started:
            return
        await self.store.init()
        await self.extensions.start()
        self._register_tools()
        for tool in self.extensions.tools():
            self.tools.register(tool)
        if isinstance(self.onebot, OneBotClient):
            await self.onebot.start()
        try:
            removed = await self.store.prune(self.config.context.retention_days)
            if removed:
                log.info("pruned %s old messages", removed)
        except Exception:
            log.exception("message pruning failed")
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="maintenance"
        )
        self._started = True
        log.info("qqagent started; data_dir=%s", self.config.agent.data_dir)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except (asyncio.CancelledError, Exception):
                pass
            self._maintenance_task = None
        for worker in list(self._workers.values()):
            if worker._task is not None:
                worker._task.cancel()
        for worker in list(self._workers.values()):
            if worker._task is not None:
                try:
                    await worker._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._workers.clear()
        await self.extensions.stop()
        if isinstance(self.onebot, OneBotClient):
            await self.onebot.stop()
        if isinstance(self.llm, LLMClient):
            await self.llm.close()
        await self.store.close()
        log.info("qqagent stopped")

    async def _maintenance_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(6 * 3600)
                try:
                    removed = await self.store.prune(self.config.context.retention_days)
                    if removed:
                        log.info("pruned %s old messages", removed)
                except Exception:
                    log.exception("message pruning failed")
        except asyncio.CancelledError:
            raise

    def _register_tools(self) -> None:
        async def send_message(context: ToolContext, args: dict[str, Any]) -> ToolResult:
            try:
                ids = await context.sender.send_text(
                    context.chat_id,
                    str(args["text"]),
                    str(args["reply_to"]) if args.get("reply_to") else None,
                )
            except SenderError as exc:
                return ToolResult(f"发送失败：{exc}", ok=False)
            return ToolResult(
                f"已发送 {len(ids)} 条消息",
                data={"sent": True, "message_ids": ids},
            )

        self.tools.register(
            Tool(
                name="send_message",
                description=SEND_MESSAGE_DESCRIPTION,
                parameters=SEND_MESSAGE_SCHEMA,
                handler=send_message,
            )
        )

    # ----------------------------------------------------------------- events
    def _chat_allowed(self, event: MessageEvent) -> bool:
        if event.chat_type == "private":
            return self.config.trigger.allow_private
        if event.group_id is None:
            return False
        if not (
            self.config.trigger.allow_all_groups
            or event.group_id in self.config.trigger.allowed_groups
        ):
            return False
        group_config = self.config.trigger.groups.get(event.group_id)
        return group_config is None or group_config.enabled

    async def handle_event(self, raw: dict[str, Any]) -> None:
        event = normalize_event(raw, getattr(self.onebot, "self_id", ""))
        if event is None:
            return
        if isinstance(event, MessageEvent):
            await self._handle_message(event)
        elif isinstance(event, NoticeEvent):
            try:
                await self.extensions.invoke_hook(
                    "notice.received",
                    event.to_hook_dict(),
                    timeout_ms=self.config.extensions.host.hook_timeout_ms,
                )
            except Exception:
                log.exception("notice.received hook failed")

    async def _handle_message(self, event: MessageEvent) -> None:
        if event.is_self:
            await self.store.upsert_chat(
                event.chat_id, event.chat_type, event.nickname or None
            )
            await self.store.add_message(event)
            return
        if not self._chat_allowed(event):
            return

        payload: dict[str, Any] | None = event.to_hook_dict()
        try:
            result = await self.extensions.invoke_hook(
                "message.received",
                payload,
                timeout_ms=self.config.extensions.host.hook_timeout_ms,
            )
        except Exception:
            log.exception("message.received hook failed")
            result = payload
        if result is None:
            return
        if isinstance(result, dict):
            event.text = str(result.get("text", event.text))
            segments = result.get("segments")
            if isinstance(segments, list):
                event.segments = segments
            reply_to = result.get("reply_to")
            event.reply_to = str(reply_to) if reply_to is not None else None

        await self.store.upsert_chat(
            event.chat_id, event.chat_type, event.nickname or None
        )
        row_id = await self.store.add_message(event)
        if row_id is None:
            return

        decision = await self.trigger.should_respond(event)
        if not decision.respond:
            try:
                override = await self.extensions.invoke_hook(
                    "trigger.should_respond",
                    {
                        "event": event.to_hook_dict(),
                        "triggered": False,
                        "reason": decision.reason,
                    },
                    timeout_ms=self.config.extensions.host.hook_timeout_ms,
                )
                if isinstance(override, dict) and override.get("triggered") is True:
                    decision = decision.__class__(True, "extension")
            except Exception:
                log.exception("trigger.should_respond hook failed")

        if not decision.respond:
            log.debug("ignored %s: %s", event.chat_id, decision.reason)
            await self.store.mark_processed_until(event.chat_id, row_id)
            return

        log.info(
            "wake chat=%s user=%s reason=%s", event.chat_id, event.user_id, decision.reason
        )
        worker = self._workers.get(event.chat_id)
        if worker is None:
            worker = ChatWorker(self, event.chat_id)
            self._workers[event.chat_id] = worker
        worker.wake()
