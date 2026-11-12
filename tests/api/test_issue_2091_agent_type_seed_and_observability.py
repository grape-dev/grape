"""Issue #2091（上游 #10）回归：存量库补种 agent_type + Agent 路由异常不再静默。

1. 补种迁移 `_fn_seed_agent_types_from_config()`：空表补齐、幂等、不覆盖用户改过的行、
   种子文件缺失只告警；
2. 路由可观测（同时覆盖 #2096/上游 #15 的核心验收）：agent_type 查询失败必须 500 +
   明确 error_code，而不是 200 空数组 / 404「未配置」。
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEVOPS_DB_PATH", str(ROOT / "data" / "test_sessions.db"))

PROBE_TYPE = "zz_seed_probe"


@pytest.fixture()
def probe_config(tmp_path, monkeypatch):
    import src.api.db_setup as dbs
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "agent-description.json").write_text(
        json.dumps({PROBE_TYPE: {"name": "探针 Agent", "description": "seed probe", "icon": "🧪"}}),
        encoding="utf-8")
    monkeypatch.setattr(dbs, "_CONFIG_DIR", str(cfg))
    yield dbs
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("DELETE FROM agent_type WHERE agent_type=?", (PROBE_TYPE,))
        dbs.db.commit()


def _rows(dbs, agent_type=PROBE_TYPE):
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("SELECT agent_type, display_name, icon FROM agent_type WHERE agent_type=?", (agent_type,))
        return c.fetchall()


def _meta_seeded_keys(dbs) -> set:
    """读 meta.seeded_keys（无/空 → 空 set）。"""
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("SELECT value FROM meta WHERE key='seeded_keys'")
        row = c.fetchone()
        return set(row[0].split(",")) if row and row[0] else set()


def test_seed_syncs_meta_seeded_keys_when_absent(probe_config):
    """Spec 要求：新轨补种成功后必须同步 meta.seeded_keys（QA 第 1 项缺漏）。"""
    dbs = probe_config
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_keys', '')")
        dbs.db.commit()
    assert not _rows(dbs), "前置：探针类型应不存在"
    assert "agent_type" not in _meta_seeded_keys(dbs), "前置：meta.seeded_keys 不含 agent_type"

    dbs._fn_seed_agent_types_from_config()

    assert _rows(dbs), "补种应成功"
    assert "agent_type" in _meta_seeded_keys(dbs), "补种成功后 meta.seeded_keys 应含 agent_type"


def test_seed_preserves_existing_seeded_keys_entries(probe_config):
    """同步不得改写既有记账：其他 seed key 保留，已含 agent_type 时不重复写。"""
    dbs = probe_config
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_keys', 'other_seed')")
        dbs.db.commit()
    dbs._fn_seed_agent_types_from_config()
    seeded = _meta_seeded_keys(dbs)
    assert "other_seed" in seeded and "agent_type" in seeded, f"应合并记账，实得 {seeded}"

    # 幂等：再跑一次，0 行新增，记账保持不变
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("DELETE FROM agent_type WHERE agent_type=?", (PROBE_TYPE,))
        dbs.db.commit()
    dbs._fn_seed_agent_types_from_config()
    seeded2 = _meta_seeded_keys(dbs)
    assert seeded2 == seeded, f"无新增行时不得改写记账，实得 {seeded2}"


def test_seed_fills_missing_agent_type(probe_config):
    dbs = probe_config
    assert not _rows(dbs), "前置：探针类型应不存在"
    dbs._fn_seed_agent_types_from_config()
    rows = _rows(dbs)
    assert len(rows) == 1 and rows[0][1] == "探针 Agent", f"应补种成功，实得 {rows}"


def test_seed_is_idempotent_and_keeps_user_edits(probe_config):
    dbs = probe_config
    dbs._fn_seed_agent_types_from_config()
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("UPDATE agent_type SET display_name=?, icon=? WHERE agent_type=?",
                  ("我改过的名字", "🛠", PROBE_TYPE))
        dbs.db.commit()
    dbs._fn_seed_agent_types_from_config()
    rows = _rows(dbs)
    assert rows[0][1] == "我改过的名字" and rows[0][2] == "🛠", f"不得覆盖用户改过的行，实得 {rows}"


def test_seed_skips_when_config_missing(probe_config, tmp_path, monkeypatch):
    dbs = probe_config
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(dbs, "_CONFIG_DIR", str(empty))
    dbs._fn_seed_agent_types_from_config()
    assert not _rows(dbs)


def test_agents_types_returns_500_not_empty_list_on_query_failure(monkeypatch):
    """核心验收：查询失败 → 5xx 且带 error_code，不再返回 200 空数组。"""
    from fastapi.testclient import TestClient
    from src.api.main import app

    class _BrokenCursor:
        def execute(self, *a, **k):
            raise RuntimeError("no such column: order_num")
        def fetchall(self):
            raise RuntimeError("no such column: order_num")

    class _BrokenDB:
        def cursor(self):
            return _BrokenCursor()

    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _BrokenDB(), raising=False)

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/agents/types")
    assert r.status_code == 500, f"查询失败必须 500，实得 {r.status_code}: {r.text[:200]}"
    body = r.json()
    detail = body.get("detail") if isinstance(body, dict) else None
    assert isinstance(detail, dict) and detail.get("error_code") == "AGENT_TYPE_LIST_FAILED", body
