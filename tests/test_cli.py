from __future__ import annotations

import json
from pathlib import Path

from qqagent.cli import main


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
agent:
  data_dir: ./data
llm:
  api_key: test-key
trigger:
  allow_all_groups: true
extensions:
  paths: ["./extensions"]
  enabled: []
""",
        encoding="utf-8",
    )
    return path


def test_config_check(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    assert main(["config", "check", "-c", str(config_path)]) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["llm"]["api_key"].startswith("te")
    assert "test-key" not in output


def test_schema_written(tmp_path: Path, capsys) -> None:
    out = tmp_path / "schema.json"
    assert main(["schema", "-o", str(out)]) == 0
    schema = json.loads(out.read_text())
    assert schema["title"] == "qqagent configuration"
    assert "llm" in schema["properties"]


def test_extensions_list(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    assert main(["extensions", "list", "-c", str(config_path)]) == 0
    output = capsys.readouterr().out
    assert "(no extensions discovered)" in output


def test_invalid_config_returns_2(tmp_path: Path, capsys) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("llm: [not, a, mapping]\n", encoding="utf-8")
    assert main(["config", "check", "-c", str(path)]) == 2
    assert "configuration error" in capsys.readouterr().err
