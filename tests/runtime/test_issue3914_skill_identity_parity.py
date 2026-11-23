# -*- coding: utf-8 -*-
"""Issue #3914：direct / worker 技能标识一致化（B 桥接 / C 取包复用 / A 派发 id / D 门禁 fallback）。

根因：本机直接执行（local_claimer）的 evt 由任务行重建，未桥接 `_skill_id` →
`_materialize_direct_workdir._skill_primary` 退化为「按名找官方目录」→ #617 官方检查器
兜底 → #887 门禁按「本地缓存残留」误拒（`[skill_mismatch]`），PR 订阅 QA 随机断链。

覆盖：
  1. `local_claimer._rebuild_evt_from_task` 桥接 `_skill_id` / `_skill_path` / `__obs_key__`；
  2. `_materialize_direct_workdir` 复用既有 OBS 取包通道（`_skill_obs_key` → get_skill_zip_bytes）；
  3. PR 订阅补派 / 重派传权威 `skill_id`（A）；
  4. direct 门禁传 `skill_fallback`（D）：明示回退 → declared_fallback 放行；真缺失 → 仍拒。
"""
import io
import json
import os
import sqlite3
import sys
import threading
import zipfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))


def _mk_skill(skill_dir: str, name: str):
    d = os.path.join(skill_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"# {name}\n")
    return d


def _skill_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}/SKILL.md", f"# {name}\n")
    return buf.getvalue()


# ── 1. B：direct evt 重建桥接 ─────────────────────────────────────

@pytest.fixture()
def evt_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("""
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY, repo_key TEXT, issue_id TEXT, event_type TEXT,
        source TEXT, actor TEXT, payload TEXT DEFAULT '{}', title TEXT DEFAULT '',
        agent_type TEXT, runtime TEXT, skill TEXT, status TEXT DEFAULT 'completed',
        created_at TEXT
    );
    """)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", conn)
    monkeypatch.setattr(sm, "db_lock", threading.RLock(), raising=False)
    yield conn
    conn.close()


def test_rebuild_evt_bridges_skill_id(evt_db, monkeypatch):
    """任务行 skill_id>0 → evt 带 `_skill_id` 与解析后的 `_skill_path`。"""
    monkeypatch.setattr("src.domains.mgmt.public.get_skill_path",
                        lambda sid: f"/srv/skills/{sid}/repo-qa/SKILL.md")
    from src.domains.runtime.engine import local_claimer as lc
    task = {"repo_key": "gitcode:o/r", "issue_id": "#7", "event_seq": 0,
            "event_type": "a2a_event", "account": "o/r",
            "skill": "repo-qa", "skill_id": 2756}
    evt = lc._rebuild_evt_from_task(task)
    assert evt["_skill_id"] == 2756
    assert evt["_skill_path"] == "/srv/skills/2756/repo-qa/SKILL.md"


def test_rebuild_evt_bridges_obs_key(evt_db):
    """存量任务行 skill_id=0：从 skill_files 的 `__obs_key__` 桥接取包键，路径回退技能名。"""
    from src.domains.runtime.engine import local_claimer as lc
    task = {"repo_key": "gitcode:o/r", "issue_id": "#7", "event_seq": 0,
            "event_type": "a2a_event", "account": "o/r",
            "skill": "repo-qa", "skill_id": 0,
            "skill_files": json.dumps({"repo-qa": {"__obs_key__": "skills/repo-qa.zip"}})}
    evt = lc._rebuild_evt_from_task(task)
    assert evt.get("_skill_id", 0) == 0
    assert evt["_skill_obs_key"] == "skills/repo-qa.zip"
    assert evt["_skill_path"] == "repo-qa"


def test_rebuild_evt_skill_files_text_payload_ignored(evt_db):
    """skill_files 为全量内联文本（无 __obs_key__）→ 不产生取包键（保持既有降级）。"""
    from src.domains.runtime.engine import local_claimer as lc
    task = {"repo_key": "gitcode:o/r", "skill_id": 0, "skill": "repo-qa",
            "skill_files": json.dumps({"repo-qa": {"SKILL.md": "# x"}})}
    evt = lc._rebuild_evt_from_task(task)
    assert not evt.get("_skill_obs_key")


def test_rebuild_evt_malformed_skill_files_safe(evt_db):
    """skill_files 非法 JSON / 非 dict → 不抛异常，无取包键。"""
    from src.domains.runtime.engine import local_claimer as lc
    task = {"repo_key": "gitcode:o/r", "skill_id": 0, "skill": "repo-qa",
            "skill_files": "not-json"}
    evt = lc._rebuild_evt_from_task(task)
    assert not evt.get("_skill_obs_key")


# ── 2. C：复用既有 OBS 取包通道 ───────────────────────────────────

def test_direct_materialize_uses_obs_key_bridge(tmp_path, monkeypatch):
    """evt 带 `_skill_obs_key`（skill_id=0）→ 走 get_skill_zip_bytes 既有通道物化成功。"""
    import src.domains.runtime.storage as st
    monkeypatch.setattr(st, "get_skill_zip_bytes", lambda key: _skill_zip("repo-qa"))
    from src.domains.runtime.pipeline.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": "repo-qa", "_skill_obs_key": "skills/repo-qa.zip"}
    _materialize_direct_workdir(str(tmp_path), "gitcode:acme/repo", "qa",
                                "repo-qa", evt, "", "prompt")
    assert os.path.isfile(str(tmp_path / ".skills" / "repo-qa" / "SKILL.md"))
    mat = evt.get("_skill_materialize") or {}
    assert mat.get("status") == "ok"
    sv = evt.get("_skill_consistency") or {}
    assert sv.get("ok") is True and sv.get("source") == "bound"


# ── 3. A：PR 订阅派发传权威 skill_id ─────────────────────────────

def _patch_pr_sub_dispatch(ep, monkeypatch, captured):
    import src.domains.runtime.engine.engine_select as es
    monkeypatch.setattr(es, "pick_engine", lambda *a, **k: 246)
    monkeypatch.setattr(es, "engine_name", lambda rid: "opencode")
    monkeypatch.setattr(es, "_repo_user_id", lambda *a, **k: 0)
    monkeypatch.setattr(ep, "_scene_pool_gate",
                        lambda *a, **k: ("pool", {"pool_engine_ids": "[246]"}, None))
    monkeypatch.setattr(ep, "resolve_scene_node_skill_id", lambda *a, **k: 2756)
    monkeypatch.setattr("src.domains.mgmt.public.get_skill_path",
                        lambda sid: "/srv/skills/repo-qa/SKILL.md")
    monkeypatch.setattr(ep, "_enqueue_pipeline_task",
                        lambda *a, **k: captured.append((a, k)))


def test_pr_subscription_extra_passes_resolved_skill_id(monkeypatch):
    import src.api.event_store as es_store
    monkeypatch.setattr(es_store, "append_event", lambda **k: 4711)
    import src.domains.runtime.pipeline.event_pipeline as ep
    captured = []
    _patch_pr_sub_dispatch(ep, monkeypatch, captured)
    evt = {"repo_key": "gitcode:o/r", "issue_id": "#0", "actor": "tester",
           "payload": {"object_attributes": {"iid": 99}}}
    ep._dispatch_pr_subscription_extra(
        "gitcode:o/r", evt, "pull_request_opened",
        ("sc3914", "repo-qa", [], "", []))
    assert captured, "未走到 _enqueue_pipeline_task"
    args, _kw = captured[0]
    # (repo_key, seq, agent_type, backend, skill_name, evt, skill_id, skill_path, runtime_id, issue_id)
    assert args[6] == 2756, f"skill_id 应传场景节点权威 id，实际 {args[6]!r}"
    assert args[7] == "/srv/skills/repo-qa/SKILL.md"


def test_pr_subscription_redispatch_passes_resolved_skill_id(monkeypatch):
    import src.api.event_store as es_store
    import src.domains.mgmt.public as mp
    monkeypatch.setattr(mp, "scene_active_nodes_any",
                        lambda repo_key, scene_id: (1, json.dumps(
                            [{"tag": "qa", "enabled": True, "skill": "repo-qa"}])))
    monkeypatch.setattr(es_store, "update_event_status", lambda *a, **k: None)
    import src.domains.runtime.pipeline.event_pipeline as ep
    captured = []
    _patch_pr_sub_dispatch(ep, monkeypatch, captured)
    evt = {"repo_key": "gitcode:o/r", "payload": {"object_attributes": {"iid": 7}}}
    ok = ep._dispatch_pr_subscription_redispatch(evt, 4711, "sc3914")
    assert ok is True
    assert captured, "未走到 _enqueue_pipeline_task"
    args, _kw = captured[0]
    assert args[6] == 2756
    assert args[7] == "/srv/skills/repo-qa/SKILL.md"


def test_pr_subscription_extra_unresolved_id_keeps_name_fallback(monkeypatch):
    """场景节点无 skill_id（老场景）→ 仍传 0 + 空路径，按名降级语义不变。"""
    import src.api.event_store as es_store
    monkeypatch.setattr(es_store, "append_event", lambda **k: 4712)
    import src.domains.runtime.pipeline.event_pipeline as ep
    captured = []
    _patch_pr_sub_dispatch(ep, monkeypatch, captured)
    monkeypatch.setattr(ep, "resolve_scene_node_skill_id", lambda *a, **k: 0)
    evt = {"repo_key": "gitcode:o/r", "issue_id": "#0", "actor": "tester",
           "payload": {"object_attributes": {"iid": 99}}}
    ep._dispatch_pr_subscription_extra(
        "gitcode:o/r", evt, "pull_request_opened",
        ("sc3914", "repo-qa", [], "", []))
    args, _kw = captured[0]
    assert args[6] == 0 and args[7] == ""


# ── 4. D：direct 门禁传 skill_fallback ────────────────────────────

def test_materialize_fallback_result_carries_skill_name(tmp_path, monkeypatch):
    """回退分支结果补 `skill_name`（declared_fallback 判定的单点来源）。"""
    import src.kernel.config as cfg
    from src.domains.runtime.engine import skill_materialize as sm
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    checker = tmp_path / "official-qa-checker"
    checker.mkdir()
    (checker / "SKILL.md").write_text("# official-qa-checker\n", encoding="utf-8")
    skills_root = str(tmp_path / ".skills")
    res = dict(sm.materialize_skill_with_fallback(
        skills_root, "repo-qa", lambda: ("none", "missing"),
        agent_type="qa",
        agent_type_fallback=lambda at: {"skill_name": "official-qa-checker",
                                        "skill_id": 9, "skill_dir": str(checker)}))
    assert res["status"] == "fallback"
    assert res["skill_name"] == "official-qa-checker"


def test_direct_verify_declares_official_fallback(tmp_path, monkeypatch):
    """明示回退（官方检查器已物化）→ 门禁 declared_fallback 放行，不误报 [skill_mismatch]。"""
    import src.kernel.config as cfg
    from src.domains.runtime.engine import skill_materialize as sm
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    checker = tmp_path / "official-qa-checker"
    checker.mkdir()
    (checker / "SKILL.md").write_text("# official-qa-checker\n", encoding="utf-8")
    monkeypatch.setattr("src.domains.mgmt.public.resolve_official_checker_skill",
                        lambda at: {"id": 9, "skill_name": "official-qa-checker"})
    monkeypatch.setattr("src.domains.mgmt.public.get_skill_path",
                        lambda sid: str(checker / "SKILL.md"))
    from src.domains.runtime.pipeline.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    _materialize_direct_workdir(str(tmp_path), "gitcode:acme/repo", "qa",
                                "repo-qa", evt, "", "prompt")
    mat = evt.get("_skill_materialize") or {}
    assert mat.get("status") == "fallback"
    sv = evt.get(sm.SKILL_CONSISTENCY_KEY) or {}
    assert sv.get("ok") is True
    assert sv.get("source") == "declared_fallback"
    assert sv.get("actual") == "official-qa-checker"


def test_direct_verify_without_skill_name_still_records_consistency(tmp_path):
    """无绑定技能（skill_name 为空）→ 门禁仍落 `_skill_consistency`（基线 unbound ok=True）。

    回归：verify 块若引用只在 `if skill_name:` 内定义的局部变量，空技能名时会抛
    NameError 被 except 吞掉 → 键缺失 → direct 门禁退化为「verdict missing」。
    """
    from src.domains.runtime.engine import skill_materialize as sm
    from src.domains.runtime.pipeline.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    _materialize_direct_workdir(str(tmp_path), "gitcode:acme/repo", "qa",
                                "", evt, "", "prompt")
    assert "_skill_materialize" not in evt  # 无绑定技能不物化
    sv = evt.get(sm.SKILL_CONSISTENCY_KEY)
    assert sv is not None, "无绑定技能时门禁结果缺失（verify 块 NameError 被吞）"
    assert sv.get("ok") is True
    assert sv.get("source") == "unbound"


def test_direct_verify_missing_skill_still_refused(tmp_path, monkeypatch):
    """反向回归：技能真缺失 + 无明示回退 → 仍 fail-fast（local_cache 拒绝）。"""
    import src.kernel.config as cfg
    from src.domains.runtime.engine import skill_materialize as sm
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    # 官方检查器回退也不存在 → 真缺失（degraded），门禁必须继续 fail-fast
    monkeypatch.setattr("src.domains.mgmt.public.resolve_official_checker_skill",
                        lambda at: None)
    _mk_skill(str(tmp_path / ".skills"), "other-skill")
    from src.domains.runtime.pipeline.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    _materialize_direct_workdir(str(tmp_path), "gitcode:acme/repo", "qa",
                                "repo-qa", evt, "", "prompt")
    mat = evt.get("_skill_materialize") or {}
    assert mat.get("status") == "degraded"
    sv = evt.get(sm.SKILL_CONSISTENCY_KEY) or {}
    assert sv.get("ok") is False
    assert sv.get("source") == "local_cache"


# ── 5. B：入队回写解析后的 skill_id 列 ────────────────────────────

def test_enqueue_task_writes_resolved_skill_id(monkeypatch):
    import src.domains.mgmt.public as mp
    monkeypatch.setattr(mp, "resolve_skill_id", lambda *a, **k: 4242)
    monkeypatch.setattr(mp, "get_skill_path", lambda sid: "")
    monkeypatch.setattr(mp, "get_skill_object_key", lambda sid: "")
    from src.domains.runtime.engine import service as svc
    from src.kernel.session_manager import db, db_lock
    tid = svc.enqueue_task(agent_session_id="sess-3914", runtime_id=1,
                           prompt="p", skill="repo-qa", skill_id=0,
                           repo_key="gitcode:o/test3914", agent_type="qa")
    assert tid
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT skill_id FROM agent_tasks WHERE id=?", (tid,))
            row = c.fetchone()
        assert row and int(row[0] or 0) == 4242, f"skill_id 列应回写 4242，实际 {row!r}"
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
            db.commit()


def test_enqueue_task_unresolvable_skill_id_stays_zero(monkeypatch):
    """技能解析不到 → 列仍 0（既有按名降级语义不变）。"""
    import src.domains.mgmt.public as mp
    monkeypatch.setattr(mp, "resolve_skill_id", lambda *a, **k: 0)
    from src.domains.runtime.engine import service as svc
    from src.kernel.session_manager import db, db_lock
    tid = svc.enqueue_task(agent_session_id="sess-3914", runtime_id=1,
                           prompt="p", skill="no-such-skill", skill_id=0,
                           repo_key="gitcode:o/test3914", agent_type="qa")
    assert tid
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT skill_id FROM agent_tasks WHERE id=?", (tid,))
            row = c.fetchone()
        assert int(row[0] or 0) == 0
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
            db.commit()