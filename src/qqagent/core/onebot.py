"""Minimal OneBot v11 client: forward WebSocket events + HTTP actions."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import websockets

from qqagent.config import OneBotConfig

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

log = logging.getLogger(__name__)


class OneBotError(RuntimeError):
    def __init__(self, action: str, message: str, retcode: Any = None) -> None:
        super().__init__(f"{action}: {message} (retcode={retcode})")
        self.action = action
        self.retcode = retcode


class OneBotClient:
    def __init__(self, config: OneBotConfig, on_event: EventCallback) -> None:
        self.config = config
        self.on_event = on_event
        self.self_id = ""
        self.nickname = ""
        self.connected = False
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None
        self._http: httpx.AsyncClient | None = None
        self._connected_event = asyncio.Event()

    # ------------------------------------------------------------------ HTTP
    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.config.request_timeout,
                    connect=self.config.connect_timeout,
                ),
                headers=self._http_headers(),
            )
        return self._http

    def _http_headers(self) -> dict[str, str]:
        token = self.config.http_token.get_secret_value()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def call(
        self, action: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        """Call a OneBot HTTP action and return its ``data`` payload."""
        client = self._http_client()
        payload = params or {}
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await client.post(
                    f"{self.config.http_url.rstrip('/')}/{action}",
                    json=payload,
                    timeout=timeout or self.config.request_timeout,
                )
                if response.status_code >= 500 and attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                if response.status_code >= 400:
                    raise OneBotError(
                        action,
                        f"HTTP {response.status_code}: {response.text[:300]}",
                    )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise OneBotError(action, f"invalid JSON response: {response.text[:300]}") from exc
                if not isinstance(body, dict):
                    raise OneBotError(action, "response is not a JSON object")
                retcode = body.get("retcode")
                if body.get("status") == "failed" or (
                    retcode is not None and int(retcode) != 0
                ):
                    raise OneBotError(
                        action,
                        str(body.get("message") or body.get("wording") or "failed"),
                        retcode,
                    )
                return body.get("data")
            except (httpx.HTTPError, OneBotError) as exc:
                last_error = exc
                if attempt == 0 and not isinstance(exc, OneBotError):
                    await asyncio.sleep(0.3)
                    continue
                break
        if isinstance(last_error, OneBotError):
            raise last_error
        raise OneBotError(action, str(last_error or "request failed"))

    async def get_login_info(self) -> dict[str, Any]:
        data = await self.call("get_login_info")
        if isinstance(data, dict):
            self.self_id = str(data.get("user_id") or self.self_id)
            self.nickname = str(data.get("nickname") or self.nickname)
        return data or {}

    async def send_group_msg(
        self, group_id: str | int, message: list[dict[str, Any]], timeout: float | None = None
    ) -> dict[str, Any]:
        data = await self.call(
            "send_group_msg",
            {"group_id": int(group_id), "message": message},
            timeout=timeout or self.config.send_timeout,
        )
        return data if isinstance(data, dict) else {}

    async def send_private_msg(
        self, user_id: str | int, message: list[dict[str, Any]], timeout: float | None = None
    ) -> dict[str, Any]:
        data = await self.call(
            "send_private_msg",
            {"user_id": int(user_id), "message": message},
            timeout=timeout or self.config.send_timeout,
        )
        return data if isinstance(data, dict) else {}

    async def get_group_info(self, group_id: str | int) -> dict[str, Any]:
        data = await self.call("get_group_info", {"group_id": int(group_id)})
        return data if isinstance(data, dict) else {}

    async def get_group_member_info(
        self, group_id: str | int, user_id: str | int
    ) -> dict[str, Any]:
        data = await self.call(
            "get_group_member_info",
            {"group_id": int(group_id), "user_id": int(user_id)},
        )
        return data if isinstance(data, dict) else {}

    async def get_group_list(self) -> list[dict[str, Any]]:
        data = await self.call("get_group_list")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------- WS
    @property
    def ws_headers(self) -> dict[str, str]:
        token = self.config.ws_token.get_secret_value()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="onebot-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self.connected = False

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def _run(self) -> None:
        backoff = self.config.reconnect_min_seconds
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.config.ws_url,
                    additional_headers=self.ws_headers,
                    open_timeout=self.config.connect_timeout,
                    max_size=16 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    self._connected_event.set()
                    backoff = self.config.reconnect_min_seconds
                    log.info("OneBot WebSocket connected: %s", self.config.ws_url)
                    try:
                        await self.get_login_info()
                    except Exception as exc:
                        log.warning("get_login_info failed after connect: %s", exc)
                    async for message in ws:
                        await self._handle_ws_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("OneBot WebSocket disconnected: %s", exc)
            finally:
                self.connected = False
                self._connected_event.clear()
                self._ws = None

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(self.config.reconnect_max_seconds, backoff * 2)

    async def _handle_ws_message(self, message: str | bytes) -> None:
        try:
            raw = json.loads(message)
        except (TypeError, ValueError):
            log.warning("ignoring non-JSON OneBot frame")
            return
        if not isinstance(raw, dict):
            return
        if not self.self_id and raw.get("self_id") is not None:
            self.self_id = str(raw["self_id"])
        try:
            result = self.on_event(raw)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("event handler failed")
