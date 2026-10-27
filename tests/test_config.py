from __future__ import annotations

from pathlib import Path

import pytest

from qqagent.config import ConfigError, config_schema, load_config


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_minimal_config_with_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
agent:
  data_dir: ./data
llm:
  api_key: test-key
""",
    )
    config = load_config(path)
    assert config.agent.data_dir == tmp_path / "data"
    assert config.llm.api_key.get_secret_value() == "test-key"
    assert config.trigger.default == "mention"
    assert config.extensions.paths == [tmp_path / "extensions"]


def test_env_substitution_and_masking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQAGENT_TEST_KEY", "super-secret-key")
    path = write_config(
        tmp_path,
        """
llm:
  api_key: ${QQAGENT_TEST_KEY}
onebot:
  ws_token: ${MISSING_TOKEN:-fallback}
""",
    )
    config = load_config(path)
    assert config.llm.api_key.get_secret_value() == "super-secret-key"
    assert config.onebot.ws_token.get_secret_value() == "fallback"
    masked = config.masked_dict()
    assert "super-secret-key" not in str(masked)
    assert masked["llm"]["api_key"].startswith("su")


def test_missing_env_var_fails(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
llm:
  api_key: ${DEFINITELY_MISSING_QQAGENT_KEY}
""",
    )
    with pytest.raises(ConfigError, match="DEFINITELY_MISSING_QQAGENT_KEY"):
        load_config(path)


def test_invalid_group_trigger_mode(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
llm:
  api_key: x
trigger:
  default: sometimes
""",
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_schema_is_generated() -> None:
    schema = config_schema()
    assert schema["title"] == "qqagent configuration"
    assert "properties" in schema
    assert "llm" in schema["properties"]
