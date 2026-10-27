"""Build the L1 context window from stored messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qqagent.config import Config

from .store import Store

MAX_SINGLE_MESSAGE_CHARS = 2000
MAX_QUOTE_PREVIEW_CHARS = 80


@dataclass(slots=True)
class ContextResult:
    text: str
    messages: list[dict]
    last_row_id: int | None
    unread_count: int


class ContextBuilder:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        try:
            self.tz = ZoneInfo(config.agent.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            self.tz = ZoneInfo("UTC")

    def _clock(self, ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, tz=self.tz).strftime("%H:%M")

    async def _quote_preview(self, chat_id: str, reply_to: str) -> str:
        try:
            quoted = await self.store.get_message(chat_id, reply_to)
        except Exception:
            return ""
        if not quoted:
            return ""
        name = "我" if quoted.get("is_self") else str(quoted.get("nickname") or "?")
        text = str(quoted.get("text") or "").replace("\n", " ")
        if len(text) > MAX_QUOTE_PREVIEW_CHARS:
            text = text[:MAX_QUOTE_PREVIEW_CHARS] + "…"
        return f" ↳ {name}: {text}"

    async def build(self, chat_id: str) -> ContextResult:
        messages = await self.store.recent_messages(
            chat_id, self.config.context.max_messages
        )
        unread = await self.store.unread_count(chat_id)
        if not messages:
            return ContextResult("", [], None, unread)

        # The newest row in the fetched window is the point through which the
        # worker may mark messages processed. Messages arriving during the LLM
        # call have larger ids and stay unread.
        last_row_id = int(messages[-1]["id"])

        lines: list[str] = []
        for message in messages:
            text = str(message.get("text") or "").strip() or "[空消息]"
            if len(text) > MAX_SINGLE_MESSAGE_CHARS:
                text = text[:MAX_SINGLE_MESSAGE_CHARS] + "…"
            name = "我" if message.get("is_self") else str(
                message.get("nickname") or message.get("user_id") or "?"
            )
            line = f"[{self._clock(int(message['ts']))}] {name}: {text}"
            if message.get("reply_to"):
                line += await self._quote_preview(chat_id, str(message["reply_to"]))
            lines.append(line)

        # Trim oldest lines until the context fits. Always keep at least the
        # newest line so a single oversized message still reaches the model.
        total = sum(len(line) + 1 for line in lines)
        while len(lines) > 1 and total > self.config.context.max_chars:
            removed = lines.pop(0)
            total -= len(removed) + 1

        return ContextResult("\n".join(lines), messages, last_row_id, unread)
