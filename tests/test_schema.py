from __future__ import annotations

import json
from pathlib import Path

from qqagent.config import config_schema

ROOT = Path(__file__).resolve().parent.parent


def test_committed_schema_is_current() -> None:
    committed = json.loads((ROOT / "config.schema.json").read_text(encoding="utf-8"))
    assert committed == config_schema(), (
        "config.schema.json is stale; run: qqagent schema -o config.schema.json"
    )
