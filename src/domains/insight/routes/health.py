"""系统健康检查端点——免鉴权基本检查。

供 K8s livenessProbe / nginx 健康检查 / Docker healthcheck 使用。
不查数据库、不暴露系统规模。
"""
import logging

from fastapi import APIRouter

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/health")
def health():
    """免鉴权健康端点——只返回服务是否在线。

    server_base_url（2026-10-15）：CLI/worker 可达的服务端权威基址（meta.server_base_url，
    无则 server-config.json 兜底）。前端 devspace/设备接入用它替代 window.location.origin
    （用户浏览地址 ≠ CLI 可达地址，如生产 www.grapedev.cn vs worker.grapedev.cn）。
    仅供前端构造安装命令，不含敏感信息；查询失败返回空串（前端回退 origin）。
    """
    base = ""
    base_configured = True
    base_warning = ""
    browser_url = ""
    worker_url = ""
    business_url = ""
    split_warning = ""
    try:
        from src.domains.mgmt.public import (get_server_base_url, is_server_base_url_configured,
                                 get_server_base_url_warning,
                                 get_browser_callback_url, get_worker_connect_url,
                                 get_business_api_url, get_url_split_warning)
        base = get_server_base_url() or ""
        base_configured = is_server_base_url_configured()
        base_warning = get_server_base_url_warning() if not base_configured else ""
        # Issue #471 E1：拆分后双地址各取所需（未配新项回退 server_base_url）
        browser_url = get_browser_callback_url() or ""
        worker_url = get_worker_connect_url() or ""
        # Issue #1577：任务内业务 API 基址（未配回退 server_base_url）
        business_url = get_business_api_url() or ""
        split_warning = get_url_split_warning()
    except Exception:
        base = ""
        base_configured = False
        base_warning = "server_base_url 检查失败，请查看服务端日志"
        browser_url = ""
        worker_url = ""
        business_url = ""
        split_warning = ""

    # Issue #452: 暴露 agent_runtime 状态，缺失时显式标记 missing 而非静默通过
    # （三库收口：两条表的读取收进 runtime 域 public；本文件不留 SQL。原 lib 分支
    # 上的 `.fetchone()` 是对已 materialize 元组的二次调用，三库拓扑必 AttributeError
    # 并被吞成 unknown —— 本批随原语一起修掉。）
    agent_runtime_status = "unknown"
    try:
        from src.domains.runtime.public import agent_runtime_health_snapshot_any
        ready_row, runtime_total = agent_runtime_health_snapshot_any()
        if ready_row:
            agent_runtime_status = "online"
        else:
            # 有 runtime 记录但无 online+healthy 的
            agent_runtime_status = "missing" if runtime_total == 0 else "unhealthy"
    except Exception as _e:  # noqa: BLE001
        log.debug(f"agent runtime health snapshot failed: {_e}")

    # Issue #887：skill 一致性门禁聚合项（近 7 天 [skill_mismatch] 拒绝数，AC4）。
    # 免鉴权端点只暴露计数与状态，不含仓库/技能明细；查询失败显式 unknown。
    skill_consistency = {"status": "unknown", "refused_recent": 0, "detail": ""}
    try:
        from src.domains.runtime.public import aggregate_skill_consistency
        _sk = aggregate_skill_consistency()
        skill_consistency = {"status": _sk.get("status", "unknown"),
                             "refused_recent": int(_sk.get("refused_recent") or 0),
                             "detail": _sk.get("detail", "")}
    except Exception:
        skill_consistency["detail"] = "skill consistency aggregate check failed"

    # 托管镜像的引擎配置自证（2026-11-23）：entrypoint 把结果写成 <data dir>/engine-status.json，
    # 这里只读文件、不探测模型端点（免鉴权的 /api/health 不能因为探测而变慢或外呼）。
    # 目的是让部署者一条 curl 就能分辨「配好了没 / 错在 URL 还是 key」，而不是等到任务失败。
    engine = {
        "engine_configured": None,
        "engine_provider": "",
        # 免鉴权端点不回完整网关地址（可能含私有路径/租户前缀），只回主机名
        "engine_base_host": "",
        "engine_probe_ok": None,
        "engine_models": 0,
        "engine_default_model": "",
        "engine_error": "",
        "engine_checked_at": "",
    }
    try:
        import json as _json
        import os as _os
        _cands = [
            _os.environ.get("ENGINE_STATUS_FILE") or "",
            _os.path.join(_os.environ.get("DEVOPS_DATA_DIR") or "/app/data", "engine-status.json"),
        ]
        for _p in _cands:
            if _p and _os.path.exists(_p):
                with open(_p, encoding="utf-8") as _f:
                    _d = _json.load(_f) or {}
                engine["engine_configured"] = bool(_d.get("configured"))
                engine["engine_provider"] = str(_d.get("provider") or "")
                _raw_base = str(_d.get("base_url") or "")
                try:
                    from urllib.parse import urlsplit as _urlsplit
                    engine["engine_base_host"] = str(_urlsplit(_raw_base).hostname or "")
                except Exception:  # noqa: BLE001
                    engine["engine_base_host"] = ""
                engine["engine_probe_ok"] = _d.get("probe_ok")
                engine["engine_models"] = int(_d.get("models") or 0)
                engine["engine_default_model"] = str(_d.get("default_model") or "")
                engine["engine_error"] = str(_d.get("error") or "")
                engine["engine_checked_at"] = str(_d.get("checked_at") or "")
                break
    except Exception as _e:  # noqa: BLE001 —— 状态缺失不应影响健康检查本身
        log.debug(f"engine status read failed: {_e}")

    # Issue #455：base_configured/base_warning 供 OSS 用户自检——未配置对外基址时明确告警
    # （OAuth 回调指向 localhost 第三方平台无法回访）。字段新增向后兼容。
    return {
        "server": "ok",
        "server_base_url": base,
        "server_base_url_configured": base_configured,
        "server_base_url_warning": base_warning,
        # Issue #471 E1：浏览器回调 / worker 回连拆分地址（回退 server_base_url）
        "browser_callback_url": browser_url,
        "worker_connect_url": worker_url,
        "task_business_api_url": business_url,
        "url_split_warning": split_warning,
        "agent_runtime": agent_runtime_status,
        "skill_consistency": skill_consistency,
        # 托管镜像的引擎状态（非托管/未写过状态文件时为 None/空，向后兼容）
        **engine,
    }
