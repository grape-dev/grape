# -*- coding: utf-8 -*-
"""Issue #887：技能随 agent 绑定下发——执行前一致性校验 / 来源标注 / 缓存兜底测试。

回归用例（events seq=46108）：绑定 skill=caserun-prep 落到本地缓存持有
casegen-prep 的工位 → 拒绝执行并显式报错（[skill_mismatch]），不再静默跑错流程。

覆盖：
  1. verify_skill_execution_consistency：绑定物化 / 绑定为空 / 声明回退(#617) /
     本地缓存残留拒绝 / FALLBACK.md 恢复路径；
  2. health.check_skill_consistency：一致性校验项口径（mismatch/declared/unbound）；
  3. task_log_stream.build_skill_source_message + _to_payload：技能来源标注字段；
  4. agent._skill_consistency_gate：worker 执行前 fail-fast（拒绝 + 日志上报）；
  5. event_pipeline._materialize_direct_workdir：direct 端 evt._skill_consistency；
  6. service.enqueue_task：payload 携带绑定 skill（env GRAPE_BOUND_SKILL）。
"""
import asyncio
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.runtime import skill_materialize as sm
from src.runtime.task_log_stream import build_skill_source_message, _to_payload


def _mk_skill(skill_dir: str, name: str, with_md=True, fallback=False):
    d = os.path.join(skill_dir, name)
    os.makedirs(d, exist_ok=True)
    if with_md:
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(f"# {name}\n")
    if fallback:
        with open(os.path.join(d, "FALLBACK.md"), "w", encoding="utf-8") as f:
            f.write("fallback notice\n")
    return d


# ── 1. verify_skill_execution_consistency ─────────────────────────

def test_verify_bound_materialized_ok(tmp_path):
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "caserun-prep")
    v = sm.verify_skill_execution_consistency("caserun-prep", skill_dir)
    assert v["ok"] is True
    assert v["actual"] == "caserun-prep"
    assert v["source"] == "bound"


def test_verify_unbound_local_cache_fallback(tmp_path):
    # 绑定为空 → 引擎本地缓存仅此时兜底（放行 + 标注）
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "casegen-prep")
    v = sm.verify_skill_execution_consistency("", skill_dir)
    assert v["ok"] is True
    assert v["source"] == "unbound"
    assert v["bound"] == ""


def test_verify_declared_fallback_ok(tmp_path):
    # 服务端明示回退（#617 skill_fallback）→ 放行但 mismatch 显式标注
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "grape-skill-validator")
    v = sm.verify_skill_execution_consistency(
        "caserun-prep", skill_dir, {"skill_name": "grape-skill-validator"})
    assert v["ok"] is True
    assert v["source"] == "declared_fallback"
    assert v["actual"] == "grape-skill-validator"
    assert "declared" in v["reason"]


def test_verify_local_cache_leftover_refused(tmp_path):
    # seq=46108 回归：绑定 caserun-prep 未物化 + 本地缓存持有 casegen-prep → 拒绝
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "casegen-prep")
    v = sm.verify_skill_execution_consistency("caserun-prep", skill_dir)
    assert v["ok"] is False
    assert v["actual"] == "casegen-prep"
    assert v["source"] == "local_cache"
    assert "casegen-prep" in v["reason"]
    assert "refusing execution" in v["reason"]


def test_verify_fallback_md_recovery_ok(tmp_path):
    # 绑定未物化但仅剩 FALLBACK.md（#607 权威取回）且无其它本地技能 → 放行
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "caserun-prep", with_md=False, fallback=True)
    v = sm.verify_skill_execution_consistency("caserun-prep", skill_dir)
    assert v["ok"] is True
    assert v["source"] == "degraded_recovery"
    assert v["actual"] == "caserun-prep"


def test_verify_missing_and_empty_cache_ok(tmp_path):
    # 绑定未物化且本地缓存为空（无任何可跑偏的技能）→ 放行（按权威取回指引执行绑定值）
    skill_dir = str(tmp_path / ".skills")
    os.makedirs(skill_dir, exist_ok=True)
    v = sm.verify_skill_execution_consistency("caserun-prep", skill_dir)
    assert v["ok"] is True
    assert v["source"] == "missing"


def test_verify_tmp_dirs_not_counted_as_leftover(tmp_path):
    # #564 临时目录（.tmp-*）不算本地缓存技能，不触发拒绝
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "caserun-prep", with_md=False, fallback=True)
    _mk_skill(os.path.join(skill_dir, ".tmp-x"), "inner")
    v = sm.verify_skill_execution_consistency("caserun-prep", skill_dir)
    assert v["ok"] is True


# ── 2. health.check_skill_consistency（健康检查项） ────────────────

def test_health_consistency_match():
    from src.runtime.health import check_skill_consistency
    r = check_skill_consistency("caserun-prep", "caserun-prep")
    assert r["ok"] is True and r["mismatch"] is False
    assert r["name"] == "skill_consistency"


def test_health_consistency_mismatch_rejected():
    from src.runtime.health import check_skill_consistency
    r = check_skill_consistency("caserun-prep", "casegen-prep")
    assert r["ok"] is False and r["mismatch"] is True
    assert "refusing execution" in r["detail"]


def test_health_consistency_declared_fallback_ok():
    from src.runtime.health import check_skill_consistency
    r = check_skill_consistency("caserun-prep", "checker-x", declared=True)
    assert r["ok"] is True and r["mismatch"] is True
    assert "declared fallback" in r["detail"]


def test_health_consistency_unbound_ok():
    from src.runtime.health import check_skill_consistency
    r = check_skill_consistency("", "casegen-prep")
    assert r["ok"] is True
    assert "local cache" in r["detail"]


# ── 3. task_log_stream：技能来源标注 ───────────────────────────────

def test_build_skill_source_message():
    m = build_skill_source_message("caserun-prep", "caserun-prep", "bound", ok=True)
    assert m["type"] == "log"
    text = m["data"]["text"]
    assert "bound=caserun-prep" in text
    assert "actual=caserun-prep" in text
    assert "source=bound" in text
    assert m["data"]["skill_source"]["bound"] == "caserun-prep"
    assert m["data"]["skill_source"]["ok"] is True


def test_skill_source_message_payload_passthrough():
    m = build_skill_source_message("caserun-prep", "casegen-prep", "local_cache",
                                   ok=False, reason="cache mismatch")
    p = _to_payload(m)
    assert p is not None
    assert "bound=caserun-prep" in p["content"]
    assert p["skill_source"]["actual"] == "casegen-prep"
    assert p["skill_source"]["ok"] is False
    assert p["skill_source"]["reason"] == "cache mismatch"


# ── 4. worker 执行前硬校验（_skill_consistency_gate） ───────────────

class _FakeClient:
    def __init__(self):
        self.reported = []

    async def report_messages(self, tid, msgs):
        self.reported.append((tid, msgs))
        return {"status": "ok"}


class _FakeRes:
    def __init__(self):
        self.disposed = False

    def dispose(self):
        self.disposed = True


def _run_gate(task, skill_dir):
    from src.runtime.agent import _skill_consistency_gate
    client = _FakeClient()
    res = _FakeRes()
    err = asyncio.run(
        _skill_consistency_gate(client, task.get("id", "t-887"), task, skill_dir, res))
    return err, client, res


def test_gate_refuses_local_cache_mismatch(tmp_path):
    # seq=46108 回归：绑定 caserun-prep + 本地缓存 casegen-prep → 拒绝执行并显式报错
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "casegen-prep")
    task = {"id": "t-46108", "skill": "caserun-prep"}
    err, client, res = _run_gate(task, skill_dir)
    assert err is not None
    assert err["_error"].startswith("[skill_mismatch]")
    assert "casegen-prep" in err["_error"]
    assert res.disposed is True
    # 拒绝前仍上报技能来源日志（绑定值 vs 实际值，可观测）
    assert client.reported, "skill source log must be reported before refusing"
    msg = client.reported[0][1][0]
    assert "bound=caserun-prep" in msg["data"]["text"]
    assert "actual=casegen-prep" in msg["data"]["text"]
    assert msg["data"]["skill_source"]["ok"] is False


def test_gate_passes_when_bound_materialized(tmp_path):
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "caserun-prep")
    task = {"id": "t-887-ok", "skill": "caserun-prep"}
    err, client, res = _run_gate(task, skill_dir)
    assert err is None
    assert res.disposed is False
    msg = client.reported[0][1][0]
    assert msg["data"]["skill_source"]["source"] == "bound"
    assert msg["data"]["skill_source"]["ok"] is True


def test_gate_passes_unbound_with_cache_fallback(tmp_path):
    # 绑定为空 → 本地缓存兜底放行（日志标注 source=unbound）
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "casegen-prep")
    task = {"id": "t-887-unbound", "skill": ""}
    err, client, _ = _run_gate(task, skill_dir)
    assert err is None
    assert client.reported[0][1][0]["data"]["skill_source"]["source"] == "unbound"


# ── 5. direct 端（event_pipeline._materialize_direct_workdir） ─────

def test_direct_materialize_consistency_ok(tmp_path, monkeypatch):
    import src.config as cfg
    official = tmp_path / "off" / "caserun-prep"
    official.mkdir(parents=True)
    (official / "SKILL.md").write_text("# caserun-prep\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "resolve_official_skill_path",
                        lambda name: str(official / "SKILL.md"))
    from src.api.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    _materialize_direct_workdir(str(tmp_path), "gitcode:acme/repo", "qa",
                                 "caserun-prep", evt, "", "prompt")
    sv = evt.get("_skill_consistency") or {}
    assert sv.get("ok") is True
    assert sv.get("source") == "bound"


def test_direct_materialize_local_cache_leftover_refused(tmp_path, monkeypatch):
    # direct 端：绑定未物化 + 工位缓存残留其它流水线技能 → ok=False（调用方拒绝派发）
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    skill_dir = tmp_path / ".skills"
    _mk_skill(str(skill_dir), "casegen-prep")
    from src.api.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    _materialize_direct_workdir(str(tmp_path), "gitcode:acme/repo", "qa",
                                "caserun-prep", evt, "", "prompt")
    sv = evt.get("_skill_consistency") or {}
    assert sv.get("ok") is False
    assert sv.get("actual") == "casegen-prep"
    assert sv.get("source") == "local_cache"


# ── 6. 派发 payload 携带绑定 skill（env GRAPE_BOUND_SKILL） ─────────

def test_enqueue_task_carries_bound_skill_env():
    from src.session_manager import db, db_lock
    from src.runtime import service as svc
    tid = svc.enqueue_task(agent_session_id="sess-887", runtime_id=1,
                           prompt="p", skill="caserun-prep",
                           repo_key="gitcode:o/test887", agent_type="qa")
    assert tid
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT env_extra FROM agent_tasks WHERE id=?", (tid,))
            row = c.fetchone()
        env = json.loads(row[0] or "{}")
        assert env.get("GRAPE_BOUND_SKILL") == "caserun-prep"
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
            db.commit()


def test_enqueue_task_bound_skill_env_empty_when_unbound():
    from src.runtime import service as svc
    from src.session_manager import db, db_lock
    tid = svc.enqueue_task(agent_session_id="sess-887", runtime_id=1,
                           prompt="p", skill="",
                           repo_key="gitcode:o/test887", agent_type="qa")
    assert tid
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT env_extra FROM agent_tasks WHERE id=?", (tid,))
            row = c.fetchone()
        env = json.loads(row[0] or "{}")
        assert env.get("GRAPE_BOUND_SKILL", "") == ""
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
            db.commit()
