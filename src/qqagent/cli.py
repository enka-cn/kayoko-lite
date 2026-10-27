"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from .config import ConfigError, config_schema, load_config
from .core.app import App
from .core.extensions import ExtensionError, discover_manifests
from .logging import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqagent",
        description="Minimal, extension-first QQ group agent for OneBot v11",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start the agent")
    run.add_argument("-c", "--config", default="config.yaml", help="YAML config path")

    config = sub.add_parser("config", help="configuration utilities")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="validate and print the resolved config")
    check.add_argument("-c", "--config", default="config.yaml")
    check.add_argument("--json", action="store_true", help="print JSON (default)")

    schema = sub.add_parser("schema", help="write the JSON Schema for config.yaml")
    schema.add_argument("-o", "--out", default="config.schema.json")

    extensions = sub.add_parser("extensions", help="extension utilities")
    ext_sub = extensions.add_subparsers(dest="ext_command", required=True)
    listing = ext_sub.add_parser("list", help="list discovered extensions")
    listing.add_argument("-c", "--config", default="config.yaml")

    return parser


def _run(config_path: str) -> int:
    config = load_config(config_path)
    setup_logging(config.agent.log_level, config.agent.log_format)

    async def runner() -> None:
        app = App(config)
        await app.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        try:
            await stop.wait()
        finally:
            await app.stop()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        return 130
    return 0


def _config_check(config_path: str) -> int:
    config = load_config(config_path)
    print(json.dumps(config.masked_dict(), ensure_ascii=False, indent=2))
    return 0


def _schema(out: str) -> int:
    path = Path(out)
    path.write_text(
        json.dumps(config_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")
    return 0


def _extensions_list(config_path: str) -> int:
    config = load_config(config_path)
    manifests = discover_manifests(
        config.extensions.paths, config.extensions.enabled
    )
    if not manifests:
        print("(no extensions discovered)")
        return 0
    for manifest in manifests:
        print(
            f"{manifest.id:24} {manifest.version:10} {manifest.kind:7} "
            f"{manifest.path}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args.config)
        if args.command == "config":
            return _config_check(args.config)
        if args.command == "schema":
            return _schema(args.out)
        if args.command == "extensions":
            return _extensions_list(args.config)
    except (ConfigError, ExtensionError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"file not found: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
