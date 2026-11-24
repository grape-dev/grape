"""scene_skill_ref 绑定自愈（PR #2087 整改）：定义未变的场景也要修掉存量 skill_id=0。

背景：`sync_official_scenes` 在 content_hash 未变时 `action == "skipped"` 直接跳过重建
绑定，于是首次同步写入的 `skill_id=0` 会永久留存（全新实例首轮技能库尚未就绪时必现），
表现为场景市场技能全被标「已删除」、Apply 报「引用了已删除的技能」。

本用例锁定两条契约：
1. 存在未解析引用（skill_id<=0）→ 下一轮同步触发重建；全部已解析 → 不写库。
2. A11（架构守护）：runtime 侧不得直读 mgmt 归属表 `scene_skill_ref`，
   判定一律经 mgmt 原语 `count_unresolved_scene_skill_ref`。

跑法：.venv/bin/python -m pytest -q tests/runtime/test_scene_skill_ref_selfheal.py
"""
import pathlib
import re

import src.domains.mgmt.public as mgmt_public
from src.domains.runtime.storage import scene_sync

NODES = [{"skill": "vod", "skill_id": 0}, {"skill": "implementation"}]


def _stub_mgmt(monkeypatch, unresolved: int):
    """单库/测试拓扑：mgmt lib 句柄为 None，回落 mgmt public 原语。"""
    monkeypatch.setattr("src.kernel.domain_read.mgmt_lib", lambda *a, **k: None)
    monkeypatch.setattr(mgmt_public, "count_unresolved_scene_skill_ref", lambda sid: unresolved)


def test_selfheal_rebuilds_when_refs_unresolved(monkeypatch):
    _stub_mgmt(monkeypatch, 2)
    seen = {}
    monkeypatch.setattr(
        scene_sync, "_rebuild_scene_skill_ref",
        lambda market_id, nodes, name="", repo_key="": seen.update(market_id=market_id, name=name))
    scene_sync._repair_scene_skill_ref_if_unresolved("demo", 42, NODES)
    assert seen.get("market_id") == 42 and seen.get("name") == "demo"


def test_selfheal_skips_when_all_resolved(monkeypatch):
    _stub_mgmt(monkeypatch, 0)
    called = []
    monkeypatch.setattr(scene_sync, "_rebuild_scene_skill_ref", lambda *a, **k: called.append(a))
    scene_sync._repair_scene_skill_ref_if_unresolved("demo", 42, NODES)
    assert called == [], "没有未解析引用时不应写库"


def test_selfheal_skips_without_market_id(monkeypatch):
    _stub_mgmt(monkeypatch, 3)
    called = []
    monkeypatch.setattr(scene_sync, "_rebuild_scene_skill_ref", lambda *a, **k: called.append(a))
    scene_sync._repair_scene_skill_ref_if_unresolved("demo", 0, NODES)
    assert called == [], "market_id 未知时无法定位归属行，不应重建"


def test_declared_skill_nodes_decision():
    """O-1：区分「模板本来没有技能」与「声明了但解析失败」两种空集。"""
    assert scene_sync._declares_skill_nodes([{"skill": "vod"}]) is True
    assert scene_sync._declares_skill_nodes([{"skill": ""}, {"skill": "  "}]) is False
    assert scene_sync._declares_skill_nodes([]) is False
    assert scene_sync._declares_skill_nodes(None) is False


def test_runtime_domain_never_queries_foreign_scene_skill_ref():
    """A11 回归：runtime 域内不得出现对 mgmt 归属表 scene_skill_ref 的 SQL。"""
    root = pathlib.Path(scene_sync.__file__).resolve().parents[1]  # src/domains/runtime
    pat = re.compile(r"\b(FROM|INTO|UPDATE)\s+scene_skill_ref\b", re.I)
    offenders = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                       if pat.search(p.read_text(encoding="utf-8")))
    assert offenders == [], f"runtime 域出现 scene_skill_ref 直读: {offenders}"