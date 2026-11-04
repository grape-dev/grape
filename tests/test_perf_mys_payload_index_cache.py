"""weekly-mys 性能补丁回归（2026-11-03）

覆盖：
1. #859 payload 按需内联：每个 issue 只补最新一条，其余标记 payload_omitted、
   且保留 output=None / logs=[] 形状（前端不会因缺键报错）
2. #859 单条 payload 超 32KB → 截断标记 *_truncated，不再把 MB 级日志塞进响应
3. #859 旧库（无 structured_output/agent_logs 列）不触发 payload 补查
4. obs_llm_usage engine_id 索引迁移：建索引 + 两次调用幂等
5. detect_engine_model 60s TTL 缓存：同窗口只解析一次，过期后重算
"""
import json
import os
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest
from src.session_manager import db, db_lock

import src.runtime  # 环形导入防护（src.agent.* / routes 侧导入顺序）
import src.api.routes.issues as iss
import src.runtime.engine_model as em

REPO = "gitcode:owner/perf-mys-test"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()


def _seed(seq, issue_id, so=None, logs=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, "
            "agent_type, runtime, skill, status, entity_type, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (seq, REPO, "agent_done", "gitcode", issue_id, f"t{seq}",
             "coding", "rt-1", "sk-1", "completed", "issue"))
        c.execute("UPDATE events SET structured_output=?, agent_logs=? WHERE seq=?",
                  (json.dumps(so) if so is not None else None,
                   json.dumps(logs) if logs is not None else "[]", seq))
        db.commit()


def _get(seq):
    for evs in iss._group_events_by_issue([REPO]).values():
        for e in evs:
            if e["seq"] == seq:
                return e
    return None


def test_only_latest_event_carries_payload():
    _seed(9001, "#1", so={"verdict": "old"}, logs=["old-log"])
    _seed(9002, "#1", so={"verdict": "new"}, logs=["new-log"])
    _seed(9003, "#2", so={"verdict": "other"}, logs=["other-log"])

    old = _get(9001)
    new = _get(9002)
    other = _get(9003)

    assert new["output"] == {"verdict": "new"} and new["logs"] == ["new-log"]
    assert other["output"] == {"verdict": "other"} and other["logs"] == ["other-log"]
    # 非最新事件：形状保持，但不内联 payload
    assert old["output"] is None and old["logs"] == []
    assert old.get("payload_omitted") is True
    assert "payload_omitted" not in new


def test_oversized_payload_truncated():
    cap = iss._EVENT_PAYLOAD_MAX_CHARS
    big = ["x" * 1024 for _ in range(cap // 1024 + 8)]  # > cap
    _seed(9101, "#3", so={"verdict": "big"}, logs=big)
    ev = _get(9101)
    assert ev.get("logs_truncated") is True
    assert ev["logs"] == []
    # structured_output 很小，仍应正常解析
    assert ev["output"] == {"verdict": "big"}


def test_legacy_db_without_payload_columns(monkeypatch):
    """旧库无 payload 列：主查询照样可用，且不会去补查 payload。"""
    calls = {"n": 0}
    real = iss._attach_event_payloads

    def boom(group_map):
        calls["n"] += 1
        return real(group_map)

    monkeypatch.setattr(iss, "_attach_event_payloads", boom)

    class _Cur:
        def execute(self, sql, *a):
            if "PRAGMA table_info(events)" in sql:
                self._rows = [(0, "seq"), (1, "repo_key"), (2, "issue_id")]
            else:
                self._rows = [(1, "#9", "agent_done", "coding", "t", "b",
                               "s", "done", "2026-11-03T00:00:00Z", "rt", "sk", "")] if "FROM events" in sql else []
            return self

        def fetchall(self):
            return self._rows

    class _DB:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr("src.session_manager.db", _DB())
    out = iss._group_events_by_issue([REPO])
    assert out["9"][0]["runtime"] == "rt"
    assert calls["n"] == 0, "旧库不应触发 payload 补查"


def test_index_migration_idempotent():
    from src.api import db_setup
    with db_lock:
        c = db.cursor()
        c.execute("PRAGMA index_list(obs_llm_usage)")
        _ = c.fetchall()

    db_setup._fn_add_obs_llm_usage_engine_index()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                  ("idx_obs_llm_engine_model",))
        assert c.fetchone(), "索引未创建"
        c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
                  ("idx_obs_llm_engine_model",))
        first = c.fetchone()[0]
    db_setup._fn_add_obs_llm_usage_engine_index()  # 再跑一次不应报错/重复
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
                  ("idx_obs_llm_engine_model",))
        assert c.fetchone()[0] == first == 1


def test_detect_engine_model_cache(monkeypatch):
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return "cached-model"

    monkeypatch.setitem(em._PARSERS, "opencode", fake)
    monkeypatch.setattr(em, "_model_cache", {})

    assert em.detect_engine_model("opencode") == "cached-model"
    assert em.detect_engine_model("opencode") == "cached-model"
    assert calls["n"] == 1, "指纹不变时应命中缓存"

    sig, ts, val = em._model_cache["opencode"]
    em._model_cache["opencode"] = (sig, ts - em._MODEL_CACHE_TTL - 1, val)
    assert em.detect_engine_model("opencode") == "cached-model"
    assert calls["n"] == 2, "TTL 过期后应重算"


def test_detect_cache_invalidated_by_config_change(monkeypatch, tmp_path):
    """配置文件内容一变（mtime/大小变化）→ 立刻重算，不等 TTL。"""
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    cfg = home / ".config" / "opencode" / "opencode.json"
    cfg.write_text(json.dumps({"model": "model-a"}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(em, "_model_cache", {})

    assert em.detect_engine_model("opencode") == "model-a"
    assert em.detect_engine_model("opencode") == "model-a"  # 命中缓存
    cfg.write_text(json.dumps({"model": "model-b-longer"}))
    assert em.detect_engine_model("opencode") == "model-b-longer", "配置改动应立即生效"