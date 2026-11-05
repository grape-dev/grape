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


# ── 1b. 引擎原生技能目录扫描（QA 修复：seq=46108 原生缓存残留漏检） ──

def test_verify_bound_in_engine_native_ok(tmp_path):
    # 绑定未物化但引擎原生缓存持有同名技能 → 引擎按绑定名加载，放行 + 显式标注
    skill_dir = str(tmp_path / ".skills")
    native = str(tmp_path / "native")
    os.makedirs(skill_dir, exist_ok=True)
    _mk_skill(native, "caserun-prep")
    v = sm.verify_skill_execution_consistency(
        "caserun-prep", skill_dir, extra_scan_dirs=[native])
    assert v["ok"] is True
    assert v["source"] == "engine_native"
    assert v["actual"] == "caserun-prep"


def test_verify_engine_native_leftover_refused(tmp_path):
    # seq=46108 原生缓存版：绑定未物化 + .skills 空 + 引擎原生缓存持有其它技能
    # （casegen-prep 残留）且无 FALLBACK.md → 拒绝（不再漏检）
    skill_dir = str(tmp_path / ".skills")
    native = str(tmp_path / "native")
    os.makedirs(skill_dir, exist_ok=True)
    _mk_skill(native, "casegen-prep")
    v = sm.verify_skill_execution_consistency(
        "caserun-prep", skill_dir, extra_scan_dirs=[native])
    assert v["ok"] is False
    assert v["source"] == "engine_native"
    assert v["actual"] == "casegen-prep"
    assert "casegen-prep" in v["reason"]
    assert "refusing execution" in v["reason"]


def test_verify_engine_native_leftover_with_fallback_md_ok(tmp_path):
    # FALLBACK.md（#607 权威取回指引）优先于原生缓存残留 → 降级放行
    skill_dir = str(tmp_path / ".skills")
    native = str(tmp_path / "native")
    _mk_skill(skill_dir, "caserun-prep", with_md=False, fallback=True)
    _mk_skill(native, "casegen-prep")
    v = sm.verify_skill_execution_consistency(
        "caserun-prep", skill_dir, extra_scan_dirs=[native])
    assert v["ok"] is True
    assert v["source"] == "degraded_recovery"


def test_skill_mismatch_error_and_contract_key():
    # 拒绝消息单一实现 + evt 契约键常量（event_pipeline/scheduler 共用）
    assert sm.SKILL_CONSISTENCY_KEY == "_skill_consistency"
    msg = sm.skill_mismatch_error({"reason": "boom"})
    assert msg == "[skill_mismatch] boom"
    assert sm.skill_mismatch_error({}) == "[skill_mismatch] skill consistency check failed"


# ── 1c. worker 官方包回退物化名≠绑定名 → 回写 skill_fallback（QA 自相矛盾修复） ──

def test_worker_official_fallback_writes_back_declared(tmp_path, monkeypatch):
    # 物化官方目录名（checker-x）≠绑定名 → 回写 task.skill_fallback，
    # 门 verify 识别为 declared_fallback 放行，而非 local_cache 拒绝
    import src.config as cfg
    skill_dir = str(tmp_path / ".skills")
    official = tmp_path / "official" / "checker-x"
    official.mkdir(parents=True)
    (official / "SKILL.md").write_text("# checker-x\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "resolve_official_skill_path",
                        lambda name: str(official / "SKILL.md"))
    from src.runtime.agent import _materialize_worker_skill
    task = {"id": "t-887-fb", "skill": "caserun-prep", "skill_files": "{}",
            "skill_fallback": {"skill_name": "checker-x", "skill_id": 42}}
    _materialize_worker_skill(task, {}, "p", skill_dir)
    assert task["skill_fallback"] == {"skill_name": "checker-x", "skill_id": 42}
    v = sm.verify_skill_execution_consistency(
        task["skill"], skill_dir, task["skill_fallback"])
    assert v["ok"] is True
    assert v["source"] == "declared_fallback"
    assert v["actual"] == "checker-x"


def test_worker_official_fallback_diff_name_writes_back(tmp_path, monkeypatch):
    # 无 skill_fallback claim，官方按名解析到目录名不同的包 → 同样回写 declared
    import src.config as cfg
    skill_dir = str(tmp_path / ".skills")
    official = tmp_path / "official" / "caserun-alt"
    official.mkdir(parents=True)
    (official / "SKILL.md").write_text("# caserun-alt\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "resolve_official_skill_path",
                        lambda name: str(official / "SKILL.md"))
    from src.runtime.agent import _materialize_worker_skill
    task = {"id": "t-887-fb2", "skill": "caserun-prep", "skill_files": "{}",
            "skill_fallback": {}}
    _materialize_worker_skill(task, {}, "p", skill_dir)
    assert task["skill_fallback"]["skill_name"] == "caserun-alt"
    v = sm.verify_skill_execution_consistency(
        task["skill"], skill_dir, task["skill_fallback"])
    assert v["ok"] is True
    assert v["source"] == "declared_fallback"


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


# ── 4b. worker 门：env 双源互校 + 引擎原生目录扫描（QA 修复） ──

def test_gate_env_bound_conflict_refused(tmp_path):
    # task.skill 与 env GRAPE_BOUND_SKILL 不一致（绑定基准被覆盖/漂移）→ 拒绝
    from src.runtime.agent import _skill_consistency_gate
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "caserun-prep")
    task = {"id": "t-887-env", "skill": "caserun-prep"}
    client = _FakeClient()
    res = _FakeRes()
    err = asyncio.run(_skill_consistency_gate(
        client, "t-887-env", task, skill_dir, res,
        env={"GRAPE_BOUND_SKILL": "casegen-prep"}))
    assert err is not None
    assert err["_error"].startswith("[skill_mismatch]")
    assert "GRAPE_BOUND_SKILL" in err["_error"]
    assert res.disposed is True
    msg = client.reported[0][1][0]
    assert msg["data"]["skill_source"]["source"] == "env_conflict"
    assert msg["data"]["skill_source"]["ok"] is False


def test_gate_env_bound_used_when_task_skill_empty(tmp_path):
    # task.skill 为空但 env 有绑定值（绑定基准兜底）→ 以 env 值校验
    from src.runtime.agent import _skill_consistency_gate
    skill_dir = str(tmp_path / ".skills")
    _mk_skill(skill_dir, "caserun-prep")
    task = {"id": "t-887-env2", "skill": ""}
    client = _FakeClient()
    err = asyncio.run(_skill_consistency_gate(
        client, "t-887-env2", task, skill_dir, _FakeRes(),
        env={"GRAPE_BOUND_SKILL": "caserun-prep"}))
    assert err is None
    assert client.reported[0][1][0]["data"]["skill_source"]["source"] == "bound"
    assert client.reported[0][1][0]["data"]["skill_source"]["bound"] == "caserun-prep"


def test_gate_scans_engine_native_dirs(tmp_path, monkeypatch):
    # seq=46108 原生缓存版回归：绑定未物化 + .skills 空 + 引擎原生缓存残留
    # casegen-prep → 门必须拒绝（不再漏检）
    from src.runtime.agent import _skill_consistency_gate
    skill_dir = str(tmp_path / ".skills")
    native = str(tmp_path / "native")
    os.makedirs(skill_dir, exist_ok=True)
    _mk_skill(native, "casegen-prep")
    import src.runtime.os_adapter as osa
    monkeypatch.setattr(osa, "engine_skill_dirs", lambda name: [native])
    task = {"id": "t-887-native", "skill": "caserun-prep"}
    client = _FakeClient()
    err = asyncio.run(_skill_consistency_gate(
        client, "t-887-native", task, skill_dir, _FakeRes(),
        env={}, engine_name="opencode-worker-1"))
    assert err is not None
    assert err["_error"].startswith("[skill_mismatch]")
    assert "casegen-prep" in err["_error"]
    msg = client.reported[0][1][0]
    assert msg["data"]["skill_source"]["source"] == "engine_native"
    assert msg["data"]["skill_source"]["actual"] == "casegen-prep"


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


def test_direct_materialize_engine_native_leftover_refused(tmp_path, monkeypatch):
    # QA 复审修复：seq=46108 原生缓存变体——绑定未物化 + 工位 .skills 空 +
    # casegen-prep 残留引擎原生缓存 → direct 端判定必须 ok=False（不再静默放行），
    # 与 worker 门（_skill_consistency_gate）扫描口径一致。
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    skill_dir = str(tmp_path / ".skills")
    native = str(tmp_path / "native")
    os.makedirs(skill_dir, exist_ok=True)
    _mk_skill(native, "casegen-prep")
    import src.runtime.os_adapter as osa
    monkeypatch.setattr(osa, "engine_skill_dirs", lambda name: [native])
    # 物化降级会写 FALLBACK.md（#607 权威取回优先于原生缓存判定放行）——
    # 本用例聚焦「无 FALLBACK 指引时的原生缓存残留拒绝」，屏蔽降级文件写入。
    monkeypatch.setattr(sm, "write_skill_fallback", lambda *a, **k: None)
    from src.api.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    _materialize_direct_workdir(str(tmp_path), "gitcode:acme/repo", "qa",
                                "caserun-prep", evt, "", "prompt",
                                engine_name="opencode-server-1")
    sv = evt.get(sm.SKILL_CONSISTENCY_KEY) or {}
    assert sv.get("ok") is False
    assert sv.get("actual") == "casegen-prep"
    assert sv.get("source") == "engine_native"


def test_direct_materialize_engine_native_same_skill_allowed(tmp_path, monkeypatch):
    # 绑定技能存在于引擎原生缓存（内容一致的本地副本）→ 放行并显式标注
    # source=engine_native、actual=bound（不因原生目录存在而误拒）。
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    skill_dir = str(tmp_path / ".skills")
    native = str(tmp_path / "native")
    os.makedirs(skill_dir, exist_ok=True)
    _mk_skill(native, "caserun-prep")
    import src.runtime.os_adapter as osa
    monkeypatch.setattr(osa, "engine_skill_dirs", lambda name: [native])
    monkeypatch.setattr(sm, "write_skill_fallback", lambda *a, **k: None)
    from src.api.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    _materialize_direct_workdir(str(tmp_path), "gitcode:acme/repo", "qa",
                                "caserun-prep", evt, "", "prompt",
                                engine_name="hermes-node-2")
    sv = evt.get(sm.SKILL_CONSISTENCY_KEY) or {}
    assert sv.get("ok") is True
    assert sv.get("source") == "engine_native"
    assert sv.get("actual") == "caserun-prep"


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


# ── 6b. GRAPE_BOUND_SKILL 权威注入（不可被 env_extra 覆盖，QA 修复） ──

def test_enqueue_task_overrides_spoofed_bound_skill_env():
    from src.session_manager import db, db_lock
    from src.runtime import service as svc
    tid = svc.enqueue_task(agent_session_id="sess-887", runtime_id=1,
                            prompt="p", skill="caserun-prep",
                            repo_key="gitcode:o/test887", agent_type="qa",
                            env_extra={"GRAPE_BOUND_SKILL": "casegen-prep"})
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


# ── 7. 健康检查聚合项（QA AC4：check_skill_consistency 接入端点汇总） ──

def test_aggregate_skill_consistency_counts_refusals():
    from src.session_manager import db, db_lock
    from src.runtime.health import aggregate_skill_consistency
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, "
            "status, result_text, completed_at) VALUES ('t-887-agg', 'sess-887', "
            "1, 'p', 'failed', '[skill_mismatch] test refusal', ?)",
            (_now,))
        db.commit()
    try:
        agg = aggregate_skill_consistency()
        assert agg["name"] == "skill_consistency"
        assert agg["status"] == "warn"
        assert agg["refused_recent"] >= 1
        assert "skill" in agg["detail"]
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id='t-887-agg'")
            db.commit()
    agg0 = aggregate_skill_consistency()
    assert agg0["status"] == "ok"
    assert agg0["refused_recent"] == 0
