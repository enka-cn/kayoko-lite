from __future__ import annotations

import asyncio
import json

import pytest

from qqagent.rpc import (
    RPCChannel,
    RPCError,
    RPCTimeout,
    encode_frame,
    read_frame,
)


async def test_frame_roundtrip() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"文本": "你好"}}
    reader = asyncio.StreamReader()
    reader.feed_data(encode_frame(payload))
    reader.feed_eof()
    assert await read_frame(reader) == payload


async def test_rpc_request_response() -> None:
    async def server_handler(method: str, params: dict):
        if method == "echo":
            return {"echo": params}
        raise RPCError(-32601, "not found")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        channel = RPCChannel(reader, writer, request_handler=server_handler)
        channel.start()
        await channel.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = RPCChannel(reader, writer)
    client.start()
    try:
        result = await client.request("echo", {"x": 1}, timeout=1.0)
        assert result == {"echo": {"x": 1}}
        with pytest.raises(RPCError, match="not found"):
            await client.request("missing", {}, timeout=1.0)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_rpc_timeout() -> None:
    async def server_handler(method: str, params: dict):
        await asyncio.sleep(1)
        return {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        channel = RPCChannel(reader, writer, request_handler=server_handler)
        channel.start()
        await channel.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = RPCChannel(reader, writer)
    client.start()
    try:
        with pytest.raises(RPCTimeout):
            await client.request("slow", {}, timeout=0.05)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_rpc_notification() -> None:
    seen: list[tuple[str, dict]] = []

    async def on_notification(method: str, params: dict) -> None:
        seen.append((method, params))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        channel = RPCChannel(
            reader, writer, notification_handler=on_notification
        )
        channel.start()
        await channel.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = RPCChannel(reader, writer)
    client.start()
    try:
        await client.notify("log", {"message": "hello"})
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == [("log", {"message": "hello"})]
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


def test_encode_rejects_huge_frame() -> None:
    # Only checks the framing helper's guard without allocating 8 MB.
    with pytest.raises(RPCError):
        encode_frame({"x": "a" * (8 * 1024 * 1024 + 1)})
