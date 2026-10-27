"""OneBot v11 event normalization.

The rest of the core works with these small typed objects instead of raw
OneBot dictionaries. Extensions receive JSON-safe dictionaries via hooks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

Segment = dict[str, Any]

TEXT_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[转发消息]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "poke": "[拍一拍]",
}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _segments(raw: Any) -> list[Segment]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str):
        # SnowLuma is configured to send arrays. A string fallback keeps the
        # agent usable with endpoints that still emit CQ-code strings.
        return [{"type": "text", "data": {"text": raw}}]
    return []


def segments_to_text(segments: list[Segment]) -> str:
    parts: list[str] = []
    for segment in segments:
        kind = str(segment.get("type", "unknown"))
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            name = data.get("name") or data.get("qq") or ""
            parts.append(f"@{name}")
        elif kind == "reply":
            continue
        else:
            parts.append(TEXT_PLACEHOLDERS.get(kind, f"[{kind}]"))
    return "".join(parts).strip()


def extract_reply_to(segments: list[Segment]) -> str | None:
    for segment in segments:
        if segment.get("type") == "reply":
            data = segment.get("data") or {}
            value = data.get("id")
            if value is not None:
                return str(value)
    return None


def is_at_user(segments: list[Segment], user_id: str) -> bool:
    target = str(user_id)
    for segment in segments:
        if segment.get("type") != "at":
            continue
        data = segment.get("data") or {}
        if str(data.get("qq", "")) == target:
            return True
    return False


def is_at_all(segments: list[Segment]) -> bool:
    for segment in segments:
        if segment.get("type") != "at":
            continue
        data = segment.get("data") or {}
        if str(data.get("qq", "")) == "all":
            return True
    return False


@dataclass(slots=True)
class MessageEvent:
    chat_id: str
    chat_type: Literal["group", "private"]
    group_id: str | None
    user_id: str
    nickname: str
    self_id: str
    message_id: str | None
    text: str
    segments: list[Segment]
    reply_to: str | None
    is_self: bool
    ts: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_hook_dict(self) -> dict[str, Any]:
        return {
            "kind": "message",
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "nickname": self.nickname,
            "self_id": self.self_id,
            "message_id": self.message_id,
            "text": self.text,
            "segments": self.segments,
            "reply_to": self.reply_to,
            "is_self": self.is_self,
            "ts": self.ts,
        }


@dataclass(slots=True)
class NoticeEvent:
    notice_type: str
    sub_type: str | None
    chat_id: str | None
    chat_type: Literal["group", "private"] | None
    group_id: str | None
    user_id: str | None
    target_id: str | None
    operator_id: str | None
    message_id: str | None
    duration: int | None
    self_id: str
    ts: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_hook_dict(self) -> dict[str, Any]:
        return {
            "kind": "notice",
            "notice_type": self.notice_type,
            "sub_type": self.sub_type,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "target_id": self.target_id,
            "operator_id": self.operator_id,
            "message_id": self.message_id,
            "duration": self.duration,
            "self_id": self.self_id,
            "ts": self.ts,
        }


Event = MessageEvent | NoticeEvent


def normalize_event(raw: dict[str, Any], self_id: str = "") -> Event | None:
    """Convert a raw OneBot v11 event into a typed event.

    Unknown post types (heartbeat, lifecycle) are ignored.
    """
    post_type = str(raw.get("post_type", ""))
    event_self = str(raw.get("self_id") or self_id or "")
    ts = _as_int(raw.get("time"), int(time.time())) * 1000

    if post_type in {"message", "message_sent"}:
        message_type = str(raw.get("message_type", ""))
        if message_type not in {"group", "private"}:
            return None
        segments = _segments(raw.get("message"))
        sender = raw.get("sender") or {}
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        if user_id is None:
            return None
        chat_id = (
            f"group:{group_id}" if message_type == "group" else f"private:{user_id}"
        )
        nickname = str(
            sender.get("card")
            or sender.get("nickname")
            or raw.get("sender", {}).get("user_id")
            or user_id
        )
        message_id = raw.get("message_id")
        return MessageEvent(
            chat_id=chat_id,
            chat_type=message_type,  # type: ignore[arg-type]
            group_id=str(group_id) if group_id is not None else None,
            user_id=str(user_id),
            nickname=nickname,
            self_id=event_self,
            message_id=str(message_id) if message_id is not None else None,
            text=segments_to_text(segments),
            segments=segments,
            reply_to=extract_reply_to(segments),
            is_self=post_type == "message_sent" or str(user_id) == event_self,
            ts=ts,
            raw=raw,
        )

    if post_type == "notice":
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        if group_id is not None:
            chat_id: str | None = f"group:{group_id}"
            chat_type: Literal["group", "private"] | None = "group"
        elif user_id is not None:
            chat_id = f"private:{user_id}"
            chat_type = "private"
        else:
            chat_id = None
            chat_type = None
        return NoticeEvent(
            notice_type=str(raw.get("notice_type", "")),
            sub_type=str(raw["sub_type"]) if raw.get("sub_type") is not None else None,
            chat_id=chat_id,
            chat_type=chat_type,
            group_id=str(group_id) if group_id is not None else None,
            user_id=str(user_id) if user_id is not None else None,
            target_id=str(raw["target_id"]) if raw.get("target_id") is not None else None,
            operator_id=(
                str(raw["operator_id"]) if raw.get("operator_id") is not None else None
            ),
            message_id=(
                str(raw["message_id"]) if raw.get("message_id") is not None else None
            ),
            duration=_as_int(raw["duration"]) if raw.get("duration") is not None else None,
            self_id=event_self,
            ts=ts,
            raw=raw,
        )

    return None
