"""Issue #3796：启动期场景分类 slug 校验接线（src/api/main.py）单测。

契约（Spec）：
- scenes/ 目录缺失（grape-skills 下发，部署根不带）→ 1 条 INFO 降级跳过，不阻断；
- 存在「字典无此 slug 且无别名」的 scene.json → 默认 1 条聚合 WARNING（scene→slug）；
- CATEGORY_SLUG_STRICT=1 → 同场景 fail-fast（SystemExit 2）；
- 仅别名可解析的旧 slug（skill-dev 等）→ 通过。
"""
import json
import logging
import os

import pytest

from src.api.main import _startup_category_slug_check

LOGGER_NAME = "devops-bot"


def _write_scene(scenes_dir: str, name: str, category: str) -> None:
    d = os.path.join(scenes_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "scene.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "category": category}, f, ensure_ascii=False)


def _records(caplog, level: int) -> list:
    return [r for r in caplog.records if r.levelno == level and "[CONFIG]" in r.getMessage()]


def test_missing_scenes_dir_degrades_with_info(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        _startup_category_slug_check(str(tmp_path / "nope"))
    infos = _records(caplog, logging.INFO)
    assert any("skipped" in r.getMessage() for r in infos)
    assert not _records(caplog, logging.WARNING)


def test_unknown_slug_warns_aggregated_by_default(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("CATEGORY_SLUG_STRICT", raising=False)
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    _write_scene(scenes, "bad-scene", "no-such-slug")
    _write_scene(scenes, "legacy-ok", "skill-dev")
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        _startup_category_slug_check(scenes)
    warns = _records(caplog, logging.WARNING)
    assert len(warns) == 1
    msg = warns[0].getMessage()
    assert "bad-scene" in msg and "no-such-slug" in msg
    assert "legacy-ok" not in msg


def test_unknown_slug_strict_mode_fails_fast(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("CATEGORY_SLUG_STRICT", "1")
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    _write_scene(scenes, "bad-scene", "no-such-slug")
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(SystemExit) as exc:
            _startup_category_slug_check(scenes)
    assert exc.value.code == 2
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_alias_resolved_slugs_pass(tmp_path, caplog):
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    for name, slug in (("a", "skill-dev"), ("b", "general"), ("c", "swarm-dev")):
        _write_scene(scenes, name, slug)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        _startup_category_slug_check(scenes)
    assert any("validated OK" in r.getMessage() for r in _records(caplog, logging.INFO))
    assert not _records(caplog, logging.WARNING)
