"""Grape Service Bus — 服务注册表（DB 驱动，零外部依赖）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.3.

每个域进程启动时注册自己到 gsb_service_registry 表，30s 心跳更新，
关闭时注销。其他域通过 discover_service() 发现服务地址。

注册表表名不走 TABLE_REGISTRY（GSB 基础设施表，不参与分 Schema 前缀）。

2026-11 补齐：新增 list_all_services()（运维全量视图，含离线/过期行）与
purge_stale()（把心跳超时的 online 行标为 offline），供 /internal/gsb/services
与巡检脚本使用；register/discover 行为保持设计稿语义不变。

2026-11 跨域收口：注册表 SQL 全部下沉 mgmt 域 public（`gsb_service_registry` 归
mgmt，见 configs/table_ownership.json）。本模块只保留 GSB 语义——心跳超时判定、
实例派生字段、负载均衡依赖的排序口径；不再直连进程库。三库拓扑下各域进程写的是
同一张 mgmt 表（改前各写各的库：注册不报错但互相发现不到），单库 / 测试拓扑由
owner 域的「任意拓扑」原语回落进程主库，行为与改前逐字一致。

访问路径：本模块不再延迟 import `mgmt.public`，而是经 kernel 端口 `pipeline` 调用这些
mgmt 原语（GSB 是服务总线，注册表就是总线的发现层，与事件管道同属路由基础设施；
同一端口已承载 mgmt.public 的其它基础设施原语）。符号名与调用点不变，符号解析由
装配层（src/api/contracts_wiring.py）在启动时绑定，kernel 侧不出现 domains import。
"""
from __future__ import annotations

import json
import time

from src.kernel.ports import pipeline as _pipeline

# 心跳超时：90s = 3 个心跳周期（30s × 3）
HEARTBEAT_TIMEOUT = 90


def _ensure_table() -> None:
    """幂等建表/补列（DDL 归 mgmt 域；每次 register 前调用，时机与原实现一致）。"""
    _pipeline.ensure_gsb_registry_schema()


def register_service(
    name: str,
    base_url: str,
    health_url: str = "/api/health",
    metadata: dict | None = None,
    version: str | None = None,
) -> None:
    """服务启动时注册自己。

    Args:
        name: 服务名（mgmt / event / runtime / insight / future-service）
        base_url: 服务基础 URL（如 http://127.0.0.1:38081）
        health_url: 健康检查路径（默认 /api/health）
        metadata: 额外元数据（role, port, pid, started_at, ...）
        version: 服务版本（写入 version 列，便于灰度期间比对）
    """
    _ensure_table()
    payload = json.dumps(metadata or {}, ensure_ascii=False)
    _pipeline.gsb_register(
        name, base_url, health_url, version or "1", time.time(), payload
    )


def heartbeat_service(name: str) -> None:
    """定时心跳更新（30s 调用一次）。"""
    _ensure_table()
    _pipeline.gsb_heartbeat(name, time.time())


def discover_service(name: str) -> dict:
    """发现服务地址——带健康检查（心跳超时视为离线）。

    Returns:
        服务行 dict（service_name, base_url, health_url, ...）或空 dict。
    """
    _ensure_table()
    data = _pipeline.gsb_discover_row(name)
    if not data:
        return {}
    # 心跳超时检查
    if time.time() - float(data.get("last_heartbeat", 0)) > HEARTBEAT_TIMEOUT:
        return {}
    return data


def list_services() -> list:
    """列出所有在线服务（心跳未超时）。"""
    _ensure_table()
    return _pipeline.gsb_online_services(time.time() - HEARTBEAT_TIMEOUT)


def split_key(key: str) -> tuple[str, str]:
    """把注册键拆成 (服务名, 命名空间)。

    ``mgmt@node-a`` → ``("mgmt", "node-a")``；裸 ``mgmt`` → ``("mgmt", "")``。
    命名空间为空表示未启用多机隔离（旧注册/单机模式）。
    """
    raw = (key or "").strip()
    if "@" not in raw:
        return raw, ""
    service, instance = raw.split("@", 1)
    return service, instance


def list_instances(service: str, include_offline: bool = False) -> list[dict]:
    """列出某个基础服务的全部实例（``service`` 与 ``service@namespace`` 两类键）。

    这是 GSB 负载均衡的数据基础：discover_service() 只返回单条（fetchone），
    多实例场景需要一次拿到全部候选。

    返回的每行在原字段之外补充：
    - ``service``：基础服务名（mgmt / runtime / ...）
    - ``instance``：命名空间（node-a / node-b / ...），裸键为空串
    - ``age_seconds``：距最近一次心跳的秒数
    - ``alive``：是否 online 且心跳未超时

    Args:
        service: 基础服务名，如 ``mgmt``
        include_offline: 为 True 时一并返回 status 非 online / 心跳过期的行
    """
    _ensure_table()
    cutoff = time.time() - HEARTBEAT_TIMEOUT
    rows = _pipeline.gsb_instances(service, cutoff, include_offline=include_offline)

    now = time.time()
    for r in rows:
        base, instance = split_key(r.get("service_name", ""))
        age = max(0.0, now - float(r.get("last_heartbeat") or 0))
        r["service"] = base
        r["instance"] = instance
        r["age_seconds"] = round(age, 1)
        r["alive"] = bool(r.get("status") == "online" and age <= HEARTBEAT_TIMEOUT)
    return rows


def list_all_services(include_offline: bool = True) -> list:
    """列出注册表全部行（运维视角，含离线与心跳过期行）。

    与 list_services() 的区别：不做心跳过滤，额外返回 age_seconds / alive
    两个派生字段，方便一眼看出"注册了但已经死掉"的服务。
    """
    _ensure_table()
    rows = _pipeline.gsb_all_services(include_offline=include_offline)

    now = time.time()
    for r in rows:
        age = max(0.0, now - float(r.get("last_heartbeat") or 0))
        r["age_seconds"] = round(age, 1)
        r["alive"] = bool(r.get("status") == "online" and age <= HEARTBEAT_TIMEOUT)
    return rows


def purge_stale(ttl: float = HEARTBEAT_TIMEOUT) -> int:
    """把心跳超时的 online 行标记为 offline，返回受影响行数。

    仅做状态收敛，不删除行——保留 base_url / metadata 便于事后排查。
    """
    _ensure_table()
    return _pipeline.gsb_purge_stale(time.time() - ttl)


def deregister_service(name: str) -> None:
    """服务关闭时注销。"""
    _ensure_table()
    _pipeline.gsb_mark_offline(name)