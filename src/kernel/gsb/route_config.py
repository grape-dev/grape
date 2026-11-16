"""Grape Service Bus — Provider 路由配置表（Issue #2479：remote/lib 按接口并存）.

设计依据：docs/issue-2479/plan.md §2.1；docs/lib-3db-3host-design.md §11（按方法路由）。

把 provider 选择从 ``GRAPE_PROVIDER_MODE`` 全局一刀切改为按两级 key 路由：

- 契约方法 key：``service.method``（如 ``mgmt.get_bot_config``，23 个）；
- public 原语 key：``domain.primitive``（如 ``mgmt.get_repo_field``，
  mgmt 39 个 + runtime 66 个数据原语；``available``/``health`` 探活口不参与路由）。

配置落 DB 表 ``gsb_provider_route``（与 ``gsb_service_registry`` 同库），
进程内按 version+TTL 缓存：改表后 ≤TTL（默认 30s，``GRAPE_GSB_ROUTE_TTL``）
不重启生效；**读取失败退回上一次已知配置**（启动期兜底，非调用期回落）。

kernel 层禁止 import domains（check-arch R1 HARD），因此路由 key 清单以字面量
镜像 contracts/public 实际面；``tests/kernel/test_gsb_route.py`` 的同步用例把两者
锁定——契约/原语增删不同步登记会在 CI 变红。
"""
from __future__ import annotations

import os
import threading
import time

from src.kernel import session_manager

# gsb_provider_route 归 mgmt 域：任一进程读路由配置都必须打到 owner 库，
# 否则三库模式下非 mgmt 进程会得到 1146（表不在本域库里）。
from src.kernel.owner_db import owner_db as _owner_db
_owner = _owner_db("mgmt")
_owner_lock = session_manager.db_lock
from src.kernel.config import log

# ---------------- 路由 key registry（静态镜像，同步测试防漂移） ----------------

#: 三域契约方法（来源：src/domains/contracts/{mgmt,runtime,insight}_service.py）
CONTRACT_METHODS: dict[str, tuple[str, ...]] = {
    "mgmt": (
        "get_repo_webhook_config", "get_agent_session", "get_scene_config",
        "get_scene_pool_engine_ids", "verify_session_token", "get_bot_config",
        "get_skill",
        # P2 服务接口补齐（N6/N8/N9/N10）
        "get_repo_bundle", "get_account", "get_knowledge", "get_user_mapping_batch",
    ),
    "runtime": (
        "get_events", "get_event_detail", "get_tasks", "get_task_messages",
        "get_runtimes", "get_issue_state", "update_issue_state",
        "get_chat_conversations", "get_chat_messages",
        # P2 服务接口补齐（N1–N5/N11/N12）
        "list_skills", "get_skill_detail", "list_engine_capabilities",
        "get_runtime_credential_meta", "get_meta_namespace", "get_tasks_batch",
        "get_task_stats",
        # 本轮新增（mgmt → runtime 跨域读迁移）
        "get_issue_card", "get_credential_health_rows", "get_inflight_task_count",
        "get_agent_session_row", "get_event_status_counts", "search_events",
        "list_credential_rows", "list_engine_tags",
    ),
    "insight": (
        "log_obs", "log_llm_usage", "log_api_latency", "query_metrics",
        "query_slo", "query_alerts", "aggregate_runtime_stats",
        # 本轮新增（mgmt → insight 跨域读迁移）
        "aggregate_llm_usage",
    ),
}

#: 跨域 public 数据原语（来源：src/domains/{mgmt,runtime}/public/__init__.py；
#: available/health 为探活口，不参与路由）。insight 无 public 模块。
PUBLIC_PRIMITIVES: dict[str, tuple[str, ...]] = {
    "mgmt": (
        "fetch_rows", "fetch_one", "get_repo_field", "set_repo_fields",
        "ensure_repo_row", "get_repo_org_id", "list_user_ids_by_open_id",
        "get_feishu_open_id", "list_all_user_ids", "list_baseline_knowledge_files",
        "list_knowledge_obs_keys", "get_autopilot_task_brief", "mark_default_featured",
        "set_scene_deploy_url", "set_scene_nodes", "upsert_user_mapping_feishu",
        "clear_user_mapping_feishu", "set_user_mapping_nickname",
        "delete_feishu_onboard_task", "mark_external_identity_expired",
        "set_autopilot_run_info", "set_autopilot_engine_ids",
        "upsert_baseline_knowledge", "ensure_scene_knowledge_ref",
        "delete_event_knowledge", "replace_scene_skill_ref", "upsert_scene_content",
        "set_scene_icon", "get_official_scene_id", "count_rows",
        "count_official_scenes", "list_enabled_autopilot_tasks",
        "get_account_id_by_username", "get_account_username_by_id",
        "find_repo_key_in", "list_alert_user_ids", "get_user_id_by_mapping_column",
        # P2 服务接口补齐（N6/N8/N9/N10）
        "repo_bundle", "get_account_public", "get_knowledge_entry", "user_mapping_batch",
        # W2 渠道写原语（runtime IM → mgmt 归属表）
        "acquire_channel_lease", "renew_channel_lease", "release_channel_lease",
        # 渠道租约只读（runtime/im/outbox 归属过滤；任意拓扑）
        "lease_owner_by_key", "lease_owned_installations_by_owner",
        "release_all_leases", "claim_inbound_dedup", "upsert_channel_installation",
        "upsert_chat_session_binding", "upsert_sender_identity",
        "add_event_action_filter", "update_event_action_filter", "delete_event_action_filter",
        # 事件动作过滤器启用规则读口（api/event_rule.py L1 准入判定；任意拓扑）
        "event_action_filter_enabled_rows",
        # 流水线责任人展示（runtime/routes/pipeline.py；任意拓扑）
        "feishu_name_by_platform_column", "repo_bot_usernames_any",
        # VOD Bot 导航：仓库轨安装反查（runtime/im/vod_handler_async.py；任意拓扑）
        "installation_repo_app_ids_any", "installation_repo_keys_by_app_any",
        # IM 通知扇出目标解析（runtime/im/notify.py；任意拓扑）
        "installation_user_active_rows_any", "installation_p2p_chat_ids_any",
        # 互动卡片台账（runtime/im/engine/outbound_cards.py；任意拓扑）
        "outbound_card_put_any", "outbound_card_chat_row_any",
        "add_user_feedback", "save_inbound_message", "upsert_outbound_card",
        "read_one", "read_all",
    ),
    "runtime": (
        "list_runtimes", "list_runtimes_rows", "get_runtime", "task_stats",
        "append_task_message", "list_credential_rows", "latest_task_for_event",
        "task_brief", "get_skill_by_id", "get_skill_content_hash", "count_skills",
        "aggregate_counts", "list_repo_credentials", "has_engine_capability",
        "list_engine_capabilities", "list_engine_tags", "query_events",
        "group_events", "count_events", "get_autopilot_execution",
        "upsert_engine_capability", "update_engine_capability",
        "delete_engine_capability", "acquire_channel_lease", "release_channel_lease",
        "heartbeat_instance", "list_active_instances", "release_all_leases",
        "renew_channel_lease", "purge_user_rows", "delete_chat_conversations",
        "delete_vod_conversations", "detach_agent_sessions",
        "clear_agent_session_runtime", "ensure_agent_session", "stamp_task_traces",
        "mark_detach_rebound", "delete_autopilot_executions",
        "set_autopilot_findings", "set_repo_im_notifications",
        "add_event_action_filter", "update_event_action_filter",
        "delete_event_action_filter", "update_skill_content", "update_skill_obs",
        "insert_skill", "create_skill_row", "delete_skill_row", "delete_skill_by_id",
        "adjust_skill_like_count", "insert_runtime_credential",
        "rename_runtime_credential", "set_runtime_credential_scope",
        "delete_runtime_credential", "upsert_repo_credential", "clear_repo_credential",
        "ensure_device_credential", "touch_credential_metrics",
        # 本机指标上报任意拓扑版（api/server_tasks.py insight 进程 15s 心跳）
        "device_credential_id_any", "register_device_credential_any",
        "touch_device_credential_any",
        # 流水线业务状态 / 阶段任务引用（api/pipeline_status.py 任意拓扑读口）
        "issue_business_stage_status_any", "issue_state_stage_any",
        "segment_task_rows_any",
        "upsert_local_runtimes", "ensure_default_runtime", "set_agent_tags",
        "purge_runtime_credential", "change_credential_scope",
        "reassign_repo_org_id",
        # P2 服务接口补齐（N1/N2/N3/N4/N5/N11/N12）
        "list_skills", "get_skill_detail", "list_all_engine_capabilities",
        "get_runtime_credential_meta", "get_meta_namespace", "tasks_brief_many",
        "task_stats_extended", "xwrite_apply",
        # W2 写原语
        "upsert_event_active_guard", "upsert_issue_agent_session", "upsert_scene_category",
        "record_event_skipped",
        "list_user_conversations", "get_scene_category", "list_scene_categories",
        "list_featured_skills", "list_active_credential_ids", "list_runtime_credentials",
        "list_autopilot_executions", "get_meta", "set_meta_value",
        "read_one", "read_all",
    ),
    "insight": (
        # W3 派生写（指标/日志落库）
        "record_api_latency", "record_merge_gate_log",
    ),
}

_MODES = ("lib", "remote")

# ---------------- DDL（与 gsb_service_registry 同库；不走 TABLE_REGISTRY） ----------------

ROUTE_DDL = """
CREATE TABLE IF NOT EXISTS gsb_provider_route (
    route_key  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    timeout_ms INTEGER DEFAULT 1000,
    version    INTEGER DEFAULT 1,
    updated_at REAL DEFAULT 0,
    updated_by TEXT DEFAULT ''
)
"""

# ---------------- 进程内缓存（version + TTL 单飞刷新） ----------------

_cache_lock = threading.Lock()
_cache: dict[str, dict] = {}
_cache_version: int = 0
_cache_at: float = 0.0

_TTL_ENV = "GRAPE_GSB_ROUTE_TTL"
_TTL_DEFAULT = 30.0


def _ttl() -> float:
    """缓存 TTL 秒数（默认 30s；非法值回退默认并告警一次）。"""
    raw = (os.environ.get(_TTL_ENV) or "").strip()
    if not raw:
        return _TTL_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        log.warning(f"invalid {_TTL_ENV}={raw!r}, fallback to {_TTL_DEFAULT}")
        return _TTL_DEFAULT
    return value if value >= 0 else _TTL_DEFAULT


def reset_cache() -> None:
    """清空路由缓存（测试 / 配置写入后本进程立即生效用）。"""
    global _cache, _cache_version, _cache_at
    with _cache_lock:
        _cache = {}
        _cache_version = 0
        _cache_at = 0.0


def cache_state() -> dict:
    """缓存观测口（运维/测试）：快照大小、最大 version、加载时间。"""
    with _cache_lock:
        return {
            "routes": len(_cache),
            "version": _cache_version,
            "loaded_at": _cache_at,
            "age_seconds": round(max(0.0, time.time() - _cache_at), 1) if _cache_at else 0.0,
            "ttl": _ttl(),
        }


def ensure_table() -> None:
    """幂等建表（运行时惰性调用；迁移脚本同款 DDL）。"""
    with _owner_lock:
        c = _owner.cursor()
        c.execute(ROUTE_DDL)
        _owner.commit()


def _load_all() -> list[dict]:
    """全表读出为 dict 列表（SQLite Row / MySQL DictCursor 双兼容）。

    首次读前幂等建表：mgmt 域库由 provisioning 建，其它拓扑（单库/新环境）
    可能缺表，缺表时 snapshot() 只会退化成内置基线（表现是路由配置不生效）。
    """
    try:
        ensure_table()
    except Exception as _e:  # noqa: BLE001 - 建表失败不阻断读（读失败仍会退回基线）
        log.debug(f"gsb route config ensure_table skipped: {_e}")
    with _owner_lock:
        c = _owner.cursor()
        c.execute(
            "SELECT route_key, mode, timeout_ms, version, updated_at, updated_by "
            "FROM gsb_provider_route"
        )
        rows = c.fetchall()
    if not rows:
        return []
    if hasattr(rows[0], "keys"):
        return [dict(r) for r in rows]
    cols = [d[0] for d in c.description] if c.description else []
    return [dict(zip(cols, r)) for r in rows] if cols else []


def snapshot() -> dict[str, dict]:
    """取路由快照：到期才刷新（单飞），失败退回上次已知配置。

    - TTL 内直接回缓存（热路径 O(1)，无 env/DB 开销之外的开销）；
    - 刷新失败（表不存在/DB 抖动）：保留旧快照并把计时后移一个 TTL，
      避免每次调用都撞库；启动期首刷失败 → 空快照 → 调用方走内置基线。
    """
    global _cache, _cache_version, _cache_at
    now = time.time()
    with _cache_lock:
        if _cache_at and now - _cache_at < _ttl():
            return _cache
    try:
        rows = _load_all()
    except Exception as exc:  # noqa: BLE001 - 配置读失败退回上次已知配置
        log.warning(f"gsb route config load failed, keep last known: {exc}")
        with _cache_lock:
            _cache_at = now
        return _cache
    with _cache_lock:
        _cache = {r["route_key"]: r for r in rows}
        _cache_version = max((int(r.get("version") or 0) for r in rows), default=0)
        _cache_at = now
    return _cache


def get_cached_route(route_key: str) -> dict | None:
    """查单条路由配置（未配置返回 None，调用方按内置基线取值）。"""
    return snapshot().get(route_key)


def get_cached_timeout_ms(route_key: str) -> int | None:
    """路由配置的 per-method remote 超时（ms）。

    未配置/未填 → None（调用方维持客户端默认超时；显式配置才收紧/放宽，
    建议：热查询 1s、管理类查询 ≤3s，见 docs/issue-2479/spec.md）。
    """
    row = get_cached_route(route_key)
    if not row:
        return None
    try:
        value = row.get("timeout_ms")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------- 写接口（改表即热切换：其它进程 ≤TTL 生效，本进程立即生效） ----------------


def set_route(route_key: str, mode: str, timeout_ms: int | None = None,
              updated_by: str = "") -> None:
    """插入/更新一行路由配置（version 自增）。

    Args:
        route_key: ``service.method`` 或 ``domain.primitive``
        mode: ``lib`` / ``remote``（其它值 ValueError，不静默落库）
        timeout_ms: 该方法 remote 调用超时（缺省不修改既有值/默认 1000）
        updated_by: 变更人（审计）
    """
    key = (route_key or "").strip()
    if not key:
        raise ValueError("route_key must not be empty")
    value = (mode or "").strip().lower()
    if value not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")
    if timeout_ms is not None:
        timeout_ms = int(timeout_ms)
        if timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be positive, got {timeout_ms}")
    ensure_table()
    with _owner_lock:
        c = _owner.cursor()
        c.execute("SELECT version, timeout_ms FROM gsb_provider_route WHERE route_key=?", (key,))
        row = c.fetchone()
        next_version = (int(row[0]) + 1) if row else 1
        # 未显式给 timeout_ms：沿用既有行值；新行落 NULL（= 客户端默认超时，
        # 不因配置 mode 而意外改变既有 remote 时延语义）
        effective_timeout = timeout_ms if timeout_ms is not None else (
            row[1] if row else None
        )
        c.execute(
            "INSERT OR REPLACE INTO gsb_provider_route "
            "(route_key, mode, timeout_ms, version, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, value, effective_timeout, next_version, time.time(), updated_by),
        )
        _owner.commit()
    reset_cache()


def remove_route(route_key: str) -> bool:
    """删除一行配置（回落内置基线）。返回是否确实删除了行。"""
    ensure_table()
    with _owner_lock:
        c = _owner.cursor()
        c.execute("DELETE FROM gsb_provider_route WHERE route_key=?", (route_key,))
        removed = bool(c.rowcount)
        _owner.commit()
    reset_cache()
    return removed


def list_routes() -> list[dict]:
    """列出全部路由配置行（运维视角，绕过缓存直读）。"""
    return _load_all()


def known_route_keys() -> list[str]:
    """全部已知路由 key（契约方法 + public 数据原语），一键全量的目标集。

    去重：同一能力可能既是契约方法（HTTP）又是 public 原语（lib），
    两者的路由 key 形如 `<service>.<method>` / `<domain>.<prim>`，可能同名
    （如 runtime.list_skills）——对配置表而言是**同一个 key**，只登记一次。
    """
    keys: list[str] = []
    for service, methods in CONTRACT_METHODS.items():
        keys.extend(f"{service}.{m}" for m in methods)
    for domain, prims in PUBLIC_PRIMITIVES.items():
        keys.extend(f"{domain}.{p}" for p in prims)
    return list(dict.fromkeys(keys))


def set_all_known(mode: str, updated_by: str = "") -> int:
    """一键全量登记：把所有已知 key 配成同一 mode（回滚 = 全量 lib）。

    Returns:
        登记行数（23 契约方法 + mgmt/runtime public 原语）。
    """
    for key in known_route_keys():
        set_route(key, mode, updated_by=updated_by)
    return len(known_route_keys())
