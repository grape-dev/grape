"""InsightService LocalProvider — 同实例跨 schema SQL 实现（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.5.

同实例模式：直接查 grape_insight schema（SQLite 用 ins_ 前缀）。
表名通过 t_insight() 映射，在 GRAPE_SCHEMA_MODE 未启用时返回裸表名（存量零回归）。
"""
from __future__ import annotations

import json
import time
from typing import Any

from src.kernel import db_registry, session_manager
from src.kernel.db import t_insight, t_runtime

_DOMAIN = "insight"


def _t(name: str) -> str:
    """表名：自有 DSN → 裸名；共享进程库 → 跨 schema 前缀（与 RuntimeServiceLocal 同口径）。"""
    return name if db_registry.uses_own_dsn(_DOMAIN) else t_insight(name)
from src.domains.contracts.insight_service import InsightService


class InsightServiceLocal(InsightService):
    """同实例模式：直接查 grape_insight schema（SQLite 用 ins_ 前缀）。"""

    @property
    def _own_dsn(self) -> bool:
        return db_registry.uses_own_dsn(_DOMAIN)

    @property
    def _db(self):
        return db_registry.domain_db(_DOMAIN) or session_manager.db

    @property
    def _lock(self):
        return db_registry.domain_lock(_DOMAIN) if self._own_dsn else session_manager.db_lock

    def log_obs(self, entry: dict) -> Any:
        """Runtime 写可观测性日志（obs_log）。"""
        with self._lock:
            c = self._db.cursor()
            dedup_key = entry.get("dedup_key") or f"{entry.get('source', '')}:{entry.get('task_id', '')}:{time.time()}"
            c.execute(
                f"INSERT OR IGNORE INTO {_t('obs_log')} "
                f"(dedup_key, ts, level, component, source, trace_id, task_id, "
                f"repo_key, runtime_id, msg, extra_json) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dedup_key,
                    entry.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S"),
                    entry.get("level", "INFO"),
                    entry.get("component", ""),
                    entry.get("source", ""),
                    entry.get("trace_id", ""),
                    entry.get("task_id", ""),
                    entry.get("repo_key", ""),
                    entry.get("runtime_id", 0),
                    entry.get("msg", ""),
                    json.dumps(entry.get("extra", {})) if entry.get("extra") else "",
                ),
            )
            self._db.commit()
            return {"ok": True}

    def log_llm_usage(self, usage: dict) -> Any:
        """Runtime 写 LLM 用量（obs_llm_usage）。"""
        with self._lock:
            c = self._db.cursor()
            dedup_key = usage.get("dedup_key") or f"{usage.get('task_id', '')}:{usage.get('seq', 0)}"
            c.execute(
                f"INSERT OR IGNORE INTO {_t('obs_llm_usage')} "
                f"(dedup_key, task_id, seq, turn, source, repo_key, scene_id, "
                f"agent_type, engine_id, user_id, provider_raw, model_raw, model_key, "
                f"run_id, run_kind, input_tokens, output_tokens, "
                f"cache_read_tokens, cache_write_tokens, reasoning_tokens) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dedup_key,
                    usage.get("task_id", ""),
                    usage.get("seq", 0),
                    usage.get("turn", 0),
                    usage.get("source", ""),
                    usage.get("repo_key", ""),
                    usage.get("scene_id", ""),
                    usage.get("agent_type", ""),
                    usage.get("engine_id", 0),
                    usage.get("user_id", 0),
                    usage.get("provider_raw", ""),
                    usage.get("model_raw", ""),
                    usage.get("model_key", ""),
                    usage.get("run_id", ""),
                    usage.get("run_kind", "task"),
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("cache_read_tokens", 0),
                    usage.get("cache_write_tokens", 0),
                    usage.get("reasoning_tokens", 0),
                ),
            )
            self._db.commit()
            return {"ok": True}

    def log_api_latency(self, entry: dict) -> Any:
        """A 前端延迟上报（api_latency_log）。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"INSERT INTO {_t('api_latency_log')} "
                f"(request_id, method, path, status, duration_ms, "
                f"repo_key, user_id, source) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.get("request_id", ""),
                    entry.get("method", ""),
                    entry.get("path", entry.get("endpoint", "")),
                    entry.get("status", entry.get("status_code", 0)),
                    entry.get("duration_ms", entry.get("latency_ms", 0)),
                    entry.get("repo_key", ""),
                    str(entry.get("user_id", "")),
                    entry.get("source", "frontend"),
                ),
            )
            self._db.commit()
            return {"ok": True}

    def query_metrics(self, repo_key: str, window: str = "1h") -> list:
        """A 指标聚合查询（obs_metric_rollup）。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT metric_name, metric_value, labels, ts "
                f"FROM {_t('obs_metric_rollup')} "
                f"WHERE repo_key=? ORDER BY ts DESC LIMIT 100",
                (repo_key,),
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def query_slo(self, repo_key: str) -> list:
        """A SLO 状态查询（obs_slo / obs_slo_snapshots）。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT * FROM {_t('obs_slo')} WHERE repo_key=?",
                (repo_key,),
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def query_alerts(self, repo_key: str) -> list:
        """A 告警查询（obs_alert_rules / obs_alert_events）。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT * FROM {_t('obs_alert_events')} "
                f"WHERE repo_key=? AND status='active' "
                f"ORDER BY created_at DESC",
                (repo_key,),
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def aggregate_llm_usage(self, engine_ids: list) -> list:
        from src.domains.insight.public import aggregate_llm_usage as _agg
        return [list(r) for r in _agg(list(engine_ids or []))]

    def aggregate_runtime_stats(self) -> dict:
        """聚合 runtime 数据（events/agent_tasks/agent_runtimes）。

        lib 模式：跨域读必须走 runtime 域 lib（insight 连接在自己库里，读不到 runtime 表）；
        共享模式：沿用跨 schema SQL（t_runtime 前缀）。
        """
        from src.domains.runtime import public as _rt
        if _rt.available():
            return _rt.aggregate_counts()
        with self._lock:
            c = self._db.cursor()
            stats: dict[str, Any] = {}
            # events 统计
            try:
                c.execute(
                    f"SELECT status, COUNT(*) as cnt "
                    f"FROM {t_runtime('events')} "
                    f"GROUP BY status"
                )
                stats["events_by_status"] = {
                    r[0]: r[1] for r in c.fetchall()
                }
            except Exception:
                stats["events_by_status"] = {}
            # agent_tasks 统计
            try:
                c.execute(
                    f"SELECT status, COUNT(*) as cnt "
                    f"FROM {t_runtime('agent_tasks')} "
                    f"GROUP BY status"
                )
                stats["tasks_by_status"] = {
                    r[0]: r[1] for r in c.fetchall()
                }
            except Exception:
                stats["tasks_by_status"] = {}
            # 引擎统计
            try:
                c.execute(
                    f"SELECT status, COUNT(*) as cnt "
                    f"FROM {t_runtime('agent_runtimes')} "
                    f"GROUP BY status"
                )
                stats["runtimes_by_status"] = {
                    r[0]: r[1] for r in c.fetchall()
                }
            except Exception:
                stats["runtimes_by_status"] = {}
            return stats
