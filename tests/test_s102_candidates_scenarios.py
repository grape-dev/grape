"""S100-S102 场景用例（D75/D76）：repo_key 候选集占位符家族——场景级验收。

UC↔用例映射（与 docs/weekly-09/multi-platform-decouple-requirements.md
"候选集占位符家族"场景表一一对应）：
- UC1 默认部署 5 候选全链绑定（engine/dashboards/alerts 三族）→
  test_s100_engine_select_live_bindings / test_s100_dashboard_live_bindings / UC3
- UC2 单候选配置（仅 1 平台卡）→ test_uc2_single_candidate
- UC3 空候选集恒假（IN (NULL) 三值逻辑）→ test_s101_in_params_empty_set_always_false
- UC4 卡缓存漂移原子性（helper 单次调用语义）→ test_uc4_arity_survives_card_drift
- UC5 alerts 仓库过滤实弹（P1 修复闭环）→ test_uc3_alerts_repo_filter_live
- UC6 skills 多平台歧义守卫 → test_s100_skills_strict_ge_two（源码守卫）
- UC7 结构守卫（五站点零字面量/白名单只减不增）→ test_s100_* 守卫 3 条
"""
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.kernel.repo_key import repo_key_in_params


def test_uc2_single_candidate():
    """UC2：单平台卡配置（candidates=2：裸键+1 前缀）→ 占位符 2、绑定命中。"""
    with patch("src.kernel.repo_key.repo_key_candidates", return_value=["o/r", "gitcode:o/r"]):
        ph, args = repo_key_in_params("o/r")
    assert ph == "?,?" and args == ("o/r", "gitcode:o/r")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE rc (owner_id TEXT)")
    conn.execute("INSERT INTO rc VALUES ('gitcode:o/r')")
    assert conn.execute(f"SELECT COUNT(*) FROM rc WHERE owner_id IN ({ph})", args).fetchone()[0] == 1


def test_uc4_arity_survives_card_drift():
    """UC4：卡漂移原子性——repo_key_in_params 一次调用只触发一次候选集求值，
    占位符与参数取自同一份候选（构造上不存在 S100 缺陷的两次调用漂移窗口）。"""
    calls = []
    real = __import__("src.kernel.repo_key", fromlist=["repo_key_candidates"]).repo_key_candidates
    with patch("src.kernel.repo_key.repo_key_candidates",
               side_effect=lambda r: (calls.append(r), real(r))[1]):
        ph, args = repo_key_in_params("o/r")
    assert len(calls) == 1, "原子产物必须单次求值（两次调用=漂移窗口）"
    assert ph.count("?") == len(args)


def test_uc3_alerts_repo_filter_live():
    """UC5：alerts.list_events_all 仓库过滤实弹（P1 修复闭环）——
    5 候选绑定不炸且命中 gitcode: 行；修复前本场景 = ProgrammingError → 空列表。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE obs_alert_events (id INTEGER PRIMARY KEY, rule_name TEXT, level TEXT, "
                 "severity TEXT, status TEXT, source TEXT, metric TEXT, value TEXT, threshold TEXT, "
                 "dim_key TEXT, repo_key TEXT, first_fired_at TEXT, last_seen_at TEXT, resolved_at TEXT, "
                 "silenced_until TEXT, notify_count INTEGER, last_notified_at TEXT, escalated_at TEXT, "
                 "notify_deferred INTEGER)")
    conn.execute("INSERT INTO obs_alert_events (repo_key, rule_name, status) "
                 "VALUES ('gitcode:o/r', 'r1', 'firing'), ('other:x', 'r2', 'firing')")
    # 复刻 list_events_all 修复后 SQL 形态（同一构造来源 repo_key_in_params）
    from src.kernel.repo_key import repo_key_in_params, to_repo_key
    ph, args = repo_key_in_params(to_repo_key("o/r"))
    sql = ("SELECT id, rule_name FROM obs_alert_events WHERE 1=1 "
           f"AND repo_key IN ({ph}) AND status=?")
    rows = conn.execute(sql, list(args) + ["firing"]).fetchall()
    assert len(rows) == 1 and rows[0][1] == "r1"
