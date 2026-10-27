"""Outgoing message queue: serialization, rate limits, splitting, dedupe."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any

from qqagent.config import Config

from .store import Store

log = logging.getLogger(__name__)

SENTENCE_END = re.compile(r"(?<=[。！？!?；;])")


class SenderError(RuntimeError):
    pass


class RateLimitError(SenderError):
    pass


class DuplicateMessageError(SenderError):
    pass


def split_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks no longer than ``max_chars``.

    Splits prefer newlines, then sentence punctuation, then a hard cut. This is
    intentionally simple: QQ does not render Markdown, and over-engineering
    the splitter mostly produces worse-looking messages.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            paragraphs.append(paragraph)
            continue
        for sentence in SENTENCE_END.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                paragraphs.append(sentence)
            else:
                for start in range(0, len(sentence), max_chars):
                    paragraphs.append(sentence[start : start + max_chars])

    chunks: list[str] = []
    current = ""
    for piece in paragraphs:
        candidate = f"{current}\n{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)
    return chunks


class SendQueue:
    def __init__(self, config: Config, onebot: Any, store: Store) -> None:
        self.config = config
        self.onebot = onebot
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}
        self._minute: dict[str, list[float]] = {}
        self._hour: dict[str, list[float]] = {}
        self._recent: dict[str, float] = {}
        self._sent_counts: dict[str, int] = {}

    def count(self, chat_id: str) -> int:
        """Number of successful sends for a chat (used by the worker to detect
        sends made through extension capabilities)."""
        return self._sent_counts.get(chat_id, 0)

    def _lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    @staticmethod
    def _parse_chat_id(chat_id: str) -> tuple[str, str]:
        kind, _, ident = chat_id.partition(":")
        if kind not in {"group", "private"} or not ident:
            raise SenderError(f"invalid chat_id: {chat_id!r}")
        return kind, ident

    def _check_rate(self, chat_id: str, add: int) -> None:
        now = time.monotonic()
        minute = [item for item in self._minute.get(chat_id, []) if now - item < 60]
        hour = [item for item in self._hour.get(chat_id, []) if now - item < 3600]
        if len(minute) + add > self.config.limits.send_per_minute:
            raise RateLimitError(
                f"发送频率超限：每分钟最多 {self.config.limits.send_per_minute} 条"
            )
        if len(hour) + add > self.config.limits.send_per_hour:
            raise RateLimitError(
                f"发送频率超限：每小时最多 {self.config.limits.send_per_hour} 条"
            )
        self._minute[chat_id] = minute
        self._hour[chat_id] = hour

    def _record_sent(self, chat_id: str) -> None:
        now = time.monotonic()
        self._minute.setdefault(chat_id, []).append(now)
        self._hour.setdefault(chat_id, []).append(now)

    def _is_duplicate(self, chat_id: str, text: str) -> bool:
        window = self.config.limits.dedupe_window_ms
        if window <= 0:
            return False
        key = f"{chat_id}\0{text}"
        last = self._recent.get(key)
        return bool(last is not None and (time.monotonic() - last) * 1000 < window)

    def _mark_sent(self, chat_id: str, text: str) -> None:
        window = self.config.limits.dedupe_window_ms
        if window <= 0:
            return
        self._recent[f"{chat_id}\0{text}"] = time.monotonic()
        if len(self._recent) > 500:
            cutoff = time.monotonic() - window / 1000
            self._recent = {
                key: ts for key, ts in self._recent.items() if ts >= cutoff
            }

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> list[str]:
        kind, ident = self._parse_chat_id(chat_id)
        text = str(text).strip()
        if not text:
            raise SenderError("消息正文为空")
        chunks = split_text(text, self.config.limits.max_message_chars)
        if not chunks:
            raise SenderError("消息正文为空")

        async with self._lock(chat_id):
            if self._is_duplicate(chat_id, text):
                raise DuplicateMessageError("这条消息刚刚发过，已跳过重复发送")
            self._check_rate(chat_id, len(chunks))

            delay_ms = random.randint(
                self.config.limits.min_delay_ms, self.config.limits.max_delay_ms
            )
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)

            valid_reply = None
            if reply_to:
                try:
                    if await self.store.get_message(chat_id, str(reply_to)):
                        valid_reply = str(reply_to)
                except Exception:
                    valid_reply = None

            message_ids: list[str] = []
            for index, chunk in enumerate(chunks):
                segments: list[dict[str, Any]] = []
                if index == 0 and valid_reply:
                    segments.append({"type": "reply", "data": {"id": valid_reply}})
                segments.append({"type": "text", "data": {"text": chunk}})
                if kind == "group":
                    data = await self.onebot.send_group_msg(ident, segments)
                else:
                    data = await self.onebot.send_private_msg(ident, segments)
                message_id = str(data.get("message_id")) if data.get("message_id") is not None else None
                if message_id:
                    message_ids.append(message_id)
                await self.store.add_self_message(
                    chat_id,
                    chunk,
                    message_id,
                    segments=segments,
                )
                self._record_sent(chat_id)
                self._sent_counts[chat_id] = self._sent_counts.get(chat_id, 0) + 1
                if index < len(chunks) - 1 and self.config.limits.max_delay_ms > 0:
                    await asyncio.sleep(0.2)

            self._mark_sent(chat_id, text)
            return message_ids
