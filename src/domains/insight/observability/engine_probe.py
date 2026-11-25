"""引擎健康探测：每 10 分钟对各 runtime 引擎发起 CLI/ACP 测试对话。

[P0b B5 探活合一] 探活分派单点化：本模块只保留循环策略（10 分钟防抖、
结果内存表、ops/overview 读取），探活知识全部在 descriptor
（probe.mode/impl/command）+ probe_dispatch 单点——worker 侧 10 分钟循环
走同一分派器，双表/双名字分支漂移（worker 缺 atomcode、insight 缺
dsh/cursor/hwcloud，互补缺口实证）自此不可能发生。

ACP 探活实现单源 = worker 侧 src.domains.runtime.engine.agent（同
aishell-dsh 单源先例）；insight 原自带 hermes 2 步浅版副本已删——统一
3 步全真（initialize + session/new + prompt，Issue #441 口径），insight
侧 hwcloud 探活缺口（原落命令表报 skip）随派发自然修复。
"""
import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("devops-bot")

# [B5] 探活命令表已消：分派器运行时按 descriptor 派生，本模块无表。

# [B5] 探活超时常量已随命令表移入 probe_dispatch（cli_run 值）；循环周期
# 属循环策略，留本模块。

from src.domains.runtime.engine import probe_dispatch as _probe_dispatch

_PROBE_INTERVAL = 600  # 10 分钟

# 探测结果：{engine_name: {"status": "ok"/"fail"/"pending", "latency": 秒, "last_probe": ISO, "error": str}}
_probe_results: dict = {}
_probe_lock = threading.Lock()
_probe_thread = None
_probe_started = False
_last_probe_time: float = 0  # 上次探测完成的时间戳（用于防抖）
_probing: bool = False  # 是否正在探测中（防止并发探测）


def _probe_engine(engine_name: str, cli_path: str) -> dict:
    """对单个引擎发起测试对话，返回探测结果（单点分派，无名字分支）。

    acp_handshake 引擎（hermes/hwcloud/aishell-dsh）按 probe.impl 派发到
    worker 侧 agent 实现；cli_run 引擎按 probe.command 跑测试对话——
    原双表/双名字分支已消，insight 侧 hwcloud 缺口随合一修复。
    """
    return _probe_dispatch.probe_deep(engine_name, cli_path)


def _probe_loop():
    """后台探测循环：每 10 分钟对所有本机引擎发起测试对话。"""
    global _last_probe_time, _probing
    while True:
        if _probing:
            time.sleep(_PROBE_INTERVAL)
            continue
        with _probe_lock:
            _probing = True
        try:
            from src.domains.runtime.public import discover_backends
            engines = discover_backends()
            for eng in engines:
                if not eng.get("installed"):
                    with _probe_lock:
                        _probe_results[eng["name"]] = {
                            "status": "skip", "latency": 0,
                            "last_probe": datetime.now(timezone.utc).isoformat(),
                            "error": "not installed",
                        }
                    continue
                log.info(f"engine probe: testing {eng['name']}...")
                result = _probe_engine(eng["name"], eng.get("cli_path", ""))
                result["last_probe"] = datetime.now(timezone.utc).isoformat()
                with _probe_lock:
                    _probe_results[eng["name"]] = result
                log.info(f"engine probe: {eng['name']} -> {result['status']} ({result['latency']}s)")
            _last_probe_time = time.time()
        except Exception as e:
            log.warning(f"engine probe loop error: {e}")
        finally:
            with _probe_lock:
                _probing = False
        time.sleep(_PROBE_INTERVAL)


def start_probe():
    """启动后台探测线程（在 lifespan 中调用）。"""
    global _probe_thread, _probe_started
    if _probe_started:
        return
    _probe_started = True
    _probe_thread = threading.Thread(target=_probe_loop, daemon=True, name="engine-probe")
    _probe_thread.start()
    log.info("Engine probe thread started (10min interval)")


def get_probe_results() -> dict:
    """返回所有引擎的探测结果（供 ops/overview 读取）。"""
    with _probe_lock:
        return dict(_probe_results)


def probe_now(engine_name: str = "") -> dict:
    """手动触发一次探测（可选：只探测指定引擎）。"""
    from src.domains.runtime.public import discover_backends
    engines = discover_backends()
    for eng in engines:
        if engine_name and eng["name"] != engine_name:
            continue
        if not eng.get("installed"):
            continue
        result = _probe_engine(eng["name"], eng.get("cli_path", ""))
        result["last_probe"] = datetime.now(timezone.utc).isoformat()
        with _probe_lock:
            _probe_results[eng["name"]] = result
        return result
    return {"status": "skip", "latency": 0, "error": "engine not found"}


def probe_all_now():
    """手动触发所有引擎探测（同步阻塞，各引擎串行探测）。"""
    global _last_probe_time, _probing
    if _probing:
        return  # 已有探测在进行，直接返回
    with _probe_lock:
        _probing = True
    try:
        from src.domains.runtime.public import discover_backends
        engines = discover_backends()
        for eng in engines:
            if not eng.get("installed"):
                with _probe_lock:
                    _probe_results[eng["name"]] = {
                        "status": "skip", "latency": 0,
                        "last_probe": datetime.now(timezone.utc).isoformat(),
                        "error": "not installed",
                    }
                continue
            log.info(f"engine probe (manual): testing {eng['name']}...")
            result = _probe_engine(eng["name"], eng.get("cli_path", ""))
            result["last_probe"] = datetime.now(timezone.utc).isoformat()
            with _probe_lock:
                _probe_results[eng["name"]] = result
            log.info(f"engine probe (manual): {eng['name']} -> {result['status']} ({result['latency']}s)")
        _last_probe_time = time.time()
    finally:
        with _probe_lock:
            _probing = False
