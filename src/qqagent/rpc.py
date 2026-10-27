"""Length-prefixed JSON-RPC 2.0 over a pair of asyncio streams.

Used between the trusted core and the extension host process. Frames are a
4-byte big-endian length followed by UTF-8 JSON. The protocol is deliberately
small and debuggable.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import struct
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

MAX_FRAME_BYTES = 8 * 1024 * 1024


class RPCError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class RPCTimeout(RPCError, TimeoutError):
    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(-32001, f"RPC timeout after {timeout:.3f}s: {method}")
        self.method = method
        self.timeout = timeout


class RPCClosed(RPCError):
    def __init__(self, message: str = "RPC connection closed") -> None:
        super().__init__(-32002, message)


def encode_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise RPCError(-32003, f"frame too large: {len(body)} bytes")
    return struct.pack(">I", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_BYTES:
        raise RPCError(-32003, f"frame too large: {length} bytes")
    body = await reader.readexactly(length)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise RPCError(-32700, f"invalid JSON frame: {exc}") from exc
    if not isinstance(payload, dict):
        raise RPCError(-32600, "frame must be a JSON object")
    return payload


RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class RPCChannel:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        request_handler: RequestHandler | None = None,
        notification_handler: NotificationHandler | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.request_handler = request_handler
        self.notification_handler = notification_handler
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ids = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._incoming_tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="rpc-reader"
            )

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> Any:
        if self._closing:
            raise RPCClosed()
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError as exc:
                raise RPCTimeout(method, timeout) from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._closing:
            return
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _send(self, payload: dict[str, Any]) -> None:
        frame = encode_frame(payload)
        async with self._write_lock:
            if self.writer.is_closing():
                raise RPCClosed()
            self.writer.write(frame)
            await self.writer.drain()

    async def _read_loop(self) -> None:
        try:
            while True:
                message = await read_frame(self.reader)
                if "method" in message:
                    await self._handle_incoming(message)
                else:
                    self._resolve_response(message)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, RPCError):
            pass
        except Exception:
            log.exception("RPC reader failed")
        finally:
            self._closed.set()
            self._fail_pending(RPCClosed())
            # When the peer closes its side, close ours too. Without this a
            # server-side handler can leave the TCP connection half-open and
            # Server.wait_closed() hangs.
            if not self.writer.is_closing():
                self.writer.close()

    async def _handle_incoming(self, message: dict[str, Any]) -> None:
        method = str(message.get("method"))
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        message_id = message.get("id")
        if message_id is None:
            if self.notification_handler is not None:
                try:
                    result = self.notification_handler(method, params)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("notification handler failed: %s", method)
            return
        task = asyncio.create_task(
            self._respond(message_id, method, params), name=f"rpc-{method}"
        )
        self._incoming_tasks.add(task)
        task.add_done_callback(self._incoming_tasks.discard)

    async def _respond(
        self, message_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        try:
            if self.request_handler is None:
                raise RPCError(-32601, f"method not found: {method}")
            result = await self.request_handler(method, params)
            await self._send({"jsonrpc": "2.0", "id": message_id, "result": result})
        except RPCError as exc:
            await self._safe_error(message_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            log.exception("RPC request handler failed: %s", method)
            await self._safe_error(message_id, -32603, f"internal error: {exc}")

    async def _safe_error(
        self, message_id: Any, code: int, message: str, data: Any = None
    ) -> None:
        try:
            error: dict[str, Any] = {"code": code, "message": message}
            if data is not None:
                error["data"] = data
            await self._send({"jsonrpc": "2.0", "id": message_id, "error": error})
        except (RPCClosed, ConnectionError):
            pass

    def _resolve_response(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if not isinstance(message_id, int):
            return
        future = self._pending.pop(message_id, None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error") or {}
            future.set_exception(
                RPCError(
                    int(error.get("code", -32603)),
                    str(error.get("message", "unknown error")),
                    error.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))

    def _fail_pending(self, error: RPCError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        for task in list(self._incoming_tasks):
            task.cancel()
        if not self.writer.is_closing():
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        self._closed.set()
        self._fail_pending(RPCClosed())
