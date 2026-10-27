"""Decide whether an incoming message should wake the model."""

from __future__ import annotations

import time
from dataclasses import dataclass

from qqagent.config import Config

from .events import MessageEvent, is_at_user
from .store import Store


@dataclass(slots=True)
class TriggerDecision:
    respond: bool
    reason: str


class TriggerPolicy:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self._last_response: dict[str, float] = {}

    def record_response(self, chat_id: str) -> None:
        self._last_response[chat_id] = time.monotonic()

    def _group_allowed(self, group_id: str | None) -> bool:
        if group_id is None:
            return False
        if self.config.trigger.allow_all_groups:
            return True
        return group_id in self.config.trigger.allowed_groups

    def _group_config(self, group_id: str | None):
        if group_id is None:
            return None
        return self.config.trigger.groups.get(group_id)

    async def should_respond(self, event: MessageEvent) -> TriggerDecision:
        if event.is_self:
            return TriggerDecision(False, "self")

        if event.chat_type == "group":
            if not self._group_allowed(event.group_id):
                return TriggerDecision(False, "group_not_allowed")
            group_config = self._group_config(event.group_id)
            if group_config is not None and not group_config.enabled:
                return TriggerDecision(False, "group_disabled")
            mode = (
                group_config.mode
                if group_config is not None and group_config.mode is not None
                else self.config.trigger.default
            )
            keywords = (
                group_config.keywords
                if group_config is not None and group_config.keywords
                else self.config.trigger.keywords
            )
        else:
            if not self.config.trigger.allow_private:
                return TriggerDecision(False, "private_not_allowed")
            # Mentions do not exist in private chats; a permitted private chat
            # is always allowed to wake the model.
            mode = "always"
            keywords = self.config.trigger.keywords

        cooldown = self.config.trigger.cooldown_seconds
        if cooldown > 0:
            last = self._last_response.get(event.chat_id)
            if last is not None and time.monotonic() - last < cooldown:
                return TriggerDecision(False, "cooldown")

        if mode == "always":
            return TriggerDecision(True, "always")

        mentioned = is_at_user(event.segments, event.self_id)
        if mentioned:
            return TriggerDecision(True, "mention")

        if event.reply_to:
            try:
                if await self.store.is_self_message(event.chat_id, event.reply_to):
                    return TriggerDecision(True, "reply_to_self")
            except Exception:
                pass

        if mode == "keyword":
            text = event.text.casefold()
            for keyword in keywords:
                keyword = str(keyword).strip()
                if keyword and keyword.casefold() in text:
                    return TriggerDecision(True, f"keyword:{keyword}")

        return TriggerDecision(False, "no_trigger")
