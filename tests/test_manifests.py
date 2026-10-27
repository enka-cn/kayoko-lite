from __future__ import annotations

from pathlib import Path

import pytest

from qqagent.core.extensions import ExtensionError, discover_manifests


def write_ext(root: Path, name: str, ext_id: str, *, enabled: bool = True) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "extension.toml").write_text(
        f"""
[extension]
id = "{ext_id}"
version = "0.1.0"
api_version = 1
kind = "plugin"
enabled = {"true" if enabled else "false"}
""",
        encoding="utf-8",
    )
    (path / "__init__.py").write_text("def setup(api):\n    pass\n", encoding="utf-8")
    return path


def test_default_filters_disabled(tmp_path: Path) -> None:
    write_ext(tmp_path, "a", "a", enabled=True)
    write_ext(tmp_path, "b", "b", enabled=False)
    manifests = discover_manifests([tmp_path], [])
    assert [item.id for item in manifests] == ["a"]


def test_explicit_enabled_list_includes_disabled(tmp_path: Path) -> None:
    write_ext(tmp_path, "a", "a", enabled=True)
    write_ext(tmp_path, "b", "b", enabled=False)
    manifests = discover_manifests([tmp_path], ["b"])
    assert [item.id for item in manifests] == ["b"]


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    write_ext(tmp_path, "a", "same")
    write_ext(tmp_path, "b", "same")
    with pytest.raises(ExtensionError, match="duplicate extension id"):
        discover_manifests([tmp_path], [])


def test_invalid_id_rejected(tmp_path: Path) -> None:
    write_ext(tmp_path, "a", "bad id")
    with pytest.raises(ExtensionError, match="invalid extension id"):
        discover_manifests([tmp_path], [])
