"""Extension host entrypoint.

stdout is reserved for RPC frames; extension ``print`` calls are redirected to
stderr so they cannot corrupt the protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from qqagent.rpc import RPCChannel

from .runtime import HostRuntime


async def run() -> None:
    if sys.platform == "win32":
        raise RuntimeError(
            "the extension host currently requires POSIX stdio pipes; "
            "run qqagent on Linux for M1"
        )

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s host: %(message)s",
        datefmt="%H:%M:%S",
    )

    rpc_in = sys.stdin.buffer
    rpc_out = sys.stdout.buffer
    # From here on, extension output must go to stderr.
    sys.stdout = sys.stderr
    devnull = open(os.devnull, "r", encoding="utf-8")
    sys.stdin = devnull

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    read_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: read_protocol, rpc_in)

    write_transport, write_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, rpc_out
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, reader, loop)

    runtime = HostRuntime()
    channel = RPCChannel(
        reader,
        writer,
        request_handler=runtime.handle_request,
    )
    runtime.channel = channel
    channel.start()

    def request_shutdown() -> None:
        runtime.shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            pass

    closed_task = asyncio.create_task(channel.wait_closed(), name="host-closed")
    shutdown_task = asyncio.create_task(
        runtime.shutdown_event.wait(), name="host-shutdown"
    )
    done, pending = await asyncio.wait(
        {closed_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await channel.close()
    devnull.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
