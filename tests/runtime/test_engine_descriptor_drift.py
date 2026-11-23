"""P0b B4：drift 守卫脚本单测——散布静态值与 descriptor 派生不一致必须红。

spec 风险表第 5 条：脚本生成物进 CI 比对而非人肉维护。本单测验证：
1. 当前树全绿（无漂移）；
2. 人为制造漂移（改 _KNOWN / known 表 / probe 命令 / ENGINE_REGISTRY）→ 脚本检出。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import src.domains.runtime.engine.registry  # noqa: E402,F401

from scripts.engine_descriptor_drift import collect_drift  # noqa: E402


def test_current_tree_has_no_drift():
    """当前树必须零漂移（这是 CI 门禁的 GREEN 基线）。"""
    drifts = collect_drift()
    assert drifts == [], f"检测到 descriptor 漂移: {drifts}"


def test_known_list_drift_detected(monkeypatch):
    from src.domains.runtime.agents import executor
    original = list(executor.discover_backends.__code__.co_consts)  # 只为确认可 patch
    # 人为造漂移：把 known 表的 hermes 条目改 cli 名
    real = executor.discover_backends

    def fake_discover():
        results = real()
        for r in results:
            if r["name"] == "hermes":
                r["cli_path"] = r["cli_path"]  # noop 保持结构
        results.append({"name": "drift-engine", "installed": False, "cli_path": None,
                        "pip_package": ""})
        return results

    monkeypatch.setattr(executor, "discover_backends", fake_discover)
    drifts = collect_drift()
    assert any("discover/known" in d for d in drifts), f"known 表漂移未检出: {drifts}"


def test_probe_impl_drift_detected(monkeypatch):
    """[B5 合一后修正] 命令表已消——漂移口径改为 descriptor probe.impl
    与分派器实现表的注册一致性。"""
    from src.domains.runtime.engine import probe_dispatch
    monkeypatch.delitem(probe_dispatch._ACP_IMPLS, "hermes_acp")
    drifts = collect_drift()
    assert any("probe.impl 未注册" in d and "hermes" in d for d in drifts), \
        f"probe.impl 漂移未检出: {drifts}"


def test_registry_drift_detected(monkeypatch):
    from src.domains.runtime.agents.sandbox.backends import engine_strategies
    monkeypatch.setitem(engine_strategies.ENGINE_REGISTRY, "drift-engine", object())
    drifts = collect_drift()
    assert any("ENGINE_REGISTRY" in d for d in drifts), f"ENGINE_REGISTRY 漂移未检出: {drifts}"


def test_prefix_map_drift_detected(monkeypatch):
    from src.domains.runtime.engine import instances
    monkeypatch.setitem(instances.ENGINE_CANONICAL_PREFIXES, "drift-prefix", "hermes")
    drifts = collect_drift()
    assert any("prefix" in d for d in drifts), f"prefix map 漂移未检出: {drifts}"


def test_device_known_drift_detected(monkeypatch):
    from src.domains.mgmt.user import db as user_db
    # db.py 的 _KNOWN 是函数内局部静态白名单——直接验证静态字符串源码比对路径
    from scripts.engine_descriptor_drift import _db_known_source_check
    src = open(user_db.__file__, encoding="utf-8").read()
    drifted = src.replace('"opencode", "codearts", "hermes", "codex", "cursor", "atomcode"',
                          '"opencode", "codearts"')
    problems = _db_known_source_check(drifted, {"opencode", "codearts", "hermes", "codex",
                                               "cursor", "atomcode"})
    assert problems, "db._KNOWN 漂移未检出"
