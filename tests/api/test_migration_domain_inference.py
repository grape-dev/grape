"""迁移域判定：显式 env → 库名推断 → 角色映射（S1–S7）。

背景（2026-11-21 实测事故）：四进程 env 只有 `GRAPE_DOMAIN_LIB=1`、没有
`GRAPE_MIGRATION_OWNER` 时，`current_migration_domain()` 返回空串 = **不筛域**：

  · mgmt 进程以「全量迁移」形态把 runtime 域的 DDL 建进自己的库
    → 迁移 202611200006 把 `agent_runtimes.desired_plugins` 建在 `app_mgmt`
      （那张表 0 行），而 `app_runtime` 有 19 行却缺这一列；
  · event/insight 的 `run_domain_migrations()` 看到 owner 为空直接 return
    → 本域库永远拿不到该有的列和台账表（连 `grape_t_schema_migrations` 都没有）。

结果：引擎注册连续 500 `(1054, "Unknown column 'desired_plugins'")`，而台账看起来
是「已盖章」的假绿。部署脚本漏一个 env 就能造成这种事故，必须由代码兜住。

场景表（详见 docs/weekly-11/migration-domain-inference-scenarios.md）：
  S1 显式 GRAPE_MIGRATION_OWNER 优先
  S2 env 缺失 → 由 DEVOPS_DB_NAME 推断（runtime）
  S3 env 缺失 → 由 DEVOPS_DB_NAME 推断（mgmt）
  S4 env 是非法值 → 回落推断（不让脏值静默变「不筛域」）
  S5 库名判不出 → 由进程角色映射（event/insight/server）
  S6 单库模式（GRAPE_DOMAIN_LIB 非 1）→ 仍返回空串（历史行为不变）
  S7 worker 角色 → 域迁移入口直接跳过（与 event 共库，不并发跑同一批 DDL）
"""
from __future__ import annotations

import pytest

from src.api import db_setup as D


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("GRAPE_DOMAIN_LIB", "GRAPE_MIGRATION_OWNER", "DEVOPS_DB_NAME",
              "GRAPE_PROCESS_ROLE", "GRAPE_SERVICE_NAME"):
        monkeypatch.delenv(k, raising=False)
    yield


def _three_db(monkeypatch, *, owner=None, dbname=None, role=None):
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    if owner is not None:
        monkeypatch.setenv("GRAPE_MIGRATION_OWNER", owner)
    if dbname is not None:
        monkeypatch.setenv("DEVOPS_DB_NAME", dbname)
    if role is not None:
        monkeypatch.setenv("GRAPE_PROCESS_ROLE", role)


def test_s1_explicit_owner_wins(monkeypatch):
    """S1：显式 env 是权威来源，优先于任何推断。"""
    _three_db(monkeypatch, owner="insight", dbname="app_runtime", role="worker")
    assert D.current_migration_domain() == "insight"


def test_s2_infer_runtime_from_db_name(monkeypatch):
    """S2：env 缺失 → 库名推断为 runtime（本机 event 进程的真实形态）。"""
    _three_db(monkeypatch, dbname="app_runtime")
    assert D.current_migration_domain() == "runtime"


def test_s3_infer_mgmt_from_db_name(monkeypatch):
    """S3：mgmt 库 → mgmt。这条最关键 —— 它让 mgmt 进程不再跑全量。"""
    _three_db(monkeypatch, dbname="app_mgmt")
    assert D.current_migration_domain() == "mgmt"


def test_s4_invalid_owner_falls_back_to_inference(monkeypatch):
    """S4：env 是脏值（拼错/空白）→ 回落推断，不得静默变成「不筛域」。"""
    _three_db(monkeypatch, owner="runtme", dbname="app_runtime")
    assert D.current_migration_domain() == "runtime"


def test_s5_role_mapping_when_db_name_unknown(monkeypatch):
    """S5：库名判不出域（单库命名/自定义库）→ 用进程角色兜底。"""
    _three_db(monkeypatch, dbname="some_custom_db", role="event")
    assert D.current_migration_domain() == "runtime"

    _three_db(monkeypatch, dbname="some_custom_db", role="insight")
    assert D.current_migration_domain() == "insight"

    _three_db(monkeypatch, dbname="some_custom_db", role="server")
    assert D.current_migration_domain() == "mgmt"


def test_s6_single_db_mode_stays_empty(monkeypatch):
    """S6：单库部署不筛域（历史行为不变），不能因为推断把单库部署切成分域。"""
    monkeypatch.setenv("DEVOPS_DB_NAME", "app_runtime")
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "event")
    assert D.current_migration_domain() == ""


def test_s7_worker_skips_domain_migrations(monkeypatch):
    """S7：worker 与 event 共库 → 域迁移入口必须直接跳过（不并发跑同一批 DDL）。

    注意这一跳要在推断之前：库名推断会把 worker 也判成 runtime。
    """
    _three_db(monkeypatch, dbname="app_runtime", role="worker")
    calls: list[str] = []
    monkeypatch.setattr(D, "_ensure_migrations_ledger", lambda db: calls.append("ledger"))
    monkeypatch.setattr(D, "_run_migrations_with_conn",
                        lambda *a, **k: calls.append("run"))

    D.run_domain_migrations()

    assert calls == [], "worker 不得执行域迁移（台账/DDL 都不该碰）"
    # 但它的域判定本身仍是 runtime —— 供日志与其他入口使用
    assert D.current_migration_domain() == "runtime"

def test_s8_inferred_domain_does_not_trigger_error_guard(monkeypatch, caplog):
    """S8：推断成功时 migration-guard 不该报 ERROR（否则真问题会被淹掉）。

    实测场景：env 漏配、但库名推断已让 mgmt 筛成 122→82 条，日志却仍刷
    「将以不筛域形态执行全量迁移」—— 噪音会掩盖真正的漏配。
    """
    import logging

    _three_db(monkeypatch, dbname="app_mgmt", role="server")
    monkeypatch.setattr(D, "log", logging.getLogger("test.migration.guard"), raising=False)
    with caplog.at_level(logging.INFO):
        D._warn_missing_migration_owner_if_multi_db("pending")

    assert "ERROR" not in caplog.text, "推断成功不得报 ERROR"
    if caplog.text:
        assert "已按本进程推断域" in caplog.text or "库名/角色" in caplog.text
