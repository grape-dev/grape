"""RuntimeAgent 边端入口（python -m runtime.agent，仅外连）。

见 agent-runtime-design.md §7.4 / tech-impl.md §1.4 / skill-delivery-remote.md §3.5。
流程：discover_runtimes → register → recover_orphans → 循环 claim（带 jitter）→ 执行 → 回报。
"""
import sys

# 版本守卫：代码使用 Python 3.10+ 联合类型语法（X | None），<3.10 会在 import 期崩溃。
# 放最顶部（任何 import 之前），给清晰报错而非 traceback。
if sys.version_info < (3, 10):
    print(f"RuntimeAgent requires Python >= 3.10 (got {sys.version.split()[0]}). "
          f"请用 python3.12 重建 venv: python3.12 -m venv .venv && .venv/bin/pip install -r requirements-worker.txt",
          file=sys.stderr)
    sys.exit(1)

import asyncio, json, os, random, re, time
import base64 as _b64
import zipfile as _zf
import urllib.request as _urllib
import shutil as _shutil

# 方案 G: codearts per-task 权限隔离预写配置
_CODEARTS_ALLOW_PERMS = [
    {"permission": "browser", "pattern": "*", "action": "ask"},
    {"permission": "edit", "pattern": "*", "action": "allow"},
    {"permission": "write", "pattern": "*", "action": "allow"},
    {"permission": "webfetch", "pattern": "*", "action": "ask"},
    {"permission": "websearch", "pattern": "*", "action": "allow"},
    {"permission": "external_directory_write", "pattern": "*", "action": "allow"},
    {"permission": "external_directory_read", "pattern": "*", "action": "allow"},
    {"permission": "dotfile", "pattern": "*", "action": "ask"},
]

from src.config import log, CHAT_CWD_BASE
from src.runtime.client import CredentialRevokedError
# R11.4: _get_task_token 抽取到 src/user/db.py，统一调用。
# （2026-10-21 恢复：fbc499c4a 曾把调用点换成裸 task.get("repo_gitcode_token")，
#   丢失路径 B 托管仓库的实时获取+自动刷新回退，6/29 近期任务 token 为空会受影响。）
from src.user.db import get_task_token as _get_task_token

# 凭据吊销信号（全局）：_wrap 独立 task 中感知 401 时置位，主循环检查后退出进程。
# create_task 的异常不穿透主循环，用 Event 跨 task 传递退出信号。
_credential_revoked = asyncio.Event()

# Worker 日志内存缓冲区——收集最近 ERROR/WARNING 日志，心跳时上报给主控
import collections as _collections
import time as _time_mod
import threading as _threading
import subprocess as _subprocess
import datetime as _datetime


# ===== 引擎探测（worker 端，每 10 分钟对本机引擎发起测试对话）=====

_PROBE_COMMANDS = {
    "opencode": ["opencode", "run", "hello"],
    "codearts": ["codearts", "run", "hello"],
    "codex": ["codex", "exec", "--json", "--sandbox", "danger-full-access", "--skip-git-repo-check", "hello"],
    "cursor": ["cursor", "run", "hello"],
}
_PROBE_TIMEOUT = 30
_ACP_PROBE_TIMEOUT = 15  # hermes ACP initialize 超时
_ACP_PROMPT_TIMEOUT = 30  # hermes ACP prompt 超时（模型推理需要更长时间）
_PROBE_INTERVAL = int(os.environ.get("RUNTIME_PROBE_INTERVAL", "600"))  # 引擎探测间隔，默认 600s（10 分钟），可通过 runtime.env 配置

# Issue #378: 按 agent_type 差异化静默超时阈值（coding/qa 放宽到 1200s，其余默认 300s）。
# 在 _execute_task 中以 setdefault 语义注入 env，底层各 backend 读 env 生效：
#   - executor.py       _silent_window  读 AGENT_SILENT_WINDOW
#   - hermes_acp.py     _SILENT_TIMEOUT 读 HERMES_SILENT_TIMEOUT
#   - hwcloud_acp.py    _SILENT_TIMEOUT 读 RUNTIME_TOOL_SILENT_TIMEOUT（已支持）
# coding/QA 长任务（编译/构建/PR 审查）常长时间无流式输出，300s 会在正常进行中误杀。
_AGENT_SILENT_TIMEOUTS = {"coding": 1200, "qa": 1200}
_DEFAULT_SILENT_WINDOW = 300

_worker_url_probe_cache: dict = {}  # {(rts, task_server_url): (ts, reachable)}
_worker_url_probe_lock = _threading.Lock()
_worker_probe_results: dict = {}  # {engine_name: {status, latency, last_probe, error}}
_worker_probe_lock = _threading.Lock()
_worker_probe_thread = None
_worker_probe_started = False


def _probe_hermes_acp(cli_path: str) -> dict:
    """hermes ACP 模式探活：initialize + 轻量 prompt，端到端验证引擎可用性。

    1. 启动 hermes acp 子进程（和实际任务执行路径一致）
    2. 发 JSON-RPC initialize → 验证 ACP 协议层
    3. 发 session/prompt "hi" → 验证 provider 连接 + 模型推理
    收到 stopReason 响应即表示端到端可用。
    """
    import json as _json
    import shutil as _shutil

    agent_cmd = cli_path or _shutil.which("hermes") or _shutil.which("hermes.cmd")
    if not agent_cmd:
        return {"status": "fail", "latency": 0, "error": "hermes CLI not found"}

    start = _time_mod.time()
    proc = None
    try:
        # 二进制模式（同 _probe_hwcloud_acp 修复——TextIOWrapper 解码缓冲
        # 会吞掉 select 之后的字节；hermes 暂未触发仅因无大块多字节通知，
        # 属潜伏同款 bug，一并修复）。
        proc = _subprocess.Popen(
            [agent_cmd, "acp"],
            stdin=_subprocess.PIPE,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )

        import select as _sel

        def _send(msg: dict):
            proc.stdin.write((_json.dumps(msg) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def _read_response(req_id: int, timeout: float) -> dict | None:
            """读 JSON-RPC 行直到匹配到指定 id 的 response。"""
            deadline = _time_mod.time() + timeout
            for _ in range(10):  # 最多读 10 行（跳过 notification）
                _remaining = max(deadline - _time_mod.time(), 0.1)
                _ready, _, _ = _sel.select([proc.stdout], [], [], _remaining)
                if not _ready:
                    return None
                _line = proc.stdout.readline()
                if not _line:
                    return None
                _line = _line.strip().decode("utf-8", errors="replace")
                if not _line.startswith("{"):
                    continue
                try:
                    _msg = _json.loads(_line)
                except _json.JSONDecodeError:
                    continue
                # 跳过 notification（无 id）和 agent request（有 method）
                if "id" not in _msg or "method" in _msg:
                    # agent request（如 session/request_permission）→ 自动批准
                    if "method" in _msg and "id" in _msg:
                        _send({"jsonrpc": "2.0", "id": _msg["id"],
                               "result": {"optionId": ""}})
                    continue
                if _msg.get("id") == req_id and ("result" in _msg or "error" in _msg):
                    return _msg
            return None

        # 1. initialize
        _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": 1,
                          "clientInfo": {"name": "grape-probe", "version": "1.0"},
                          "capabilities": {}}})
        _init_resp = _read_response(1, _ACP_PROBE_TIMEOUT)
        if not _init_resp:
            latency = round(_time_mod.time() - start, 1)
            _stderr = ""
            try:
                proc.terminate()
                _stderr = (proc.stderr.read() or "")[:200]
            except Exception:
                pass
            return {"status": "fail", "latency": latency,
                    "error": f"ACP initialize no response: {_stderr}"}
        if "error" in _init_resp:
            latency = round(_time_mod.time() - start, 1)
            _err = _init_resp["error"].get("message", str(_init_resp["error"]))[:200]
            return {"status": "fail", "latency": latency,
                    "error": f"ACP initialize error: {_err}"}

        # 2. session/prompt "hi" — 端到端验证 provider + 模型推理
        _send({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
               "params": {"sessionId": "",
                          "prompt": [{"type": "text", "text": "hi"}]}})
        _prompt_resp = _read_response(2, _ACP_PROMPT_TIMEOUT)
        if not _prompt_resp:
            latency = round(_time_mod.time() - start, 1)
            _stderr = ""
            try:
                proc.terminate()
                _stderr = (proc.stderr.read() or "")[:200]
            except Exception:
                pass
            return {"status": "fail", "latency": latency,
                    "error": f"ACP prompt no response (provider/model unreachable?): {_stderr}"}
        if "error" in _prompt_resp:
            latency = round(_time_mod.time() - start, 1)
            _err = _prompt_resp["error"].get("message", str(_prompt_resp["error"]))[:200]
            return {"status": "fail", "latency": latency,
                    "error": f"ACP prompt error: {_err}"}

        # prompt 成功（收到 stopReason）→ 端到端可用
        latency = round(_time_mod.time() - start, 1)
        _stop = (_prompt_resp.get("result") or {}).get("stopReason", "unknown")
        return {"status": "ok", "latency": latency, "error": "",
                "output": f"ACP initialize+prompt ok (stopReason={_stop})"}

    except _subprocess.TimeoutExpired:
        latency = round(_time_mod.time() - start, 1)
        return {"status": "fail", "latency": latency, "error": f"timeout ({_ACP_PROBE_TIMEOUT + _ACP_PROMPT_TIMEOUT}s)"}
    except FileNotFoundError:
        return {"status": "fail", "latency": 0, "error": "hermes CLI not found"}
    except Exception as e:
        latency = round(_time_mod.time() - start, 1)
        return {"status": "fail", "latency": latency, "error": str(e)[:200]}
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass



def _probe_hwcloud_acp(cli_path: str) -> dict:
    """hwcloud ACP 模式探活：initialize + session/new + 轻量 prompt。

    与 hermes 探活同构，差异（docs/hwcloud-runtime-acp-design.md 实测行为）：
    - 启动命令 （非  子命令）
    - prompt 前必须 session/new（对空/不存在 sessionId 的 prompt 报 not found）
    - mcpServers 必须传 []（传 {} 报 -32602）
    """
    import json as _json
    import shutil as _shutil

    agent_cmd = cli_path or _shutil.which("hwcloud") or _shutil.which("hwcloud.cmd")
    if not agent_cmd:
        return {"status": "fail", "latency": 0, "error": "hwcloud CLI not found"}

    start = _time_mod.time()
    proc = None
    try:
        # 二进制模式（无 text=True）：TextIOWrapper 的内部解码缓冲会"吞掉"
        # readline 之后仍在 Python 缓冲区里的字节（hwcloud session/new 发 18KB
        # available_skills_update 含多字节 UTF-8 → ③ mcp_servers_update 与
        # ④ response 被 TextIOWrapper 预读进缓冲区）——select 只查内核管道，
        # 查不到 Python 缓冲区 → 永远 not-ready → 15s 超时假阴性。
        # 设备侧对照实测：text=True 0/5 OK，二进制模式 5/5 OK（2026-10-13）。
        # 此前"O_NONBLOCK 与文本缓冲冲突"的结论是同源 bug 的另一种表现。
        proc = _subprocess.Popen(
            [agent_cmd, "serve", "--acp"],
            stdin=_subprocess.PIPE,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )

        import select as _sel

        def _send(msg: dict):
            proc.stdin.write((_json.dumps(msg) + "\n").encode("utf-8"))
            proc.stdin.flush()

        # select + readline 轮询；二进制模式下 readline 返回 bytes。
        _fd = proc.stdout.fileno()
        _probe_buf = [""]

        def _read_response(req_id: int, timeout: float) -> dict | None:
            deadline = _time_mod.time() + timeout
            while _time_mod.time() < deadline:
                _ready, _, _ = _sel.select([_fd], [], [], 0.3)
                if not _ready:
                    if proc.poll() is not None:
                        return None
                    continue
                _line = proc.stdout.readline()
                if not _line:
                    return None
                _line = _line.strip().decode("utf-8", errors="replace")
                if not _line.startswith("{"):
                    continue
                try:
                    _msg = _json.loads(_line)
                except _json.JSONDecodeError:
                    continue
                if "id" not in _msg or "method" in _msg:
                    if "method" in _msg and "id" in _msg:
                        # 标准 ACP outcome 结构（openagent server.go 3237 处理 union 判别；
                        # 旧 {"optionId": ""} 走 legacy 路径且空值 fail-closed）。
                        # 探活不需要执行工具 → 明确 cancelled。
                        _send({"jsonrpc": "2.0", "id": _msg["id"],
                               "result": {"outcome": {"outcome": "cancelled"}}})
                    continue
                if _msg.get("id") == req_id and ("result" in _msg or "error" in _msg):
                    return _msg
            return None

        # 1. initialize
        _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": 1,
                          "clientInfo": {"name": "grape-probe", "version": "1.0"},
                          "capabilities": {}}})
        _init_resp = _read_response(1, _ACP_PROBE_TIMEOUT)
        if not _init_resp or "error" in _init_resp:
            latency = round(_time_mod.time() - start, 1)
            _err = "no response" if not _init_resp else _init_resp["error"].get("message", "")[:200]
            return {"status": "fail", "latency": latency, "error": f"ACP initialize: {_err}"}

        # 2. session/new（hwcloud 必须先建会话；mcpServers 必须空数组）
        # cwd 用轻量临时目录：家目录在 devspace/网络挂载场景可能不可写或初始化慢
        # （session/new 的 process manager + 持久化依赖 cwd 可用），探活不应受其拖累
        import tempfile as _tempfile
        _probe_cwd = _tempfile.mkdtemp(prefix="hwcloud-probe-")
        _send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
               "params": {"cwd": _probe_cwd, "mcpServers": []}})
        _new_resp = _read_response(2, _ACP_PROBE_TIMEOUT)
        if not _new_resp or "error" in _new_resp:
            latency = round(_time_mod.time() - start, 1)
            _err = "no response" if not _new_resp else _new_resp["error"].get("message", "")[:200]
            return {"status": "fail", "latency": latency, "error": f"ACP session/new: {_err}"}
        _sid = (_new_resp.get("result") or {}).get("sessionId", "")

        # 3. session/prompt "hi" — 端到端验证 provider + 模型推理
        _send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
               "params": {"sessionId": _sid,
                          "prompt": [{"type": "text", "text": "hi"}]}})
        _prompt_resp = _read_response(3, _ACP_PROMPT_TIMEOUT)
        if not _prompt_resp:
            latency = round(_time_mod.time() - start, 1)
            return {"status": "fail", "latency": latency,
                    "error": f"ACP prompt no response (provider/model unreachable?)"}
        if "error" in _prompt_resp:
            latency = round(_time_mod.time() - start, 1)
            _err = _prompt_resp["error"].get("message", str(_prompt_resp["error"]))[:200]
            return {"status": "fail", "latency": latency, "error": f"ACP prompt error: {_err}"}

        latency = round(_time_mod.time() - start, 1)
        _stop = (_prompt_resp.get("result") or {}).get("stopReason", "unknown")
        return {"status": "ok", "latency": latency, "error": "",
                "output": f"ACP initialize+session/new+prompt ok (stopReason={_stop})"}

    except FileNotFoundError:
        return {"status": "fail", "latency": 0, "error": "hwcloud CLI not found"}
    except Exception as e:
        latency = round(_time_mod.time() - start, 1)
        return {"status": "fail", "latency": latency, "error": str(e)[:200]}
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass



def _probe_engine_worker(engine_name: str, cli_path: str) -> dict:
    """对本机引擎发起测试对话，返回探测结果。

    hermes 使用 ACP 模式探活（和实际任务执行路径一致）；
    其他引擎使用 CLI 测试对话。
    """
    # hermes 使用 ACP 模式探活
    if engine_name == "hermes":
        return _probe_hermes_acp(cli_path)
    # hwcloud 使用 ACP 模式探活（与实际任务执行路径一致：hwcloud serve --acp）
    if engine_name == "hwcloud":
        return _probe_hwcloud_acp(cli_path)


    cmd = _PROBE_COMMANDS.get(engine_name)
    if not cmd:
        return {"status": "skip", "latency": 0, "error": f"unknown engine: {engine_name}"}
    if cli_path:
        cmd = [cli_path] + cmd[1:]
    start = _time_mod.time()
    try:
        result = _subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT, stdin=_subprocess.DEVNULL,
        )
        latency = round(_time_mod.time() - start, 1)
        if result.returncode == 0:
            return {"status": "ok", "latency": latency, "error": ""}
        else:
            err = (result.stderr or result.stdout or "").strip()[:200]
            return {"status": "fail", "latency": latency, "error": f"exit={result.returncode}: {err}"}
    except _subprocess.TimeoutExpired:
        latency = round(_time_mod.time() - start, 1)
        return {"status": "fail", "latency": latency, "error": f"timeout ({_PROBE_TIMEOUT}s)"}
    except FileNotFoundError:
        return {"status": "fail", "latency": 0, "error": "CLI not found"}
    except Exception as e:
        latency = round(_time_mod.time() - start, 1)
        return {"status": "fail", "latency": latency, "error": str(e)[:200]}


def _worker_probe_once():
    """同步执行一轮引擎探测，结果写入 _worker_probe_results。

    注册成功后立即调用，确保第一次心跳上报时 probe_results 非空，
    使服务端能及时纠正 healthy（ok→1 / fail→0）。
    """
    try:
        engines = _discover_local_runtimes()
        for eng in engines:
            if not eng.get("installed"):
                with _worker_probe_lock:
                    _worker_probe_results[eng["name"]] = {
                        "status": "skip", "latency": 0,
                        "last_probe": _datetime.datetime.now(_datetime.timezone.utc).isoformat() + "Z",
                        "error": "not installed",
                    }
                continue
            log.info(f"worker engine probe: testing {eng['name']}...")
            result = _probe_engine_worker(eng["name"], eng.get("cli_path", ""))
            result["last_probe"] = _datetime.datetime.now(_datetime.timezone.utc).isoformat() + "Z"
            with _worker_probe_lock:
                _worker_probe_results[eng["name"]] = result
            log.info(f"worker engine probe: {eng['name']} -> {result['status']} ({result['latency']}s)")
    except Exception as e:
        log.warning(f"worker engine probe (initial) error: {e}")


def _worker_probe_loop():
    """后台探测循环：每 10 分钟对所有本机引擎发起测试对话。"""
    global _worker_probe_started
    while True:
        try:
            engines = _discover_local_runtimes()
            for eng in engines:
                if not eng.get("installed"):
                    with _worker_probe_lock:
                        _worker_probe_results[eng["name"]] = {
                            "status": "skip", "latency": 0,
                            "last_probe": _datetime.datetime.now(_datetime.timezone.utc).isoformat() + "Z",
                            "error": "not installed",
                        }
                    continue
                log.info(f"worker engine probe: testing {eng['name']}...")
                result = _probe_engine_worker(eng["name"], eng.get("cli_path", ""))
                result["last_probe"] = _datetime.datetime.now(_datetime.timezone.utc).isoformat() + "Z"
                with _worker_probe_lock:
                    _worker_probe_results[eng["name"]] = result
                log.info(f"worker engine probe: {eng['name']} -> {result['status']} ({result['latency']}s)")
        except Exception as e:
            log.warning(f"worker engine probe loop error: {e}")
        _time_mod.sleep(_PROBE_INTERVAL)


def _start_worker_probe():
    """启动后台探测线程。"""
    global _worker_probe_thread, _worker_probe_started
    if _worker_probe_started:
        return
    _worker_probe_started = True
    _worker_probe_thread = _threading.Thread(target=_worker_probe_loop, daemon=True, name="worker-engine-probe")
    _worker_probe_thread.start()
    log.info("Worker engine probe thread started (10min interval)")


def _collect_worker_probe_results() -> dict:
    """返回当前探测结果快照（供心跳上报）。"""
    with _worker_probe_lock:
        return dict(_worker_probe_results)


def _collect_worker_metrics() -> dict:
    """采集 worker 机器的系统资源指标（用标准库，不依赖 psutil）。

    返回 {cpu_percent, memory_percent, disk_percent, load_avg}。
    采集失败的字段保持 0.0 / 空字符串。
    CPU 使用两次调用的差值计算，首次调用返回 0.0（无基准）。

    Windows：/proc 不可用，用 wmic / shutil.disk_usage 采集（降级）。
    """
    from src.runtime.os_adapter import is_windows, temp_dir as _os_temp
    metrics = {"cpu_percent": 0.0, "memory_percent": 0.0, "disk_percent": 0.0, "tmp_percent": 0.0, "bandwidth_percent": 0.0, "bandwidth_mbps": 0.0, "load_avg": "", "spec": ""}

    if is_windows():
        # Windows: wmic 采集 CPU/MEM（降级模式，无 /proc）
        # spec: CPU 核数 + 总内存
        if not hasattr(_collect_worker_metrics, "_spec"):
            try:
                _cpu_count = os.cpu_count() or 1
                import subprocess as _ws
                _r = _ws.run(["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"],
                             capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                _mem_bytes = 0
                for _line in (_r.stdout or "").split("\n"):
                    _line = _line.strip()
                    if _line.isdigit():
                        _mem_bytes = int(_line)
                        break
                if _mem_bytes > 0:
                    _mem_gb = _mem_bytes // (1024**3)
                    _mem_label = f"{_mem_gb}G" if _mem_gb >= 1 else f"{_mem_bytes // (1024**2)}MB"
                else:
                    _mem_label = "?"
                _collect_worker_metrics._spec = f"{_cpu_count}C{_mem_label}"
            except Exception:
                _collect_worker_metrics._spec = ""
        metrics["spec"] = _collect_worker_metrics._spec

        # CPU: wmic 取 LoadPercentage
        try:
            import subprocess as _ws
            _r = _ws.run(["wmic", "CPU", "get", "LoadPercentage"],
                         capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            for _line in (_r.stdout or "").split("\n"):
                _line = _line.strip()
                if _line.isdigit():
                    metrics["cpu_percent"] = float(_line)
                    break
        except Exception:
            pass

        # MEM: wmic 取 FreePhysicalMemory / TotalVisibleMemorySize
        try:
            import subprocess as _ws
            _r = _ws.run(["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize"],
                         capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            _vals = []
            for _line in (_r.stdout or "").split("\n"):
                _parts = _line.split()
                if len(_parts) == 2 and all(p.isdigit() for p in _parts):
                    _vals = [int(p) for p in _parts]
                    break
            if len(_vals) == 2 and _vals[1] > 0:
                metrics["memory_percent"] = round((_vals[1] - _vals[0]) / _vals[1] * 100, 1)
        except Exception:
            pass

        # DISK: shutil.disk_usage（C 盘）
        try:
            du = _shutil.disk_usage("C:\\")
            if du.total > 0:
                metrics["disk_percent"] = round(du.used / du.total * 100, 1)
        except Exception:
            pass

        # TMP: 临时目录使用率
        try:
            _tmp = _os_temp()
            tu = _shutil.disk_usage(_tmp)
            if tu.total > 0:
                metrics["tmp_percent"] = round(tu.used / tu.total * 100, 1)
        except Exception:
            pass

        # load_avg: Windows 无等价指标，保持空
        # bandwidth: Windows 无 /proc/net/dev 等价，保持 0.0
        return metrics

    # ===== Unix: /proc 采集（保持原有行为不变）=====
    # spec: CPU 核数 + 总内存（如 "4C8G"），首次采集后缓存
    if not hasattr(_collect_worker_metrics, "_spec"):
        try:
            _cpu_count = os.cpu_count() or 1
            _mem_kb = 0
            with open("/proc/meminfo") as f:
                for _line in f:
                    if _line.startswith("MemTotal:"):
                        _mem_kb = int(_line.split()[1])
                        break
            if _mem_kb > 0:
                _mem_gb = _mem_kb * 1024 // (1024**3)
                _mem_label = f"{_mem_gb}G" if _mem_gb >= 1 else f"{_mem_kb // 1024}MB"
            else:
                _mem_label = "?"
            _collect_worker_metrics._spec = f"{_cpu_count}C{_mem_label}"
        except Exception:
            _collect_worker_metrics._spec = ""
    metrics["spec"] = _collect_worker_metrics._spec

    # CPU: 读 /proc/stat，用上次缓存值计算差值（无 sleep，非阻塞）
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        total = sum(int(x) for x in parts)
        idle = int(parts[3])
        if hasattr(_collect_worker_metrics, "_prev"):
            pt, pi = _collect_worker_metrics._prev
            dt = total - pt
            di = idle - pi
            if dt > 0:
                metrics["cpu_percent"] = round((1 - di / dt) * 100, 1)
        _collect_worker_metrics._prev = (total, idle)
    except Exception:
        pass

    # MEM: 读 /proc/meminfo（匹配 top 口径，不含 buff/cache）
    try:
        mi = {}
        with open("/proc/meminfo") as f:
            for line in f:
                p = line.split(":")
                if len(p) == 2:
                    mi[p[0]] = int(p[1].strip().split()[0]) * 1024
        total_mem = mi.get("MemTotal", 0)
        if total_mem > 0:
            free = mi.get("MemFree", 0)
            buffers = mi.get("Buffers", 0)
            cached = mi.get("Cached", 0)
            sreclaimable = mi.get("SReclaimable", 0)
            used = total_mem - free - buffers - cached - sreclaimable
            metrics["memory_percent"] = round(used / total_mem * 100, 1)
    except Exception:
        pass

    # DISK: shutil.disk_usage
    try:
        du = _shutil.disk_usage("/")
        if du.total > 0:
            metrics["disk_percent"] = round(du.used / du.total * 100, 1)
    except Exception:
        pass

    # TMP: /tmp 目录使用率（tmpfs 内存文件系统，独立于根分区）
    try:
        tu = _shutil.disk_usage("/tmp")
        if tu.total > 0:
            metrics["tmp_percent"] = round(tu.used / tu.total * 100, 1)
    except Exception:
        pass

    # load_avg: 读 /proc/loadavg
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            metrics["load_avg"] = ",".join(parts[:3])
    except Exception:
        pass

    # BANDWIDTH: 读 /proc/net/dev（标准库，不依赖 psutil）。两次采样差分算速率，
    # 与配置阈值比较得出占用率（RUNTIME_BANDWIDTH_THRESHOLD_MBPS 默认 1000 Mbps）。
    # 首次采样无基准返回 0.0（与 CPU 采集行为一致）。
    try:
        _now_bw = time.time()
        _rx = _tx = 0
        with open("/proc/net/dev") as f:
            for _line in f:
                if ":" not in _line:
                    continue
                _name, _rest = _line.split(":", 1)
                _name = _name.strip()
                if _name == "lo":
                    continue
                _f = _rest.split()
                if len(_f) >= 9:
                    _rx += int(_f[0])
                    _tx += int(_f[8])
        _bw_prev = getattr(_collect_worker_metrics, "_bw_prev", None)
        if _bw_prev:
            _pts, _pbytes = _bw_prev
            _dt = _now_bw - _pts
            _db = (_rx + _tx) - _pbytes
            if _dt > 0 and _db >= 0:
                _mbps = _db * 8 / _dt / 1e6
                metrics["bandwidth_mbps"] = round(_mbps, 1)
                _thr = float(os.environ.get("RUNTIME_BANDWIDTH_THRESHOLD_MBPS", "10"))
                if _thr > 0:
                    metrics["bandwidth_percent"] = round(min(_mbps / _thr * 100, 100.0), 1)
        _collect_worker_metrics._bw_prev = (_now_bw, _rx + _tx)
    except Exception:
        pass

    return metrics
import logging as _logging_mod
_worker_log_buffer = _collections.deque(maxlen=50)  # 最近 50 条 ERROR/WARNING
_last_sent_log_ts = 0.0  # 上次上报的时间戳，避免重复发送

# 注册一个 logging Handler 把 ERROR/WARNING 写入内存缓冲区
class _WorkerLogHandler(_logging_mod.Handler):
    def emit(self, record):
        try:
            if record.levelno >= _logging_mod.WARNING:
                _worker_log_buffer.append({
                    "ts": _time_mod.strftime("%Y-%m-%d %H:%M:%S", _time_mod.localtime(record.created)),
                    "level": record.levelname,
                    "message": self.format(record)[:500],
                })
        except Exception:
            pass

try:
    _h = _WorkerLogHandler()
    _h.setLevel(_logging_mod.WARNING)
    log.addHandler(_h)
except Exception:
    pass


def _collect_recent_logs():
    """收集自上次上报以来的新日志条目（去重），最多 20 条。"""
    global _last_sent_log_ts
    if not _worker_log_buffer:
        return []
    # 取所有未发送的条目（按时间戳过滤）
    result = []
    max_ts = 0.0
    for ll in _worker_log_buffer:
        # 简单策略：取最近 20 条
        result.append(ll)
    # 取最后 20 条
    result = result[-20:]
    # 清空缓冲区（已发送）
    _worker_log_buffer.clear()
    return result

# v3.0：OBS 直连能力 + 自适应降级状态（运行时能力反馈 C）
_OBS_SUPPORT = False      # 启动探测结果（supports_obs_url）
_OBS_FALLBACK = False     # 直连不稳 → claim 声明 prefer_fallback
_OBS_FAIL_STREAK = 0      # 连续 URL/代拉失败计数（≥阈值降级）
_OBS_OK_STREAK = 0        # 连续成功计数（≥阈值恢复）
# 降级/恢复阈值（可配 env RUNTIME_OBS_FALLBACK_THRESHOLD，默认 3）
_OBS_FALLBACK_THRESHOLD = int(os.environ.get("RUNTIME_OBS_FALLBACK_THRESHOLD", "3"))


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return int(default)


def _discover_local_runtimes() -> list[dict]:
    """本机 CLI 列表（复用 discover_runtimes，补齐 version/healthy）。

    hermes 额外做 ACP 依赖自举（hermes-acp-auto-bootstrap-design.md）：
    --version 探测不出 ACP 依赖缺失（可选 extra），必须 `hermes acp --check`
    才能暴露；缺失时自动补装 agent-client-protocol==0.9.0，补装失败则
    healthy=False（引擎照常注册，但服务端 engine_select 只挑 healthy，
    不会再把任务派到一台调不通的机器上）。
    """
    from src.runtime.registry import discover_runtimes
    from src.runtime.acp_bootstrap import _ensure_hermes_acp
    out = []
    for r in discover_runtimes():
        version = ""
        healthy = False
        cli = r.get("cli_path") or ""
        if cli:
            try:
                import subprocess
                res = subprocess.run([cli, "--version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                healthy = res.returncode == 0
                if healthy:
                    version = (res.stdout or "").strip().split("\n")[0][:60]
            except Exception:
                pass
        # hermes：ACP 依赖可用才算真健康（缺失时已尝试自动补装）
        if healthy and (r["name"] or "").lower() == "hermes":
            healthy = _ensure_hermes_acp(cli)
        # display_name：展示名映射（hwcloud → AIShell；register 接口落 DB display_name 列）
        from src.runtime.instances import engine_display_name, ENGINE_DISPLAY_NAMES
        _et = (r["name"] or "").strip().lower()
        _dn = ENGINE_DISPLAY_NAMES.get(_et, "")
        out.append({"name": r["name"], "display_name": (_dn.capitalize() if _dn else r["name"]),
                    "version": version, "healthy": healthy, "cli_path": cli, "installed": r.get("installed", False)})
    return out


def _remap_cwd(path: str) -> str:
    """将服务端下发的 cwd 路径映射到本地用户 home 目录。

    服务端以 CHAT_CWD_BASE（默认 /home/grape）为基准生成 chat 工作路径，但各接入
    机器的用户不同（lvjuntao、zhangsan 等）。通过 RUNTIME_CWD_PREFIX_MAP 环境变量配置映射：
      RUNTIME_CWD_PREFIX_MAP=/home/grape=/home/lvjuntao
    支持多组映射，用逗号分隔：
      RUNTIME_CWD_PREFIX_MAP=/home/grape=/home/lvjuntao,/opt/data=/data
    未配置时默认将 CHAT_CWD_BASE 映射到当前用户的 home 目录。

    Windows 上 Unix 风格路径前缀（/home/grape）映射到 %USERPROFILE%。
    """
    if not path:
        return path
    from src.runtime.os_adapter import remap_cwd as _os_remap
    import os as _os
    mapping = _os.environ.get("RUNTIME_CWD_PREFIX_MAP", "")
    if not mapping:
        return _os_remap(path)
    for pair in mapping.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        src, dst = pair.split("=", 1)
        src, dst = src.strip(), dst.strip()
        if src and path.startswith(src):
            return dst + path[len(src):]
    return path


def _backend_for_engine(engine_name: str | None = "") -> str:
    """由引擎实例名推导执行后端（hermes-xxx → hermes；codearts-xxx → codearts；codex-xxx → codex；其余默认 opencode）。"""
    name = (engine_name or "").lower()
    if name.startswith("hermes"):
        return "hermes"
    if name.startswith("codearts"):
        return "codearts"
    if name.startswith("codex"):
        return "codex"
    if name.startswith("hwcloud") or name.startswith("aishell"):
        return "hwcloud"
    return "opencode"


def _scan_local_skills() -> list[dict]:
    """扫描 worker 上各 engine 本地物理存在的 skill，返回 per-engine skill 列表。

    每个 engine 扫描其标准 skill 目录。
    每个含 SKILL.md 的子目录视为一个 skill（支持一级和二级目录结构）。
    不扫描 ~/grape/skills（grape 项目自带 skill，Web 端已纳管可见）。
    不扫描 grape-workspaces 下的临时 .skills 目录（task 物化的，非持久）。
    """
    import os as _os
    from src.runtime.os_adapter import engine_skill_dirs, extra_skill_dirs

    # per-engine 专属 skill 目录（跨平台）
    engine_dirs = {
        "hermes": engine_skill_dirs("hermes"),
        "opencode": engine_skill_dirs("opencode"),
        "codearts": engine_skill_dirs("codearts"),
        # hwcloud(AIShell)：~/.hwcloud/skills——修复引擎卡片 skills 列表全空
        # （扫描映射漏此引擎 → local_skills 上报无 entry → DB 空 → 前端空）
        "hwcloud": engine_skill_dirs("hwcloud"),
    }

    # 用户自建 skill 目录（通过环境变量配置，可选，跨平台分隔符）
    extra_dirs = extra_skill_dirs()

    def _scan_dir(base_dir: str) -> list[str]:
        """扫描目录下含 SKILL.md 的子目录，返回 skill_name 列表。
        支持一级（base/skill/SKILL.md）和二级（base/category/skill/SKILL.md）结构。"""
        skills = []
        if not _os.path.isdir(base_dir):
            return skills
        try:
            for entry in _os.listdir(base_dir):
                entry_path = _os.path.join(base_dir, entry)
                if not _os.path.isdir(entry_path):
                    continue
                # 一级：base/entry/SKILL.md
                if _os.path.isfile(_os.path.join(entry_path, "SKILL.md")):
                    skills.append(entry)
                else:
                    # 二级：base/entry/sub/SKILL.md
                    try:
                        for sub in _os.listdir(entry_path):
                            sub_path = _os.path.join(entry_path, sub)
                            if _os.path.isdir(sub_path) and _os.path.isfile(_os.path.join(sub_path, "SKILL.md")):
                                skills.append(sub)
                    except Exception:
                        pass
        except Exception:
            pass
        return skills

    # 扫描用户自建目录（合并到所有 engine）
    common_skills: set[str] = set()
    for d in extra_dirs:
        common_skills.update(_scan_dir(d))

    # 扫描 per-engine 目录 + 合用用户自建
    result = []
    for engine, dirs in engine_dirs.items():
        engine_skills: set[str] = set()
        for d in dirs:
            engine_skills.update(_scan_dir(d))
        engine_skills |= common_skills
        if engine_skills:
            result.append({"engine": engine, "skills": sorted(engine_skills)})

    return result


def _scan_local_skills_with_detail(engine_type: str) -> list[dict]:
    """扫描指定 engine 的 skill 详情（name/description/path/file_count/provider）。

    worker 端执行，扫描结果通过 skills-report API 回报给服务端。
    """
    import os as _os

    all_skills = _scan_local_skills()
    engine_key = (engine_type or "").strip().lower()
    skill_names = []
    for entry in all_skills:
        if (entry.get("engine") or "").strip().lower() == engine_key:
            skill_names = entry.get("skills") or []
            break

    home = _os.path.expanduser("~")
    engine_dirs = {
        "hermes": [_os.path.join(home, ".hermes", "skills")],
        "opencode": [_os.path.join(home, ".config", "opencode", "skills")],
        "codearts": [_os.path.join(home, ".codeartsdoer", "cli-data", "system", "skills")],
        # hwcloud(AIShell)：与 _scan_local_skills/engine_skill_dirs 对齐
        # （.agents/skills 为 openagent 原生全局根，.hwcloud/skills 为 grape 自定义）
        "hwcloud": [_os.path.join(home, ".agents", "skills"),
                    _os.path.join(home, ".hwcloud", "skills")],
    }
    extra = _os.environ.get("RUNTIME_EXTRA_SKILL_DIRS", "")
    scan_dirs = list(engine_dirs.get(engine_key, []))
    if extra:
        for d in extra.split(":"):
            d = d.strip()
            if d and _os.path.isdir(d):
                scan_dirs.append(d)

    skill_details = []
    for sname in skill_names:
        detail = {"name": sname, "description": "", "path": "", "file_count": 0, "provider": engine_type}
        for base_dir in scan_dirs:
            p1 = _os.path.join(base_dir, sname)
            if _os.path.isfile(_os.path.join(p1, "SKILL.md")):
                detail["path"] = p1
                break
            if _os.path.isdir(base_dir):
                for cat in _os.listdir(base_dir):
                    p2 = _os.path.join(base_dir, cat, sname)
                    if _os.path.isfile(_os.path.join(p2, "SKILL.md")):
                        detail["path"] = p2
                        break
                if detail["path"]:
                    break
        if detail["path"]:
            try:
                with open(_os.path.join(detail["path"], "SKILL.md"), "r", encoding="utf-8") as f:
                    content = f.read()
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        fm = content[3:end]
                        for line in fm.split("\n"):
                            if line.strip().startswith("description:"):
                                desc = line.split("description:", 1)[1].strip()
                                desc = desc.strip("\"'")
                                if desc:
                                    detail["description"] = desc[:200]
                                break
            except Exception:
                pass
            try:
                detail["file_count"] = sum(1 for _ in _os.walk(detail["path"]))
            except Exception:
                pass
        skill_details.append(detail)
    return skill_details


def _cleanup_task_tmp(task: dict):
    """task 完成后清理 agent 在 /tmp 下产生的工作残留。
    agent 执行过程中会在 /tmp 创建临时目录（tf-deploy-*/blog-build-*/blog-deploy.*
    /tmp/ptmpl_* 等），task 完成后不清理会叠加撑满 tmpfs。
    只清理已知的 agent 工作产物前缀，不碰其他进程的文件。"""
    import shutil as _shutil
    import glob as _glob
    from src.runtime.os_adapter import is_windows, temp_dir as _os_temp
    _tmp = _os_temp()
    tmp_patterns = [
        f"{_tmp}/tf-deploy-*",
        f"{_tmp}/tf-sys-xhy*",
        f"{_tmp}/blog-build-*",
        f"{_tmp}/blog-deploy*",
        f"{_tmp}/blog-gen*",
        f"{_tmp}/blog-publish-*",
        f"{_tmp}/blog-sys-*.tar.gz",
        f"{_tmp}/ptmpl_*",
        f"{_tmp}/tf-state-backup.json",
        f"{_tmp}/inventory_state.json",
        f"{_tmp}/inventory*.json",
        # bun/opencode 的 JIT .so 缓存——按 cwd 生成不同 hash，
        # 前缀会变化（.39ee*/.3cdc*/.3cdf*/.dccf* 等），必须用通配 /tmp/.*.so 全清。
        # Windows 上 bun 使用 .dll 缓存而非 .so，此 pattern 无匹配（无副作用）。
        f"{_tmp}/.*.so",
        # hermes ACP 隔离目录（ACP 协议自行创建的 hermes-acp-* 前缀，
        # executor.py 只清理 hermes-iso-*，ACP 模式的残留在此兜底）
        # issue #238: hermes-iso-* 已迁移到 ~/.cache；hermes-acp-homes 是持久化目录不清理
        os.path.expanduser("~/.cache/hermes-iso-*"),
        f"{_tmp}/hermes-acp-*",
        f"{_tmp}/hermes-iso-*",
        # hermes results 缓存（agent 执行后的 call 结果文件，堆积不清理）
        f"{_tmp}/hermes-results",
        # hermes sandbox 临时目录
        f"{_tmp}/hermes-sandbox-*",
        f"{_tmp}/hermes-snap-*",
        # tirith 安全扫描安装残留
        f"{_tmp}/tirith-install-*",
    ]
    cleaned = 0
    for pattern in tmp_patterns:
        for path in _glob.glob(pattern):
            try:
                if os.path.isdir(path):
                    _shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    os.remove(path)
                cleaned += 1
            except Exception:
                pass
    if cleaned:
        log.info(f"cleaned {cleaned} temp files/dirs from {_tmp} after task")


def _worker_server_url_reachable(rts: str, task_server_url: str) -> bool:
    """实测 RUNTIME_SERVER_URL 是否可达（内网 worker 判定，1s 短超时 + 60s 结果缓存）。

    仅当 rts 与任务 server_url 不同才需要探测（相同无意义）；探测失败/异常一律
    返回 False（保持原 server_url，宁可走已知公网地址也不猜内网）。
    结果按 (rts, task_server_url) 缓存 60s——探测在任务执行路径上，不能每次任务
    都付 1s 超时（不可达时）。缓存同时记录失败，避免不可达 worker 每任务空等。

    2026-10-15 修复（假阳性覆盖）：探测 URL 必须是"CLI 实际调用方式"——裸 /api/health
    在 nginx/ELB 层就可能 200，但 CLI 的 /api/runtime/* 路由可能不同（鉴权/路由差异），
    造成"探测通了但 CLI 不通"，把任务里正确的 server_url 覆盖成错地址（0005 liufei
    worker：worker.grapedev.cn 任务被覆盖成 www.grapedev.cn:3000，2 任务 failed）。
    改探 /api/runtime/runtimes（CLI 心跳同款端点，不带凭据 401 也算可达——能拿到
    HTTP 响应即证明路由通，比裸 health 更接近 CLI 真实链路）。
    """
    if not rts or not task_server_url or rts.rstrip("/") == task_server_url.rstrip("/"):
        return False
    cache_key = (rts.rstrip("/"), task_server_url.rstrip("/"))
    now = _time_mod.time()
    with _worker_url_probe_lock:
        hit = _worker_url_probe_cache.get(cache_key)
        if hit and now - hit[0] < 60:
            return hit[1]
    ok = False
    try:
        # 探测路径与 CLI 心跳同款路由前缀（/api/runtime/runtimes/0/tags）：不带凭据返回
        # 401/404 都证明"路由可达"（HTTP 层有响应）；curl 000（超时/拒连/DNS 失败）= 不可达。
        # 实测：正确地址 401，坏地址 :3000 与不存在域名 000。
        probe_url = rts.rstrip("/") + "/api/runtime/runtimes/0/tags"
        r = _subprocess.run(
            ["curl", "-sS", "--connect-timeout", "1", "--max-time", "2",
             "-o", "/dev/null", "-w", "%{http_code}", probe_url],
            capture_output=True, timeout=3, text=True,
        )
        # 任何 HTTP 状态码（含 401/403/404）都算"CLI 路由可达"；curl 失败（超时/拒绝/DNS）= 不可达
        code = (r.stdout or "").strip()
        ok = r.returncode == 0 and code.isdigit() and 100 <= int(code) < 600
    except Exception:
        ok = False
    with _worker_url_probe_lock:
        _worker_url_probe_cache[cache_key] = (now, ok)
    return ok


def _kill_stale_engine(task: dict, backend: str = "opencode"):
    """超时/终止后杀掉对应引擎子进程（opencode run / hermes acp / hermes chat / codearts run）。
    按 cwd 匹配，避免误杀其他任务。SIGTERM 后 3s SIGKILL 兜底。

    Windows 用 taskkill /T /F（无 cwd 精确匹配，按 CommandLine 匹配）。"""
    from src.runtime.os_adapter import is_windows, find_process_by_cwd, kill_process_tree
    import subprocess, signal, time as _time
    cwd = _remap_cwd(task.get("cwd") or os.getcwd())
    # hermes ACP 模式用 `hermes acp`，CLI 模式用 `hermes chat`；两个模式都匹配
    patterns = {
        "hermes": ["hermes acp", "hermes chat", "hermes -z"],
        "hwcloud": ["hwcloud serve --acp"],
        "codearts": ["codearts run"],
        "codex": ["codex exec"],
    }.get(backend, ["opencode run"])

    if is_windows():
        # Windows: wmic 查 CommandLine 匹配，taskkill /T /F 终止进程树
        killed_any = False
        for pattern in patterns:
            try:
                out = subprocess.run(
                    ["wmic", "process", "where",
                     f"CommandLine like '%{pattern}%'",
                     "get", "ProcessId"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
                )
                for line in (out.stdout or "").strip().split("\n"):
                    line = line.strip()
                    if not line.isdigit():
                        continue
                    pid = int(line)
                    kill_process_tree(pid, force=False)
                    log.info(f"killed {backend} pid={pid} (pattern={pattern})")
                    killed_any = True
                    # 3s 后强制杀
                    import threading
                    def _force_kill_win(p, delay=3):
                        _time.sleep(delay)
                        kill_process_tree(p, force=True)
                    threading.Thread(target=_force_kill_win, args=(pid,), daemon=True).start()
            except Exception as e:
                log.warning(f"_kill_stale_engine({backend}, pattern={pattern}) failed: {e}")
        if not killed_any:
            log.info(f"_kill_stale_engine({backend}): no matching process found for cwd={cwd}")
        return

    killed_any = False
    for pattern in patterns:
        try:
            out = subprocess.run(
                ["pgrep", "-u", os.environ.get("USER", os.getlogin() if hasattr(os, "getlogin") else ""), "-f", pattern],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
            )
            for pid_str in out.stdout.strip().split():
                if not pid_str:
                    continue
                try:
                    pid = int(pid_str)
                    proc_cwd = os.readlink(f"/proc/{pid}/cwd")
                    if proc_cwd.startswith(cwd):
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGTERM)
                        log.info(f"killed {backend} pid={pid} pgid={pgid} cwd={proc_cwd}")
                        killed_any = True
                        # 3s 后检查进程是否还在，SIGKILL 兜底
                        _time.sleep(0.1)
                        try:
                            os.killpg(pgid, 0)  # check if still alive
                            # 启动后台计时器 3s 后 SIGKILL
                            import threading
                            def _force_kill(pg, delay=3):
                                _time.sleep(delay)
                                try:
                                    os.killpg(pg, signal.SIGKILL)
                                    log.warning(f"force killed pgid={pg} (SIGKILL after {delay}s)")
                                except Exception:
                                    pass
                            threading.Thread(target=_force_kill, args=(pgid,), daemon=True).start()
                        except ProcessLookupError:
                            pass  # already dead
                except Exception as e:
                    log.debug(f"skip pid={pid}: {e}")
        except Exception as e:
            log.warning(f"_kill_stale_engine({backend}, pattern={pattern}) failed: {e}")
    if not killed_any:
        log.info(f"_kill_stale_engine({backend}): no matching process found for cwd={cwd}")


def _fetch_skill_zip(skill_obs_url: str, task_id: str = "") -> bytes | None:
    """v3.0 技能 zip 获取降级链：① URL 直连 OBS（重试）→ ② 服务端代拉（复用 zip_cache）。

    返回 zip 字节；两条路都失败返回 None（调用方降级 skill_files 或回池）。
    """
    timeout = float(os.environ.get("RUNTIME_OBS_FETCH_TIMEOUT", "10"))
    retries = int(os.environ.get("RUNTIME_OBS_FETCH_RETRIES", "3"))
    # ① URL 直连 OBS（预签名 URL，重试）
    for i in range(retries):
        try:
            with _urllib.urlopen(skill_obs_url, timeout=timeout) as resp:
                data = resp.read()
            if data:
                return data
            log.warning(f"[skill] OBS URL 直连返回空({i + 1}/{retries})")
        except Exception as e:
            log.warning(f"[skill] OBS URL 直连失败({i + 1}/{retries}): {type(e).__name__}")
    # ② 服务端代拉（服务端现查 file_path → zip_cache → base64）
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    token = os.environ.get("RUNTIME_TOKEN", "")
    if task_id and token:
        try:
            req = _urllib.Request(
                f"{server}/api/runtime/tasks/{task_id}/skill-zip",
                headers={"Authorization": f"Bearer {token}"})
            with _urllib.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
            b64 = (payload or {}).get("zip_base64") or ""
            if b64:
                log.info(f"[skill] 服务端代拉成功 task={task_id}")
                return _b64.b64decode(b64)
            log.warning(f"[skill] 代拉返回空 task={task_id}")
        except Exception as e:
            log.warning(f"[skill] 服务端代拉失败: {type(e).__name__}")
    return None


def _fetch_skill_zip(skill_obs_url: str, task_id: str = "") -> bytes | None:
    """v3.0 技能 zip 获取降级链：① URL 直连 OBS（重试）→ ② 服务端代拉（复用 zip_cache）。

    返回 zip 字节；两条路都失败返回 None（调用方降级 skill_files 或回池）。
    """
    timeout = float(os.environ.get("RUNTIME_OBS_FETCH_TIMEOUT", "10"))
    retries = int(os.environ.get("RUNTIME_OBS_FETCH_RETRIES", "3"))
    # ① URL 直连 OBS（预签名 URL，重试）
    for i in range(retries):
        try:
            with _urllib.urlopen(skill_obs_url, timeout=timeout) as resp:
                data = resp.read()
            if data:
                return data
            log.warning(f"[skill] OBS URL 直连返回空({i + 1}/{retries})")
        except Exception as e:
            log.warning(f"[skill] OBS URL 直连失败({i + 1}/{retries}): {type(e).__name__}")
    # ② 服务端代拉（服务端现查 file_path → zip_cache → base64）
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    token = os.environ.get("RUNTIME_TOKEN", "")
    if task_id and token:
        try:
            req = _urllib.Request(
                f"{server}/api/runtime/tasks/{task_id}/skill-zip",
                headers={"Authorization": f"Bearer {token}"})
            with _urllib.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
            b64 = (payload or {}).get("zip_base64") or ""
            if b64:
                log.info(f"[skill] 服务端代拉成功 task={task_id}")
                return _b64.b64decode(b64)
            log.warning(f"[skill] 代拉返回空 task={task_id}")
        except Exception as e:
            log.warning(f"[skill] 服务端代拉失败: {type(e).__name__}")
    return None


async def _stream_runtime_async(*args, **kwargs):
    """把同步生成器 stream_runtime 放到后台线程迭代，事件循环不被 readline() 阻塞。

    见 worker-event-loop-block-rootcause.md §8：方案 1（await sleep(0)）只在生成器每次 yield 时
    让出；当 opencode 执行长命令静默无输出（readline 阻塞）时事件循环仍被占住。本函数把
    同步迭代移入线程，通过线程安全 queue.Queue 桥接事件，彻底消除阻塞。
    """
    import queue as _queue
    from src.runtime.executor import stream_runtime
    q: _queue.Queue = _queue.Queue(maxsize=256)
    sentinel = object()

    def _produce():
        try:
            for evt in stream_runtime(*args, **kwargs):
                while True:
                    try:
                        q.put_nowait(evt)
                        break
                    except _queue.Full:
                        # 队列满：短暂让出，等消费者消费；sleep 在子线程安全
                        import time as _t
                        _t.sleep(0.001)
        except Exception as e:
            log.warning(f"stream_runtime producer error: {e}")
        finally:
            q.put(sentinel)

    import threading
    _t = threading.Thread(target=_produce, daemon=True)
    _t.start()
    while True:
        try:
            evt = q.get_nowait()
        except _queue.Empty:
            await asyncio.sleep(0.001)  # 轮询间隔，同时让出事件循环
            continue
        if evt is sentinel:
            break
        yield evt


async def _materialize_files(client, tid, files_dict, file_urls, dest_dir, tag="files"):
    """三级降级物化文件到 dest_dir（project-info / knowledge 共用）。

    降级顺序：① file_urls 优先 URL 下载（urllib HTTP GET）
              ② URL 失败/无 URL → content（服务端兜底打包的内联内容）
              ③ content 为空 → storage_key 直连 storage.read_batch 并发读取
    file_urls 为空（老任务 / OBS 未启用）→ 纯走 content 老逻辑不变。

    files_dict: {rel_path: content_str}
    file_urls:  {rel_path: {"url": "https://...", "storage_key": "knowledge/..."}}
    """
    import os as _os
    if not files_dict and not file_urls:
        return

    # file_urls 为空 → 走 content 老逻辑（直接写）
    if not file_urls:
        for rel, content in (files_dict or {}).items():
            if rel == ".metadata.json":
                continue
            dest = _os.path.join(dest_dir, rel)
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        return

    stats = {"url_fail": 0, "storage_fail": 0, "empty": 0}
    # 收集需要 storage_key 直连的文件：{storage_key: rel_path}
    need_storage = {}

    for rel, content in (files_dict or {}).items():
        if rel == ".metadata.json":
            continue
        meta = file_urls.get(rel, {})
        url = meta.get("url", "")
        got_content = None  # 最终拿到的内容

        # ① 优先 URL 下载
        if url:
            try:
                with _urllib.urlopen(url, timeout=10) as resp:
                    raw = resp.read()
                got_content = raw.decode("utf-8") if isinstance(raw, bytes) else (raw or "")
                if not got_content:
                    raise ValueError("empty response")
            except Exception as e:
                log.warning(f"[{tag}] url download failed: rel={rel}, err={type(e).__name__}")
                stats["url_fail"] += 1
                got_content = None  # 降级

        # ② URL 失败/无 URL → content
        if got_content is None and content:
            got_content = content

        # ③ content 为空 → 收集到 need_storage，稍后批量读取
        if got_content is None:
            storage_key = meta.get("storage_key", "")
            if storage_key:
                need_storage[storage_key] = rel
            else:
                # 无 url 无 content 无 storage_key → 空内容
                stats["empty"] += 1
                log.error(f"[{tag}] all fallback failed, empty content: rel={rel}")
            continue

        # 写入文件
        dest = _os.path.join(dest_dir, rel)
        _os.makedirs(_os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(got_content)

    # 批量并发读取存储直连文件
    if need_storage:
        try:
            from src.storage import get_storage
            storage = get_storage()
        except Exception as e:
            storage = None
            log.error(f"[{tag}] get_storage failed: {e}")

        if storage is not None:
            keys = list(need_storage.keys())
            results = await storage.read_batch(keys)
            log.info(f"[{tag}] batch read: {sum(1 for v in results.values() if v)}/{len(keys)} succeeded")
            for key, rel in need_storage.items():
                content = results.get(key, "")
                if content:
                    dest = _os.path.join(dest_dir, rel)
                    _os.makedirs(_os.path.dirname(dest), exist_ok=True)
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(content)
                else:
                    stats["storage_fail"] += 1
                    log.error(f"[{tag}] storage read failed: rel={rel}, key={key}")
        else:
            # storage 不可用，全部记 storage_fail
            for key, rel in need_storage.items():
                stats["storage_fail"] += 1
                log.error(f"[{tag}] storage unavailable, read failed: rel={rel}, key={key}")

    # 降级统计上报
    total_degraded = stats["url_fail"] + stats["storage_fail"] + stats["empty"]
    if total_degraded > 0:
        log.warning(f"[{tag}] degraded: url_fail={stats['url_fail']}, "
                    f"storage_fail={stats['storage_fail']}, empty={stats['empty']}")
        try:
            await client.report_messages(tid, [{
                "type": "text",
                "data": f"⚠️ {tag} 文件降级：URL下载失败 {stats['url_fail']} 个，"
                        f"存储直连失败 {stats['storage_fail']} 个，空内容 {stats['empty']} 个"
            }])
        except Exception:
            pass


def _cleanup_task_workdir(task: dict, env: dict | None = None):
    """任务结束后清理工位：删轻文件（.skills/.project-info/.knowledge）+ 安全卸载 worktree。

    轻文件直接删（下次派单重新物化）；worktree 走 remove_issue_worktree_if_pushed（确认已 push）。
    清理失败只告警，不影响任务结果上报与后续流水线。

    Issue #242: coding agent 有关联仓库 worktree 时，同步清理关联 worktree。
    PR 提交由 _submit_linked_repo_prs 在 cleanup 前独立完成（不在此函数内重复）。
    """
    import os as _os
    import shutil as _shutil
    cwd = task.get("cwd") or ""
    if not cwd or not _os.path.isdir(cwd):
        return
    for _sub in (".skills", ".project-info", ".knowledge"):
        _p = _os.path.join(cwd, _sub)
        try:
            if _os.path.isdir(_p):
                _shutil.rmtree(_p, ignore_errors=True)
        except Exception:
            pass
    try:
        from src.runtime import repogit
        _num = repogit.issue_num_from(task.get("issue_id") or "")
        if _num and task.get("issue_session_id"):
            repogit.remove_issue_worktree_if_pushed(
                task.get("repo_key") or "", task.get("agent_type") or "",
                _num, token=_get_task_token(task))
            # Issue #242: 清理关联仓库 worktree（coding agent 多仓库）
            _ag = task.get("agent_type") or ""
            if _ag == "coding":
                _linked_wt = (env or {}).get("GRAPE_LINKED_WORKTREES", "") if env else ""
                if not _linked_wt:
                    _linked_wt = (task.get("env_extra") or {}).get("GRAPE_LINKED_WORKTREES", "") if isinstance(task.get("env_extra"), dict) else ""
                if _linked_wt:
                    import json as _json_lr
                    try:
                        for _wt_info in (_json_lr.loads(_linked_wt) if _linked_wt else []):
                            _lrk = _wt_info.get("repo_key") or ""
                            if _lrk:
                                repogit.remove_issue_worktree_if_pushed(
                                    _lrk, "coding", _num,
                                    token=_get_task_token(task))
                    except Exception as _le:
                        log.warning(f"linked worktree cleanup failed: {_le}")
    except Exception as e:
        log.warning(f"workdir cleanup failed for task {task.get('id')}: {e}")
    # 连空 cwd 目录一起删（non-git agent 删完物化文件后为空；git agent 的 worktree 已被整目录删掉）
    try:
        if _os.path.isdir(cwd) and not _os.listdir(cwd):
            _shutil.rmtree(cwd, ignore_errors=True)
    except Exception:
        pass


def _submit_linked_repo_prs(task: dict, env: dict):
    """Issue #242: coding agent 成功完成后，遍历 GRAPE_LINKED_WORKTREES，
    对每个有改动的关联仓库 worktree 执行 push + create_pr_for_repo 提交 PR。

    闭环多仓库 PR 提交流程：coding agent 在关联 worktree 中完成开发后，
    本函数自动 push 改动并向每个关联仓库创建 PR。
    失败只告警，不阻塞任务完成与主仓库 PR 流程。
    """
    import os as _os
    import json as _json
    import subprocess as _sp

    if (task.get("agent_type") or "") != "coding":
        return
    _linked_wt_json = env.get("GRAPE_LINKED_WORKTREES", "")
    if not _linked_wt_json:
        return
    try:
        _wts = _json.loads(_linked_wt_json)
    except Exception:
        return
    if not _wts or not isinstance(_wts, list):
        return

    _token = _get_task_token(task)
    _issue_num = ""
    try:
        from src.runtime import repogit
        _issue_num = repogit.issue_num_from(task.get("issue_id") or "")
    except Exception:
        pass
    if not _issue_num:
        log.warning("linked PR: cannot determine issue_num, skip")
        return

    _wt_branch = f"feat/issue-{_issue_num}"
    _bot_name = env.get("BOT_USERNAME") or "grape_bot"
    _results = []

    for wt in _wts:
        if not isinstance(wt, dict):
            continue
        _rk = wt.get("repo_key") or ""
        _wt_dir = wt.get("dir") or ""
        _base_branch = wt.get("branch") or "main"
        if not _rk or not _wt_dir or not _os.path.isdir(_wt_dir):
            log.warning(f"linked PR: skip invalid worktree: rk={_rk} dir={_wt_dir}")
            continue

        # 0. 分支护栏：检查当前分支是否是 feat/issue-{N}，禁止直推基线分支
        from src.runtime.repogit import ensure_feature_branch
        ensure_feature_branch(_wt_dir, _wt_branch, _base_branch, logger=log)

        try:
            # 1. git identity（防 commit 被拒）
            _sp.run(["git", "-C", _wt_dir, "config", "user.name", _bot_name],
                    capture_output=True, text=True, timeout=10)
            _sp.run(["git", "-C", _wt_dir, "config", "user.email",
                     f"{_bot_name}@noreply.git.com"],
                    capture_output=True, text=True, timeout=10)

            # 2. stage 未提交改动
            _sp.run(["git", "-C", _wt_dir, "add", "-A"],
                    capture_output=True, text=True, timeout=60)
            _r_diff = _sp.run(["git", "-C", _wt_dir, "diff", "--cached", "--quiet"],
                              capture_output=True, text=True, timeout=30)
            _has_staged = _r_diff.returncode != 0

            # 3. 检查是否有领先于 base 分支的提交（origin/{base}..HEAD）
            _r_ahead = _sp.run(
                ["git", "-C", _wt_dir, "rev-list", "--count",
                 f"origin/{_base_branch}..HEAD"],
                capture_output=True, text=True, timeout=15)
            _ahead = 0
            if _r_ahead.returncode == 0:
                try:
                    _ahead = int((_r_ahead.stdout or "0").strip() or "0")
                except ValueError:
                    _ahead = 0
            else:
                # origin/{base} 不存在 → 无法比较，保守认为有改动（push 会建远程分支）
                _ahead = 1

            if not _has_staged and _ahead == 0:
                log.info(f"linked PR: no changes in {_rk}, skip")
                _results.append({"repo_key": _rk, "status": "skipped", "reason": "no changes"})
                continue

            # 4. commit staged changes
            if _has_staged:
                _msg = f"feat: #{_issue_num} linked repo changes"
                _r_commit = _sp.run(["git", "-C", _wt_dir, "commit", "-m", _msg],
                                    capture_output=True, text=True, timeout=60)
                if _r_commit.returncode != 0:
                    log.warning(f"linked PR: commit failed {_rk}: {(_r_commit.stderr or '')[:200]}")
                    _results.append({"repo_key": _rk, "status": "failed", "reason": "commit failed"})
                    continue

            # 5. push（token 经 http.extraheader 注入，不落盘）
            from src.runtime.repogit import _run_git
            _r_push = _run_git(
                ["-C", _wt_dir, "push", "origin", f"HEAD:refs/heads/{_wt_branch}"],
                token=_token, timeout=120)
            if _r_push is None or _r_push.returncode != 0:
                _err = (_r_push.stderr[:300] if _r_push else "git exec failed") if _r_push else "git exec failed"
                log.warning(f"linked PR: push failed {_rk}: {_err}")
                _results.append({"repo_key": _rk, "status": "failed", "reason": f"push: {_err[:100]}"})
                continue

            # 6. create PR
            from src.vcs.api_client import create_pr_for_repo
            _pr_title = f"feat: #{_issue_num} linked repo ({_rk})"
            _pr_body = (f"跨仓库需求开发 PR（Issue #{_issue_num}）\n\n"
                        f"由 coding agent 自动提交，关联主仓库 {_rk}。\n\n"
                        f"Closes #{_issue_num}")
            _pr_result = create_pr_for_repo(
                repo_key=_rk, token=_token,
                title=_pr_title, head=_wt_branch, base=_base_branch,
                body=_pr_body)
            if isinstance(_pr_result, dict) and "error" not in _pr_result:
                _pr_num = _pr_result.get("number") or ""
                _pr_url = _pr_result.get("html_url") or ""
                log.info(f"linked PR: created #{_pr_num} for {_rk}: {_pr_url}")
                _results.append({"repo_key": _rk, "status": "created",
                                 "pr_number": _pr_num, "pr_url": _pr_url})
            else:
                _err = _pr_result.get("error", "unknown") if isinstance(_pr_result, dict) else "unknown"
                log.warning(f"linked PR: create_pr failed {_rk}: {_err}")
                _results.append({"repo_key": _rk, "status": "failed", "reason": f"PR create: {_err}"})
        except Exception as e:
            log.warning(f"linked PR: error for {_rk}: {e}")
            _results.append({"repo_key": _rk, "status": "error", "reason": str(e)[:200]})

    task["_linked_pr_results"] = _results
    _created = [r for r in _results if r.get("status") == "created"]
    _skipped = [r for r in _results if r.get("status") == "skipped"]
    _failed = [r for r in _results if r.get("status") in ("failed", "error")]
    log.info(f"linked PR summary: {len(_created)} created, {len(_skipped)} skipped, {len(_failed)} failed")


async def _execute_task(client, task, engine_name: str = ""):
    """执行单个任务：git worktree 前置 → 落地技能/知识 → 占位符替换 → stream_runtime → 回报。

    engine_name 为该任务所属引擎实例名（如 opencode-worker-123249），用于日志区分。
    """
    import os as _os
    tid = task.get("id", "")
    cwd = _remap_cwd(task.get("cwd") or _os.getcwd())
    task["cwd"] = cwd
    _engine_tag = f"|engine:{engine_name}" if engine_name else ""

    # LLM 用量采集（可观测性）：必须在引擎启动前记基线——hermes 的账本是累计值、
    # hwcloud 日志是 append-only，两者都要靠基线才能算出"本次任务"的增量。
    # 用本地 cwd（与各引擎 ACP 隔离目录的 cwd-hash 算法同源），失败静默降级。
    # （2026-10-21 恢复：fbc499c4a 重构 #378 看门狗时误删本块，导致 worker 侧
    #   用量上报断流——snake-game01 一仓 9 任务仅 1 条记录的根因，见 issue 记录。）
    from src.runtime.usage_collect import UsageCollector
    _usage_col = UsageCollector(_backend_for_engine(engine_name), cwd=cwd)
    try:
        _usage_col.start()
    except Exception as _ue:
        log.warning(f"usage collector start failed task={tid}: {_ue}")

    # TaskResourceStack: unified resource lifecycle for this task
    from src.runtime.resource_stack import TaskResourceStack
    _res = TaskResourceStack(task_id=tid, cwd=cwd)
    # Register known tmp patterns for cleanup on any exit path
    for pat in [
        "/tmp/tf-deploy-*", "/tmp/tf-sys-xhy*", "/tmp/blog-build-*",
        "/tmp/blog-deploy*", "/tmp/blog-gen*", "/tmp/blog-publish-*",
        "/tmp/blog-sys-*.tar.gz", "/tmp/ptmpl_*", "/tmp/tf-state-backup.json",
        "/tmp/inventory_state.json", "/tmp/inventory*.json", "/tmp/.*.so",
        "/tmp/hermes-acp-*", "/tmp/hermes-iso-*", "/tmp/hermes-results",
        "/tmp/hermes-sandbox-*", "/tmp/hermes-snap-*", "/tmp/tirith-install-*",
        # issue #238: hermes-iso-* 已迁移到 ~/.cache；hermes-acp-homes 是持久化目录不清理
        os.path.expanduser("~/.cache/hermes-iso-*"),
    ]:
        _res.track_tmp_pattern(pat)

    # Ensure cwd exists before dependency check (remote worker: server creates
    # the dir on its side, but worker needs its own local copy after _remap_cwd)
    try:
        _os.makedirs(cwd, exist_ok=True)
    except Exception:
        pass

    # Pre-execution dependency validation (Cordis inject pattern)
    from src.runtime.dependency_check import validate_task_dependencies, DependencyError
    try:
        validate_task_dependencies(task, engine_name)
    except DependencyError as _de:
        _res.dispose()
        return {"_error": str(_de), "_started": False}

    # Context compaction: will check batch size before flush
    from src.runtime.context_compaction import should_compact, compact_messages

    env = {}
    try:
        env = json.loads(task.get("env_extra") or "{}")
    except Exception:
        pass
    # Fetch latest env (AK/SK) from server DB — overrides stale snapshot
    try:
        _fresh_env = await client.get_task_env(tid)
        if _fresh_env:
            env.update(_fresh_env)
    except Exception:
        pass
    # Issue #378: 按 agent_type 注入差异化静默超时 env（coding/qa=1200s，其余默认 300s）。
    # setdefault 语义：上层 env_extra / fresh_env 已显式设置（手动调参）时不覆盖。
    # 注入到局部 env dict（并发安全，不写 os.environ 避免 async 交错竞态，见 L1774 注释），
    # 经 env_extra → stream_agent env.update(env_extra) → 各 backend 读 env 生效。
    _ag_silent = _AGENT_SILENT_TIMEOUTS.get((task.get("agent_type") or "").lower())
    if _ag_silent:
        env.setdefault("AGENT_SILENT_WINDOW", str(_ag_silent))
        env.setdefault("HERMES_SILENT_TIMEOUT", str(_ag_silent))
        env.setdefault("RUNTIME_TOOL_SILENT_TIMEOUT", str(_ag_silent))
    # 本机 worker 修正对外服务地址：任务入队时 plugin 广播 runtime_id=0，server_url 被写成
    # 公网 server_base_url（如 http://123.249.19.227）。本机 worker 抢到任务后，agent 内部
    # A2A/tasks 等调用若走公网会 NAT 回环不通（超时）；必须覆盖为 localhost 直连。
    # 远程 worker 的 RUNTIME_SERVER_URL 是公网，不受影响（保持原 server_url）。
    _rts = os.environ.get("RUNTIME_SERVER_URL", "") or ""
    _is_local_worker = ("localhost" in _rts) or ("127.0.0.1" in _rts) or _rts.startswith("127.")
    _local_base = ""
    _old_server_url = task.get("server_url") or ""
    if _is_local_worker or _worker_server_url_reachable(_rts, _old_server_url):
        # 判定改为实测可达（2026-10-12 seq#552 复盘）：本地 worker 或内网 worker
        # （RUNTIME_SERVER_URL 直连可达，如内网 worker 直连主控内网地址 3ms）
        # 都覆盖为本 worker 直连地址；远程公网 worker（内网不可达）保持原
        # server_url（公网 base）。旧逻辑只认 localhost/127.*，内网 worker 的
        # A2A 走 meta.server_base_url 公网地址 NAT 回环不通 → a2a 调用反复超时
        # （实测：内网直连 200/3ms，公网地址 NAT 回环三次 5s 超时全挂）。
        _local_base = _rts.rstrip("/") if _rts else "http://127.0.0.1:3000"
        # 端口拼接规则（2026-10-15 修复）：https:// 走标准 443，绝不补端口——老规则"无端口
        # 一律补 :3000"是 devops-bot 直连 3000 时代的遗产，会把 https://www.grapedev.cn 拼成
        # www.grapedev.cn:3000（0005 liufei worker 实测 8 次错误覆盖，2 任务失败）。
        # 仅 http:// 且明确是本机（127.0.0.1/localhost）才补 3000；http:// 内网 IP 不动
        # （非 3000 端口的服务端地址本就带端口进 RUNTIME_SERVER_URL）。
        if _local_base.startswith("https://"):
            pass  # 443，不补
        elif _local_base.startswith("http://") and (
            "localhost" in _local_base or _local_base.split("://", 1)[1].startswith(("127.", "localhost"))
        ):
            if ":" not in _local_base.split("://", 1)[1]:
                _local_base = _local_base + ":3000"
        # 其他（http:// 内网域名/IP 无端口）：保持原样，不猜端口
        env["GRAPE_SERVER_URL"] = _local_base
        task["server_url"] = _local_base
        log.info(f"local worker: override GRAPE_SERVER_URL/server_url -> {_local_base}")
    # git worktree 前置（coding/qa/sre，X 工作模型）：必须先建 worktree 再落地技能，
    # 否则 cwd 被 .skills 物化占住（目录存在但无 .git）会导致 `git worktree add` 报
    # "already exists"，coding agent 拿不到 git 基线后自行在项目根外建 worktree → 权限卡死。
    # 见 coding-worktree-skill-collision.md §4.1（方案 A）。
    # P2（vcs-env-token-boundary-audit）：不再把 GIT_ASKPASS 注入 agent env——git 操作走 `grape git`。
    try:
        from src.runtime import repogit
        _num = repogit.issue_num_from(task.get("issue_id") or "")
        if repogit.is_git_agent(task.get("agent_type") or "") and _num and task.get("issue_session_id"):
            _wb = env.get("GRAPE_WORK_BRANCH", "")
            repogit.ensure_issue_worktree(task.get("repo_key") or "", task.get("agent_type") or "",
                                          _num, token=_get_task_token(task),
                                          work_branch=_wb)
    except Exception as e:
        log.warning(f"git worktree ensure failed: {e}")
    # Issue #242: 关联仓库 worktree 初始化（coding/sre agent 多仓库并行操作）
    try:
        from src.runtime import repogit
        _linked_repos_env = env.get("GRAPE_LINKED_REPOS", "")
        if _linked_repos_env and task.get("agent_type") in ("coding", "sre", "qa"):
            import json as _json_lr
            _linked_repos_list = _json_lr.loads(_linked_repos_env) if _linked_repos_env else []
            if _linked_repos_list:
                _num_lr = repogit.issue_num_from(task.get("issue_id") or "")
                _wt_list = repogit.ensure_linked_repo_worktrees(
                    _linked_repos_list, task.get("agent_type", ""), _num_lr,
                    token=_get_task_token(task))
                if _wt_list:
                    env["GRAPE_LINKED_WORKTREES"] = _json_lr.dumps(
                        [{"repo_key": w["repo_key"], "branch": w["branch"], "dir": w["worktree_dir"]}
                         for w in _wt_list], ensure_ascii=False)
                    log.info(f"linked repos: {len(_wt_list)} worktrees initialized for issue={_num_lr} agent={task.get('agent_type','')}")
    except Exception as e:
        log.warning(f"linked repos worktree init failed: {e}")
    skill_dir = _os.path.join(cwd, ".skills")
    prompt = (task.get("prompt") or "").replace("{{SKILL_DIR}}", skill_dir) \
                                       .replace("{{SERVER_URL}}", task.get("server_url") or "")
    # 本机 worker：prompt 里 build_prompt 已把 {{SERVER_URL}} 替换成公网 server_base_url
    # （worker 端此处 .replace("{{SERVER_URL}}") 是空操作），需把 prompt 里已硬编码的公网
    # 地址回改为 localhost，否则本机 worker 的 A2A 走公网 NAT 回环不通（超时）。
    if _is_local_worker and _old_server_url and _local_base and _old_server_url != _local_base:
        prompt = prompt.replace(_old_server_url, _local_base)
        log.info(f"local worker: rewrote prompt server_url {_old_server_url} -> {_local_base}")
    try:
        # ===== v3.0：技能获取直连 OBS（降级链见 _fetch_skill_zip）=====
        # ① skill_obs_url（claim 现签预签名 URL）→ worker 直连 OBS
        # ② 失败 → 服务端代拉（POST /tasks/{id}/skill-zip，复用 zip_cache）
        # ③ 无 URL（老任务/无 OBS 技能）→ skill_files 文本物化（既有）
        skill_obs_url = task.get("skill_obs_url") or ""
        skill_zip_b64 = None
        if skill_obs_url:
            raw = _fetch_skill_zip(skill_obs_url, task_id=task.get("id", ""))
            if raw:
                skill_zip_b64 = _b64.b64encode(raw).decode()
                # v3.0 能力反馈：直连/代拉成功 → 计数恢复
                global _OBS_FAIL_STREAK, _OBS_OK_STREAK, _OBS_FALLBACK
                _OBS_FAIL_STREAK = 0
                _OBS_OK_STREAK += 1
                if _OBS_OK_STREAK >= _OBS_FALLBACK_THRESHOLD and _OBS_FALLBACK:
                    _OBS_FALLBACK = False
                    _OBS_OK_STREAK = 0
                    log.info("[skill] OBS 直连恢复（连续 3 次成功）")
            else:
                log.warning(f"[skill] OBS 直连+代拉均失败, task={task.get('id')} url={skill_obs_url[:60]}...")
                # v3.0 能力反馈：连续失败 → 声明 prefer_fallback（服务端直接代拉模式）
                _OBS_FAIL_STREAK += 1
                _OBS_OK_STREAK = 0
                if _OBS_FAIL_STREAK >= _OBS_FALLBACK_THRESHOLD and not _OBS_FALLBACK:
                    _OBS_FALLBACK = True
                    _OBS_FAIL_STREAK = 0
                    log.warning("[skill] OBS 直连不稳定，降级为服务端代拉（prefer_fallback）")
        if skill_zip_b64:
            # ===== zip 物化（直连/代拉获取的 zip → 解压 → 恢复权限）=====
            # 见 obs-skill-storage-design.md §5.7（Python 3.12+ zipfile 不再自动恢复
            # Unix 权限位，手动按 external_attr 恢复；剥离 setuid/setgid/sticky 防提权）
            raw = _b64.b64decode(skill_zip_b64)
            tmp_zip = _os.path.join(skill_dir, "_skill_delivery.zip")
            _os.makedirs(skill_dir, exist_ok=True)
            with open(tmp_zip, "wb") as f:
                f.write(raw)
            with _zf.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(skill_dir)  # zip 根 = {skill_name}/，落地 .skills/{skill_name}/
                for info in zf.infolist():
                    mode = info.external_attr >> 16
                    if mode and not info.is_dir():
                        p = _os.path.join(skill_dir, info.filename)
                        if _os.path.isfile(p):
                            _os.chmod(p, mode & 0o777)
            try:
                _os.remove(tmp_zip)
            except Exception:
                pass
            env["DEVOPS_SKILLS_DIR"] = skill_dir
        else:
            # ===== 旧格式：skill_files 文本物化（老任务/老 worker 兼容）=====
            skill_files = json.loads(task.get("skill_files") or "{}")
            for _sn, files in skill_files.items():
                for rel, content in (files or {}).items():
                    dest = _os.path.join(skill_dir, _sn, rel)
                    _os.makedirs(_os.path.dirname(dest), exist_ok=True)
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(content)
            if skill_files:
                env["DEVOPS_SKILLS_DIR"] = skill_dir
    except Exception as e:
        log.warning(f"skill materialize failed: {e}")
    # project-info 落地（repo 全局共享，见 multi-issue-isolation-design.md §2.4）
    proj_dir = _os.path.join(cwd, ".project-info")
    try:
        proj_files = json.loads(task.get("project_info_files") or "{}")
        proj_file_urls = json.loads(task.get("project_info_file_urls") or "{}")
        await _materialize_files(client, tid, proj_files, proj_file_urls, proj_dir,
                                 tag="project-info")
        prompt = prompt.replace("{{PROJECT_INFO_DIR}}", proj_dir)
    except Exception as e:
        log.warning(f"project-info materialize failed: {e}")
    # 共享知识落地（event-conversation-design.md §3.1.1；随任务交付远端 worker）
    know_dir = _os.path.join(cwd, ".knowledge")
    try:
        know_files = json.loads(task.get("knowledge_files") or "{}")
        know_file_urls = json.loads(task.get("knowledge_file_urls") or "{}")
        await _materialize_files(client, tid, know_files, know_file_urls, know_dir,
                                 tag="knowledge")
        prompt = prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)
    except Exception as e:
        log.warning(f"knowledge materialize failed: {e}")

    # ── 方案 G: codearts per-task 独立数据目录 ──
    # 通过 KERNEL_DATA_DIR 指向 per-task 临时目录，预写 global.json + sandbox/config.json
    # 实现 per-process 权限隔离，不影响宿主机全局配置。
    # env 是局部变量，并发安全（不用 os.environ 避免 async 交错竞态）。
    # 通过 env_extra 传给 stream_agent → env.update(env_extra) 覆盖 get_env 的默认值。
    _engine = engine_name or ""
    if "codearts" in _engine.lower():
        try:
            _tasks_base = _os.environ.get("CODEARTS_TASKS_DIR", "/var/cache/codearts-tasks")
            _data_dir = _os.path.join(_tasks_base, tid)
            _cli_data = _os.path.join(_data_dir, "cli-data")
            # 1. 预写 global.json (write/edit/external_directory_write=allow)
            _perm_dir = _os.path.join(_cli_data, "storage", "permission")
            _os.makedirs(_perm_dir, exist_ok=True)
            _global_json = _os.path.join(_perm_dir, "global.json")
            if not _os.path.exists(_global_json):  # 幂等
                with open(_global_json, "w") as f:
                    json.dump(_CODEARTS_ALLOW_PERMS, f, indent=2)
            # 1b. 预写 config.json (codearts 26.8.8 getEffectivePermissions 实际读取路径)
            _config_json = _os.path.join(_perm_dir, "config.json")
            if not _os.path.exists(_config_json):  # 幂等
                with open(_config_json, "w") as f:
                    json.dump(_CODEARTS_ALLOW_PERMS, f, indent=2)
            # 2. 预写 sandbox/config.json (network_policy=allow_all)
            _sandbox_dir = _os.path.join(_cli_data, "storage", "sandbox")
            _os.makedirs(_sandbox_dir, exist_ok=True)
            _sandbox_cfg = _os.path.join(_sandbox_dir, "config.json")
            if not _os.path.exists(_sandbox_cfg):  # 幂等
                with open(_sandbox_cfg, "w") as f:
                    f.write('{"network_policy": "allow_all"}')
            # 3. symlink sandbox_runner + system/skills 指向宿主机（避免每次解压 66MB）
            _home = _os.path.expanduser("~")
            _host_cli = _os.path.join(_home, ".codeartsdoer", "cli-data")
            _sandbox_runner = _os.path.join(_cli_data, "sandbox_runner")
            if not _os.path.exists(_sandbox_runner) and _os.path.isdir(_os.path.join(_host_cli, "sandbox_runner")):
                _os.symlink(_os.path.join(_host_cli, "sandbox_runner"), _sandbox_runner)
            _sys_skills = _os.path.join(_cli_data, "system", "skills")
            if not _os.path.exists(_sys_skills) and _os.path.isdir(_os.path.join(_host_cli, "system", "skills")):
                _os.makedirs(_os.path.join(_cli_data, "system"), exist_ok=True)
                _os.symlink(_os.path.join(_host_cli, "system", "skills"), _sys_skills)
            # 4. 设置 KERNEL_DATA_DIR 到 env dict（局部变量，并发安全）
            env["KERNEL_DATA_DIR"] = _cli_data
            # 5. 注册清理：task 结束时删除临时目录
            _res.track_tmp_pattern(f"{_tasks_base}/{tid}")
            log.info(f"[Agent:{task.get('agent_type') or ''}] codearts data dir: {_cli_data}")
        except Exception as e:
            log.warning(f"codearts data dir setup failed: {e}")

    done = None
    batch = []
    started = False
    _last_flush = time.monotonic()  # 实时日志双触发（50条/500ms）时间基
    try:
        from src.runtime.executor import stream_runtime
        _agent_label = f"[Agent:{task.get('agent_type') or ''}|repo:{task.get('repo_key') or ''}{_engine_tag}]"
        # 逐步日志前缀：seq + 非空的 issue/agent/repo/engine，便于在 journalctl 按事件/issue/仓库定位
        _esq_parts = [f"seq={task.get('event_seq') or '?'}"]
        _issue_num = (task.get("issue_id") or "").strip().lstrip("#")
        if _issue_num and _issue_num != "0":
            _esq_parts.append(f"issue:#{_issue_num}")
        _agent_type = task.get("agent_type") or ""
        if _agent_type:
            _esq_parts.append(f"agent:{_agent_type}")
        _repo_key = task.get("repo_key") or ""
        if _repo_key:
            _esq_parts.append(f"repo:{_repo_key}")
        if engine_name:
            _esq_parts.append(f"engine:{engine_name}")
        _esq = "|".join(_esq_parts)
        # chat 任务以用户身份执行（回帖/建 issue 等 GitCode API 操作）——注入用户级 token。
        # Issue #344: 不再从 task payload 读缓存的明文 token，改为实时从 external_identities
        # 获取（带预刷新），避免缓存过期 token 导致 agent 调 GitCode API 401。
        # username 从 DB 查询（不依赖 task payload 的 account 字段，防止用户改名后不一致）。
        user_token = ""
        if (task.get("agent_type") or "") == "chat" or (task.get("event_type") or "") == "chat":
            _task_user_id = task.get("user_id") or 0
            if _task_user_id > 0:
                try:
                    from src.api.deps import get_user_token_with_refresh
                    from src.auth.grape_account_db import get_grape_account_by_id
                    _acc = get_grape_account_by_id(_task_user_id)
                    if _acc:
                        user_token = get_user_token_with_refresh(_acc["username"], user_id=_task_user_id) or ""
                except Exception as e:
                    log.warning(f"chat user token resolve failed: {e}")
        # 方案 2：线程化迭代 stream_runtime（worker-event-loop-block-rootcause.md §8.5）——
        # 同步生成器的 readline() 阻塞移到后台线程，事件循环持续调度 claim/heartbeat，
        # 根治「长命令静默无输出」期间的事件循环停摆（替代方案 1 的 await sleep(0)）。
        async for evt_type, data in _stream_runtime_async(
            prompt, cwd, session_id=task.get("runtime_session_id") or "",
            event_type=task.get("event_type") or "",
            backend_name=_backend_for_engine(engine_name),
            account=task.get("account") or "", user_id=task.get("user_id") or 0,
            env_extra=env, bot_token=_get_task_token(task),
            user_token=user_token,
            agent_label=_agent_label,
            repo_key=task.get("repo_key") or "",
        ):
            if evt_type == "done":
                done = data
                continue
            # 流式引擎的用量(目前只有 codex)随事件带出，喂给采集器
            _usage_col.feed(evt_type, data)
            # 引擎确已开始执行（opencode 已 Popen 并产出首个执行事件）→ 才置 running。
            # 此前任务保持 dispatched（引擎启动失败/未执行不误标 running，见 event-redispatch-design.md §2.2）
            if not started and evt_type in ("step_start", "text", "tool_use"):
                await client.task_start(tid)
                started = True
            # 逐步日志（本地模式同款，供 worker 侧 journalctl 观察执行过程）；前缀含 seq 便于定位
            if evt_type == "step_start":
                log.info(f"  [{_esq}] ▶ {data}")
            elif evt_type == "text":
                log.info(f"  [{_esq}] 💬 {str(data)[:120]}")
            elif evt_type == "reasoning":
                log.info(f"  [{_esq}] 🤔 {str(data)[:120]}")
            elif evt_type == "tool_use":
                log.info(f"  [{_esq}] 🔧 {str(data)[:150]}")
            elif evt_type == "step_finish":
                log.info(f"  [{_esq}] ✅ {data}")
            batch.append({"type": evt_type, "data": data if not isinstance(data, str) else data})
            # Context compaction: compact batch if it grows too large (prevents context overflow)
            if should_compact(batch):
                batch = compact_messages(batch)
            # 双触发批量上报（实时日志实时性关键）：50 条或 500ms 时间窗任一满足即 flush
            _now = time.monotonic()
            if len(batch) >= 50 or (_last_flush and _now - _last_flush >= 0.5):
                _resp = await client.report_messages(tid, batch)
                if _resp is None:
                    # task 已被服务端标终态（410 Gone / 网络错误）→ 中断执行，杀引擎进程
                    log.warning(f"task {tid} report_messages returned None (task terminated?), aborting execution")
                    _kill_stale_engine(task, _backend_for_engine(engine_name))
                    _res.dispose()
                    _cleanup_task_workdir(task, env)
                    return {"_error": "task terminated by server (aborted during execution)", "_started": started}
                batch = []
                _last_flush = _now
        if batch:
            _resp = await client.report_messages(tid, batch)
            if _resp is None:
                log.warning(f"task {tid} final report_messages returned None (task terminated?), aborting")
                _kill_stale_engine(task, _backend_for_engine(engine_name))
                _res.dispose()
                _cleanup_task_workdir(task, env)
                return {"_error": "task terminated by server (aborted at final flush)", "_started": started}
        # Issue #242: coding agent 成功完成后，向关联仓库提交 PR（在 cleanup 删 worktree 之前执行）
        if done is not None and not (isinstance(done, dict) and done.get("_error")):
            try:
                _submit_linked_repo_prs(task, env)
            except Exception as _e:
                log.warning(f"linked PR submission failed for task {tid}: {_e}")
        # 可观测性：采集并上报 LLM 用量（引擎已退出，账本已落盘）
        await _report_llm_usage(client, tid, _usage_col, done)
        # done/complete/fail 由调用方（_wrap）在 timeout 外处理，防止网络重试超时覆盖完成状态
        _res.dispose()
        _cleanup_task_workdir(task, env)
        return done
    except CredentialRevokedError:
        # 凭据被吊销（revoke）——杀引擎进程，上抛让 _wrap/主循环退出 worker。
        # PR 提交跳过（GitCode token 虽可用，但凭据已吊销说明设备已被管理员移除，
        # 不应继续产出——避免"代码合入但服务端状态脱节"）。
        log.error(f"task {tid}: credential revoked during execution, killing engine and aborting")
        _kill_stale_engine(task, _backend_for_engine(engine_name))
        _res.dispose()
        _cleanup_task_workdir(task, env)
        raise
    except Exception as e:
        log.warning(f"task {tid} stream failed: {e}")
        try:      # 失败路径也尽力采一次（部分引擎失败前已产生真实消耗）
            await _report_llm_usage(client, tid, _usage_col, done)
        except Exception:
            pass
        _res.dispose()
        _cleanup_task_workdir(task, env)
        return {"_error": str(e), "_started": started}


def _usage_payload(u: dict, engine: str = "") -> dict:
    """把采集器的记录拍成 /tasks/{id}/usage 的请求体。

    source 记录"这条用量是怎么来的"（引擎账本 or 引擎直报），排查时能区分：
    codex/opencode/hermes/hwcloud；hermes 的辅助调用(如 title_generation)记成
    `hermes:title_generation`，因为它们是真实开销但需要与主任务区分。
    """
    def _n(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    suffix = str(u.get("_source_suffix") or "").strip()
    eng = (engine or "").strip().lower()
    if eng and suffix:
        src = f"{eng}:{suffix}"
    else:
        src = eng
    return {
        "provider": str(u.get("provider") or ""),
        "model": str(u.get("model") or ""),
        "input": _n(u.get("input")),
        "output": _n(u.get("output")),
        "cache_read": _n(u.get("cache_read")),
        "cache_write": _n(u.get("cache_write")),
        "reasoning": _n(u.get("reasoning")),
        "turn": _n(u.get("turn")),
        "cost_usd": u.get("cost_usd"),
        "cost_status": str(u.get("cost_status") or ""),
        "source": src,
    }


async def _report_llm_usage(client, tid: str, collector, done) -> None:
    """任务终态时采集并上报 LLM 用量台账（obs_llm_usage）。

    用量是观测数据、不是任务产物：采集失败/上报失败都只记日志，绝不影响任务终态。
    hermes 的 token 账本由后台合批线程异步落盘（空闲 30s 排干 + 进程退出 atexit 排干），
    理论上进程已退出即已落盘；仍留一次短重试兜住竞态，避免稳定少计。
    """
    try:
        sid = done.get("oc_session_id") if isinstance(done, dict) else ""
        usages = collector.finish(session_id=sid or "")
        if not usages and collector.engine in ("hermes", "hwcloud", "aishell"):
            await asyncio.sleep(1.5)
            usages = collector.finish(session_id=sid or "")
        if not usages:
            return
        sent = 0
        for u in usages:
            await client.report_usage(tid, _usage_payload(u, collector.engine))
            sent += 1
        log.info(f"llm usage collected task={tid} engine={collector.engine} "
                 f"records={len(usages)} sent={sent}")
    except Exception as e:
        log.warning(f"llm usage collect/report failed task={tid}: {e}")


async def _reregister(client, installed, device_name, cli_version: str = "") -> list[int] | None:
    """重新注册获取新 runtime id。先重置连接池清除 CLOSE-WAIT 死连接，再注册。

    凭据被吊销（401 → CredentialRevokedError）时直接上抛——调用方退出进程，
    不再当普通失败无限退避重试。
    """
    from src.runtime.client import CredentialRevokedError
    await client.reset_session()
    log.warning("re-registering with server to refresh runtime ids...")
    try:
        _ls = _scan_local_skills()
        ids, _probe = await client.register(installed, device_name=device_name, cli_version=cli_version, local_skills=_ls)
        if ids:
            for rid in ids:
                await client.recover_orphans(rid)
            log.info(f"re-registered successfully, new runtime ids: {ids}")
            return ids
        log.warning("re-register returned empty ids")
    except CredentialRevokedError:
        raise  # 凭据已吊销——重试无意义，上抛退出
    except Exception as e:
        log.warning(f"re-register failed: {e}")
    return None


_report_queue_obj = None


def _report_queue():
    """懒加载 worker 补报队列（Step 4，graceful-shutdown-requeue-design.md）。"""
    global _report_queue_obj
    if _report_queue_obj is None:
        from src.runtime.worker_report_queue import WorkerReportQueue
        _report_queue_obj = WorkerReportQueue()
    return _report_queue_obj


async def _report_replay_loop(client):
    """补报队列重放循环（Step 4）：周期重放 pending 补报（master 幂等+run_token 守卫）。"""
    _interval = float(os.environ.get("GRAPE_WORKER_REPORT_REPLAY_INTERVAL", "10"))
    while True:
        try:
            from src.runtime.worker_report_queue import replay_pending_reports
            q = _report_queue()
            if q.size():
                await replay_pending_reports(client, q)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"worker_report_queue: replay loop error: {e}")
        await asyncio.sleep(_interval)


async def _run_loop(client, runtime_ids, poll_interval, heartbeat_interval, max_concurrent,
                    engine_by_rid: dict | None = None, installed=None, device_name="",
                    _test_execute=None, _test_active=None, _test_active_tasks=None,
                    _test_active_task_objs=None):
    """主循环：claim → 执行 → 回报，含心跳 + 自动重连自修复。

    自修复机制（三层防护，见 worker 自修复重连设计）：
    1. 心跳失败计数：连续 N 次 heartbeat 失败 → 重注册
    2. 定期重注册：每 RE_REGISTER_INTERVAL 秒主动刷新 runtime id，
       防止服务端重启后 id 静默失效（服务端对旧 id 仍返回 200 ok）
    3. 网络中断恢复：连接错误计入心跳失败，触发重注册
    """
    active_sessions = _test_active if _test_active is not None else set()  # D9 串行槽：同 session 已有活跃任务，跳过（服务端 claim 已排除，防御双保险）
    active_tasks = _test_active_tasks if _test_active_tasks is not None else {}  # tid → sid 映射（task_id 去重 + sid 看门狗反查）
    active_task_objs = _test_active_task_objs if _test_active_task_objs is not None else {}  # tid → task dict（sid 看门狗杀进程需要 task 的 cwd/backend 信息）
    _requeue_cooldown = {}   # sid → 上次 requeue 时间戳，60s 冷却防 claim→requeue 空转死循环
    _sid_watchdog_last = 0.0 # 上次 sid 看门狗扫描时间
    hermes_active = 0         # hermes 引擎限并发 1（模型 API 分钟限流，实测并发双开 429，见 #53）
    last_heartbeat = 0.0

    # 启动 worker 引擎探测线程（每 10 分钟对本机引擎发起 CLI 测试对话）
    _start_worker_probe()
    # Step 4：补报队列重放后台任务（master 停机窗口完成/失败未上报，重连后自动补报）
    try:
        asyncio.create_task(_report_replay_loop(client), name="worker-report-replay")
    except Exception as e:
        log.warning(f"start report replay loop failed: {e}")
    heartbeat_fail_streak = 0
    HEARTBEAT_FAIL_THRESHOLD = 3
    RE_REGISTER_INTERVAL = float(os.environ.get("RUNTIME_REREGISTER_INTERVAL", "300"))
    last_reregister = asyncio.get_running_loop().time()
    engine_by_rid = engine_by_rid or {}

    # 自动升级状态
    pending_upgrade = False
    upgrade_target_version = ""
    upgrade_fail_count = 0
    last_upgrade_attempt = 0.0
    last_version_check = 0.0
    _local_ver_cached = ""
    _hb_session_just_reset = False  # B3: flag for version check after session reset
    UPGRADE_BACKOFF_BASE = float(os.environ.get("RUNTIME_UPGRADE_BACKOFF_BASE", "60"))
    UPGRADE_BACKOFF_MAX = float(os.environ.get("RUNTIME_UPGRADE_BACKOFF_MAX", "1800"))
    UPGRADE_MAX_RETRIES = int(os.environ.get("RUNTIME_UPGRADE_MAX_RETRIES", "5"))
    VERSION_CHECK_INTERVAL = float(os.environ.get("RUNTIME_VERSION_CHECK_INTERVAL", "3600"))

    while True:
        # ---- 定期重注册（防 id 静默失效）----
        now = asyncio.get_running_loop().time()
        if now - last_reregister >= RE_REGISTER_INTERVAL:
            new_ids = await _reregister(client, installed, device_name, cli_version=_read_local_worker_version())
            if new_ids:
                runtime_ids = new_ids
                heartbeat_fail_streak = 0
            last_reregister = asyncio.get_running_loop().time()

        # 同 worker 多引擎：随机打乱 rid 顺序，避免固定轮询导致排前面的引擎吃满、后面的引擎闲置
        _shuffled_rids = list(runtime_ids)
        random.shuffle(_shuffled_rids)
        for rid in _shuffled_rids:
            try:
                # Tags from server DB (single source of truth, managed via console)
                # claim_by_tag 和 claim 都返回完整 dict {task, scan_request}
                _claim_resp = None
                _db_tags = []
                try:
                    _db_tags = await client.get_tags(rid)
                except Exception:
                    _db_tags = []
                if _db_tags:
                    for _tag in _db_tags:
                        _resp = await client.claim_by_tag(rid, _tag,
                                                          supports_obs_url=_OBS_SUPPORT,
                                                          prefer_fallback=_OBS_FALLBACK)
                        if _resp and _resp.get("task"):
                            _claim_resp = _resp
                            break
                        # 无 task 但有 scan_request → 也要处理（空闲扫描）
                        if _resp and _resp.get("scan_request") and not _claim_resp:
                            _claim_resp = _resp
                if not _claim_resp or not _claim_resp.get("task"):
                    # Legacy claim (fallback: no tags or no tagged task)
                    _legacy_resp = await client.claim(rid, supports_obs_url=_OBS_SUPPORT,
                                              prefer_fallback=_OBS_FALLBACK)
                    if _legacy_resp and _legacy_resp.get("task"):
                        _claim_resp = _legacy_resp
                    elif _legacy_resp and _legacy_resp.get("scan_request") and not _claim_resp:
                        _claim_resp = _legacy_resp
            except Exception as e:
                log.warning(f"claim rid={rid} failed: {e}")
                heartbeat_fail_streak += 1
                continue
            # 处理 scan_request 指令（服务端通过 claim 响应下发）
            if _claim_resp and _claim_resp.get("scan_request"):
                try:
                    _engine_name = engine_by_rid.get(rid, "")
                    _engine_type = _backend_for_engine(_engine_name)
                    _skill_details = _scan_local_skills_with_detail(_engine_type)
                    await client.report_skills(rid, _skill_details)
                    log.info(f"skill scan completed for rid={rid}, reported {len(_skill_details)} skills")
                except Exception as _se:
                    log.warning(f"skill scan failed for rid={rid}: {_se}")
            task = (_claim_resp or {}).get("task") if _claim_resp else None
            if not task:
                continue
            tid = task.get("id", "")
            e = engine_by_rid.get(rid, "")  # 本任务引擎实例名（hermes 限并发判断用）
            # 先查 tid 再查 sid：同一任务已在跑时直接跳过（不 requeue），
            # 否则任务被 requeue 回 queued → 下轮又 claim 到 → sid 冲突 → 再 requeue →
            # 无限循环饿死同 runtime 后续任务（seq#1477 SRE 被 chat 任务死循环阻塞 20min）。
            if tid in active_tasks:
                log.debug(f"task {tid} already active, skip")
                continue
            # 并发槽 key 与服务端 claim 串行粒度一致（multi-issue-isolation-design.md §3.3）：
            # 有关联 Issue → 按 issue_session_id（跨 Issue 并行）；无（#0/旧任务）→ 回退 agent_session_id。
            # 修复：此前一律用 agent_session_id，导致不同 Issue 的任务在客户端被误判冲突拒收
            #       （服务端已放行并标记 dispatched，客户端却 skip → 任务悬空卡死，见 seq#622）。
            sid = task.get("issue_session_id") or task.get("agent_session_id") or ""
            if sid in active_sessions:
                # D9 串行槽：同 session 已有【另一个】活跃任务，跳过（服务端 claim 已排除，防御双保险）。
                # 回滚任务为 queued，避免 dispatched 悬空（skip 后无人执行也不重新排队）。
                # 冷却：同 sid 60s 内只 requeue 一次，避免每轮 poll(3s) 都 claim→requeue 空转
                # 饿死后面的任务（seq#1477 chat 任务空转 20min 阻塞 SRE）。
                _now = asyncio.get_running_loop().time()
                _last = _requeue_cooldown.get(sid, 0)
                if _now - _last < 60:
                    continue  # 冷却中：不 claim 不 requeue，留给后面的任务
                _requeue_cooldown[sid] = _now
                log.warning(f"claim conflict: session {sid} busy, requeue task {tid}")
                try:
                    await client.requeue(tid)
                except Exception as _e:
                    log.warning(f"requeue task {tid} failed: {_e}")
                continue
            if len(active_sessions) >= max_concurrent:
                continue
            if e == "hermes" and hermes_active >= 1:
                # hermes 引擎限并发 1（模型 API 分钟限流，双开实测 429 → 假完成，见 #53）
                continue
            active_sessions.add(sid)
            active_tasks[tid] = sid
            task["_engine_name"] = e  # 供 sid 看门狗杀进程用
            task["_claim_time"] = asyncio.get_running_loop().time()  # B2: zombie task age tracking
            active_task_objs[tid] = task
            if e == "hermes":
                hermes_active += 1

            async def _wrap(t=task, _tid=tid, s=sid, e=engine_by_rid.get(rid, "")):
                nonlocal hermes_active
                task_timeout = _env_float("RUNTIME_TASK_TIMEOUT", 3600)
                # 立即标记 running（不等引擎首个事件）：hermes chat -q 冷启动+模型响应
                # 常 >120s 才产出首个事件，保持 dispatched 会被服务端 120s 回收逻辑
                # 误判未执行 → 回收重跑循环（attempts 累加 3 次直接 failed，实测 #49 100042）。
                # 引擎启动失败由 fail_task 收尾（running → failed），无悬空风险。
                # task_start 重试（3次/1s间隔）：网络抖动时静默失败会导致任务留在 dispatched
                # 被服务端 300s 回收 → 重跑循环 → 最终"回收超限"failed（实测今天 11/29 失败因此）。
                _start_ok = False
                for _start_attempt in range(3):
                    try:
                        _start_result = await client.task_start(_tid)
                        if _start_result is not None:
                            _start_ok = True
                            break
                    except Exception as _se:
                        log.warning(f"task_start attempt {_start_attempt+1}/3 for {_tid} failed: {_se}")
                    if _start_attempt < 2:
                        await asyncio.sleep(1)
                if not _start_ok:
                    log.warning(f"task_start 3 attempts all failed for {_tid}, failing task to avoid dispatched stall")
                    try:
                        await client.fail(_tid, "task_start failed after 3 retries (network error)")
                    except Exception:
                        pass
                    return
                try:
                    if _test_execute is not None:
                        await _test_execute(client, t, e)
                    else:
                        # timeout 只保护流式执行阶段，complete/fail 在 timeout 外处理
                        _result = await asyncio.wait_for(_execute_task(client, t, e), timeout=task_timeout)
                except asyncio.TimeoutError:
                    log.warning(f"task {_tid} timed out after {task_timeout}s, killing child processes and failing")
                    _kill_stale_engine(t, _backend_for_engine(e))
                    _result = None  # timeout → 走 fail 路径
                except CredentialRevokedError:
                    # 凭据吊销——_execute_task 已杀引擎清理。置全局 Event 让主循环
                    # 下一轮退出进程（_wrap 是 create_task，异常不穿透主循环）。
                    log.error(f"task {_tid}: credential revoked, signaling main loop to exit")
                    _credential_revoked.set()
                    return
                # 在 timeout 外处理 complete/fail（网络重试不触发 timeout 覆盖完成状态）
                # Step 4：完成/失败先入补报队列（ms master 停机窗口不丢），ack 后出队
                _run_token = (t or {}).get("started_at") or ""

                async def _do_report(kind: str, text: str, exit_code: int, **kw):
                    q = _report_queue()
                    q.add({"task_id": _tid, "status": kind, "result_text": text,
                           "exit_code": exit_code, "run_token": _run_token,
                           "session_id": kw.get("session_id", ""),
                           "work_dir": kw.get("work_dir", ""),
                           "engine": kw.get("engine", e)})
                    try:
                        if kind == "failed":
                            resp = await client.fail(_tid, text, exit_code=exit_code, run_token=_run_token)
                        else:
                            resp = await client.complete(_tid, text, exit_code, run_token=_run_token, **kw)
                    except Exception:
                        return  # 保留补报队列，重连后由 replay 重放
                    if resp is not None:
                        q.remove(_tid)

                if _test_execute is None:
                    try:
                        if isinstance(_result, dict) and _result.get("_error"):
                            await _do_report("failed", _result["_error"], -1)
                        elif _result is None:
                            # 超时：透传负数 exit_code，服务端死 session 检测（连续3次负数）才能触发
                            await _do_report("failed", f"task timed out after {task_timeout}s", -1)
                        elif isinstance(_result, dict) and "exit_code" in _result:
                            _exit = _result.get("exit_code", 0)
                            _text = _result.get("text", "")
                            _errs = _result.get("errors") or []
                            # exit_code=0 + 有文本 → 成功（含中间 error 告警）
                            # exit_code=0 + 无文本 + 有 error → 失败（引擎吞错如 IAM 报错）
                            # exit_code!=0 + 无文本 → 失败（崩溃/超时/启动失败）
                            # exit_code!=0 + 有文本 → 交服务端 complete_task 判断（有结构化输出覆盖逻辑）
                            if _exit == 0 and not _text and _errs:
                                await _do_report("failed", f"engine errors: {'; '.join(_errs[:3])}", _exit)
                            elif _exit != 0 and not _text:
                                # seq#559 排障盲点：errors 空时 fail 消息只剩空壳
                                # （"engine exit_code=-1, errors: "），真实原因不可见。
                                # 兜底拼 stderr（引擎快速失败时常有启动报错）；
                                # 仍无线索时明确标注 errors 为空，提示查 worker 日志。
                                _stderr_hint = (_result.get("stderr") or "").strip()[:200]
                                _msg = f"engine exit_code={_exit}, errors: {'; '.join(_errs[:3])}"
                                if not _errs and _stderr_hint:
                                    _msg += f" | stderr: {_stderr_hint}"
                                elif not _errs:
                                    _msg += " (engine reported no error details; check worker logs)"
                                await _do_report("failed", _msg, _exit)
                            else:
                                await _do_report("completed", _text, _exit,
                                                 session_id=_result.get("oc_session_id", ""),
                                                 work_dir=t.get("cwd") or ".", engine=e)
                        else:
                            await _do_report("failed", "no done event", -1)
                    except Exception as _ce:
                        log.warning(f"task {_tid} complete/fail callback error: {_ce}")
                    # tmp cleanup handled by TaskResourceStack.dispose() in _execute_task
                active_sessions.discard(s)
                active_tasks.pop(_tid, None)
                active_task_objs.pop(_tid, None)
                if e == "hermes":
                    hermes_active -= 1

            asyncio.create_task(_wrap())

        # ---- 凭据吊销信号（_wrap 是独立 task，异常不穿透主循环——用 Event 传递）----
        if _credential_revoked.is_set():
            log.error("credential revoked by server (worker tasks aborted), exiting worker process")
            raise CredentialRevokedError("credential revoked")

        # ---- 心跳 + 失败检测 ----
        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= heartbeat_interval:
            # 收集最近的 ERROR/WARNING 日志（从内存日志缓冲区）
            _recent_logs = _collect_recent_logs()
            _metrics = _collect_worker_metrics()
            _probe_results = _collect_worker_probe_results()
            ok = await client.heartbeat(runtime_ids, log_lines=_recent_logs, metrics=_metrics, probe_results=_probe_results)
            # ⚠️ heartbeat 返回值从 bool 改为 dict——判断逻辑调整
            # dict: None=网络错误 / {"status":"ok",...}=成功 / 其他=异常
            if ok is not None and ok.get("status") == "ok":
                heartbeat_fail_streak = 0
                log.debug(f"heartbeat ok, ids={runtime_ids}")
                # 检查取消信号——发现 cancelled 立即杀引擎进程
                cancelled_tasks = ok.get("cancelled_tasks", [])
                if cancelled_tasks:
                    for _tid in cancelled_tasks:
                        if _tid in active_tasks:
                            _sid = active_tasks[_tid]
                            _task = active_task_objs.get(_tid)
                            log.warning(f"heartbeat: task {_tid} cancelled, killing engine")
                            if _task:
                                _kill_stale_engine(_task, _backend_for_engine(_task.get("_engine_name", "")))
                            active_sessions.discard(_sid)
                            active_tasks.pop(_tid, None)
                            active_task_objs.pop(_tid, None)
            else:
                heartbeat_fail_streak += 1
                if heartbeat_fail_streak >= HEARTBEAT_FAIL_THRESHOLD:
                    log.warning(f"heartbeat failed (streak={heartbeat_fail_streak}/{HEARTBEAT_FAIL_THRESHOLD}), resetting session")
                    await client.reset_session()
                    _hb_session_just_reset = True  # B3: trigger independent version check
                else:
                    log.debug(f"heartbeat failed (streak={heartbeat_fail_streak}/{HEARTBEAT_FAIL_THRESHOLD}), will retry")
            last_heartbeat = now

            if heartbeat_fail_streak >= HEARTBEAT_FAIL_THRESHOLD:
                log.warning(f"heartbeat failed {heartbeat_fail_streak} times in a row, triggering re-register")
                new_ids = await _reregister(client, installed, device_name, cli_version=_read_local_worker_version())
                if new_ids:
                    runtime_ids = new_ids
                    heartbeat_fail_streak = 0
                    last_reregister = asyncio.get_running_loop().time()
                else:
                    backoff = min(30 * (2 ** (heartbeat_fail_streak - HEARTBEAT_FAIL_THRESHOLD)), 300)
                    log.warning(f"re-register failed, backing off {backoff:.0f}s")
                    await asyncio.sleep(backoff)

            # ---- 版本对比 + 自动升级（每小时检查一次，不是每次心跳）----
            if ok is not None and ok.get("status") == "ok":
                _latest = ok.get("latest_version", "")
                if now - last_version_check >= VERSION_CHECK_INTERVAL or not _local_ver_cached:
                    last_version_check = now
                    if not _local_ver_cached:
                        _local_ver_cached = _read_local_worker_version()
                    _local_ver = _local_ver_cached
                    # B5: normalize old-format version strings for comparison
                    _latest_n = _normalize_worker_version(_latest)
                    _local_n = _normalize_worker_version(_local_ver)
                    if _latest_n and _latest_n != _local_n:
                        if not pending_upgrade:
                            log.info(f"Version mismatch: local={_local_ver} "
                                     f"server={_latest}, will upgrade when idle")
                        pending_upgrade = True
                        upgrade_target_version = _latest
                    elif _latest_n:
                        if pending_upgrade and _latest_n == _local_n:
                            pending_upgrade = False
                            log.info("Version now matches, clearing pending_upgrade")
                    else:
                        # Server disabled auto-upgrade (empty latest_version):
                        # clear any stale pending flag so worker stops retrying
                        # downloads forever.
                        if pending_upgrade:
                            pending_upgrade = False
                            log.info("Auto-upgrade disabled by server (empty latest_version), "
                                     "clearing pending_upgrade")
            elif _hb_session_just_reset:
                # B3 (Issue #124): heartbeat was failing (aiohttp session broken) —
                # version check was only in the success branch, causing a deadlock:
                # session broken → no heartbeat → no version check → no upgrade →
                # stale session never recovers. After reset_session(), use the
                # independent latest_version() GET endpoint to check version so the
                # worker can still sense new versions and trigger upgrade.
                _hb_session_just_reset = False
                if now - last_version_check >= VERSION_CHECK_INTERVAL or not _local_ver_cached:
                    last_version_check = now
                    if not _local_ver_cached:
                        _local_ver_cached = _read_local_worker_version()
                    _local_ver = _local_ver_cached
                    try:
                        _latest = await client.latest_version()
                    except CredentialRevokedError:
                        raise  # 凭据吊销——穿透退出
                    except Exception:
                        _latest = ""
                    _latest_n = _normalize_worker_version(_latest)
                    _local_n = _normalize_worker_version(_local_ver)
                    if _latest_n and _latest_n != _local_n:
                        if not pending_upgrade:
                            log.info(f"Version mismatch (heartbeat failing, via latest_version): "
                                     f"local={_local_ver} server={_latest}, will upgrade when idle")
                        pending_upgrade = True
                        upgrade_target_version = _latest
                    elif _latest_n and pending_upgrade and _latest_n == _local_n:
                        pending_upgrade = False
                        log.info("Version now matches (heartbeat failing), clearing pending_upgrade")

        # ---- B2 (Issue #124): 僵尸任务超龄摘除，防 dispatched 任务永久阻塞升级 ----
        # 长时间 dispatched 的任务（引擎进程 hang 住，_wrap 的 finally 不执行）会
        # 让 active_tasks 永不空 → "空闲才升级" 条件永不满足。超龄阈值后查服务端
        # 终态，已终态→直接摘除；未终态但超龄→也摘除并回调 fail（任务实际已卡死）。
        _zombie_threshold = _env_float("RUNTIME_ZOMBIE_TASK_THRESHOLD", 6 * 3600)
        if active_tasks and pending_upgrade:
            _znow = asyncio.get_running_loop().time()
            _zombie_tids = []
            for _ztid, _zsid in list(active_tasks.items()):
                _zt = active_task_objs.get(_ztid)
                if not _zt:
                    continue
                _claim_t = _zt.get("_claim_time", 0)
                if _claim_t and (_znow - _claim_t) >= _zombie_threshold:
                    # 超龄——查服务端终态
                    try:
                        _zstate = await client.get_task_state(_ztid)
                    except Exception:
                        _zstate = ""
                    if _zstate in ("failed", "cancelled", "completed", "not_found"):
                        _zombie_tids.append((_ztid, _zsid, _zstate, True))
                    else:
                        # 服务端未终态但 worker 端已超龄→任务卡死，摘除并回调 fail
                        _zombie_tids.append((_ztid, _zsid, _zstate or "unknown", False))
            for _ztid, _zsid, _zstate, _is_terminal in _zombie_tids:
                active_sessions.discard(_zsid)
                active_tasks.pop(_ztid, None)
                _zt = active_task_objs.pop(_ztid, None)
                if _zt:
                    _backend = _backend_for_engine(_zt.get("_engine_name") or "")
                    try:
                        _kill_stale_engine(_zt, _backend)
                    except Exception:
                        pass
                if not _is_terminal:
                    try:
                        await client.fail(_ztid, f"zombie task reclaimed (age>{_zombie_threshold:.0f}s, server_state={_zstate})")
                    except Exception:
                        pass
                log.warning(f"zombie reclamation: tid={_ztid} sid={_zsid} state={_zstate} "
                            f"terminal={_is_terminal}, removed from active_tasks")

        # ---- 升级执行（待升级 && 无活跃任务 && 退避已过）----
        if pending_upgrade and not active_tasks:
            _now = asyncio.get_running_loop().time()
            _backoff = min(UPGRADE_BACKOFF_BASE * (2 ** upgrade_fail_count), UPGRADE_BACKOFF_MAX)
            if _now - last_upgrade_attempt >= _backoff:
                last_upgrade_attempt = _now
                # 随机延迟 0~60s：多 worker 同时检测到版本差异时错开下载时机
                # 避免所有 worker 同时下载 35MB 包导致主控带宽瞬时占满
                if upgrade_fail_count == 0:
                    import random as _rand
                    _jitter = _rand.uniform(0, 60)
                    log.info(f"Auto-upgrade to {upgrade_target_version} "
                             f"(attempt {upgrade_fail_count + 1}), "
                             f"jitter {_jitter:.0f}s to stagger downloads")
                    await asyncio.sleep(_jitter)
                else:
                    log.info(f"Starting auto-upgrade to {upgrade_target_version} "
                             f"(attempt {upgrade_fail_count + 1})")
                _upgrade_ok = await _perform_upgrade(
                    os.environ.get("RUNTIME_SERVER_URL", ""),
                    os.environ.get("RUNTIME_TOKEN", "")
                )
                if not _upgrade_ok:
                    upgrade_fail_count += 1
                    if upgrade_fail_count >= UPGRADE_MAX_RETRIES:
                        # 超过最大重试次数 → 放弃升级，等下次版本检查重新触发
                        pending_upgrade = False
                        log.warning(f"Auto-upgrade gave up after {upgrade_fail_count} failures, "
                                    f"will retry on next version check ({VERSION_CHECK_INTERVAL:.0f}s)")
                    else:
                        # 保持 pending_upgrade = True，用指数退避控制重试频率
                        _next = min(UPGRADE_BACKOFF_BASE * (2 ** upgrade_fail_count), UPGRADE_BACKOFF_MAX)
                        log.warning(f"Auto-upgrade failed (count={upgrade_fail_count}/{UPGRADE_MAX_RETRIES}), "
                                    f"will retry in {_next:.0f}s")

        # ---- sid 看门狗：清理幽灵 sid ----
        # active_tasks 里的任务如果已在服务端终态（failed/cancelled/completed/not_found），
        # 说明 _wrap 的 finally 没执行（opencode 进程 hang 住），sid 泄漏在 active_sessions。
        # 定期查服务端状态，主动清理，防同 sid 后续任务被假冲突挡住（seq#1485 根因）。
        _watchdog_interval = _env_float("RUNTIME_SID_WATCHDOG_INTERVAL", 60)
        if active_tasks and now - _sid_watchdog_last >= _watchdog_interval:
            _sid_watchdog_last = now
            _ghost_tids = []
            for _tid, _sid in list(active_tasks.items()):
                try:
                    _state = await client.get_task_state(_tid)
                except Exception:
                    _state = ""
                if _state in ("failed", "cancelled", "completed", "not_found", "") and _state:
                    _ghost_tids.append((_tid, _sid, _state))
                elif not _state:
                    # 查询失败（网络抖动/服务端 500）：跳过，下次再查
                    pass
            for _tid, _sid, _state in _ghost_tids:
                active_sessions.discard(_sid)
                active_tasks.pop(_tid, None)
                _task = active_task_objs.pop(_tid, None)
                # 杀掉残留引擎进程：用户终止/超时后服务端已标终态，但 worker 上的
                # 引擎进程可能仍在跑（没有服务端→worker 的推送通道通知停进程）。
                # 看门狗发现 task 已终态时主动杀进程，防残留进程继续产出消息/调 complete。
                if _task:
                    _backend = _backend_for_engine(_task.get("_engine_name") or "")
                    try:
                        _kill_stale_engine(_task, _backend)
                        log.warning(f"sid watchdog: killed engine for ghost tid={_tid} sid={_sid} (server status={_state}, backend={_backend})")
                    except Exception as _ke:
                        log.warning(f"sid watchdog: kill engine failed for tid={_tid}: {_ke}")
                else:
                    log.warning(f"sid watchdog: cleaned ghost tid={_tid} sid={_sid} (server status={_state}, no task obj for kill)")

        await asyncio.sleep(poll_interval + random.uniform(0, poll_interval * 0.3))


def _read_local_worker_version() -> str:
    """读取本机 Worker 安装包版本（install-worker.sh / .ps1 写入）。

    路径与安装脚本的 VERSION_FILE 保持一致：
    - root 模式（uid=0 或 RUNTIME_ROOT_MODE=1）：/etc/grape/worker.version
    - 非 root 模式：$HOME/.grape/worker.version
    - Windows：$HOME/.grape/worker.version（Windows 无 root 模式）

    兜底：戳文件缺失（手工部署 / 写戳失败）时退回读安装包自带的 VERSION。
    没有兜底的话本地版本恒为 ""，心跳每次都比对失败 → 每个检查周期都触发一次升级，
    表现为"升级循环"（下载 35MB 包 + 重启 worker）。戳文件在位时行为不变。
    （2026-10-21 恢复：fbc499c4a 误删，67b11da14 为 2026-10-18 worker 升级死循环事故加的防线。）
    """
    from src.runtime.os_adapter import is_windows, version_file
    if is_windows():
        vp = version_file()
    elif os.environ.get("RUNTIME_ROOT_MODE") == "1" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        vp = "/etc/grape/worker.version"
    else:
        vp = os.path.join(os.path.expanduser("~"), ".grape", "worker.version")
    try:
        with open(vp, "r", encoding="utf-8") as f:
            got = f.read().strip()
        if got:
            return got
    except Exception:
        pass
    # 兜底：包内 VERSION（worker 以安装目录为 cwd 运行）
    for cand in (os.path.join(os.getcwd(), "VERSION"),):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                got = f.read().strip()
            if got:
                log.warning(f"worker.version missing/empty, falling back to {cand}: {got}")
                return got
        except Exception:
            continue
    return ""


def _normalize_worker_version(ver: str) -> str:
    """归一化 worker 版本串，兼容老格式（Issue #257 B5）。

    老格式版本串形如 h-{manifest16}-w{wheels}-p{python}，新格式为 h-{manifest12}。
    剥离 -w/-p 后缀并截取指纹主体到 12 位，使老格式版本能与新格式正确比对，
    避免「代码相同但版本串格式不同」导致的无限升级循环。
    """
    if not ver or not isinstance(ver, str):
        return ""
    ver = ver.strip()
    ver = re.split(r'-w[a-f0-9]+-p[a-f0-9]+', ver, maxsplit=1)[0]
    if ver.startswith("h-") and len(ver) > 14:
        ver = "h-" + ver[2:14]
    return ver


async def _perform_upgrade(server: str, token: str) -> bool:
    """下载并执行安装脚本升级 worker（跨平台）。

    成功时 worker 被安装脚本重启（本进程被杀，不会返回）。
    失败时返回 False，worker 继续运行当前版本。

    Linux: 下载 install-worker.sh → bash 执行
    Windows: 下载 install-worker.ps1 → PowerShell 执行
    """
    import subprocess, tempfile, sys as _sys
    if not server or not token:
        log.error("Auto-upgrade: missing server or token, skipping upgrade")
        return False

    is_win = _sys.platform == "win32"
    if is_win:
        install_script_url = f"{server.rstrip('/')}/api/runtime/install.ps1"
        suffix = ".ps1"
    else:
        install_script_url = f"{server.rstrip('/')}/api/runtime/install"
        suffix = ".sh"

    fd, tmp_script = tempfile.mkstemp(suffix=suffix)
    try:
        os.close(fd)

        if is_win:
            # Windows: 用 PowerShell 下载（Invoke-WebRequest 比 irm 更可靠）
            r = await asyncio.to_thread(
                subprocess.run,
                ["powershell", "-NoProfile", "-Command",
                 f"Invoke-WebRequest -Uri '{install_script_url}' -OutFile '{tmp_script}' -UseBasicParsing"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30
            )
            # Windows PowerShell 5.1 默认用 GBK 读 .ps1 文件，中文注释乱码导致语法错误
            # 下载后检测并补 UTF-8 BOM
            try:
                with open(tmp_script, "rb") as f:
                    raw = f.read()
                if not raw.startswith(b"\xef\xbb\xbf"):
                    with open(tmp_script, "wb") as f:
                        f.write(b"\xef\xbb\xbf" + raw)
            except Exception:
                pass
        else:
            r = await asyncio.to_thread(
                subprocess.run,
                ["curl", "-fsSL", "--connect-timeout", "15", install_script_url, "-o", tmp_script],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30
            )
        if r.returncode != 0:
            log.error(f"Auto-upgrade: download install script failed: {r.stderr[-300:]}")
            return False

        _install_dir = os.environ.get("RUNTIME_INSTALL_DIR", "")
        if not _install_dir:
            _here = os.path.dirname(os.path.abspath(__file__))
            _install_dir = os.path.dirname(os.path.dirname(_here))

        _upgrade_env = dict(os.environ)
        _upgrade_env["TOKEN"] = token

        if is_win:
            log.info(f"Auto-upgrade: running install-worker.ps1 (dir={_install_dir})")
            result = await asyncio.to_thread(
                subprocess.run,
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp_script,
                 "-Token", token, "-Server", server, "-Dir", _install_dir],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600, env=_upgrade_env
            )
        else:
            log.info(f"Auto-upgrade: running install-worker.sh (dir={_install_dir})")
            result = await asyncio.to_thread(
                subprocess.run,
                ["bash", tmp_script, "--server", server, "--dir", _install_dir],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600, env=_upgrade_env
            )

        if result.returncode == 0:
            log.info(f"Auto-upgrade: install script succeeded (rc=0), waiting for restart")
            return True
        log.error(f"Auto-upgrade: install script failed rc={result.returncode}: stderr={result.stderr[-500:]}")
        return False
    except subprocess.TimeoutExpired:
        log.error("Auto-upgrade: install script timed out (600s)")
        return False
    except Exception as e:
        log.error(f"Auto-upgrade failed: {e}")
        return False
    finally:
        try:
            if os.path.exists(tmp_script):
                os.unlink(tmp_script)
        except Exception:
            pass


def _check_opencode_api_keys():
    """启动时检测 opencode.json 中的 {env:XXX} 引用，若环境变量缺失则告警。

    opencode provider 配置 apiKey 为 "{env:VAR_NAME}" 时，opencode 运行时
    从子进程环境变量解析 VAR_NAME。worker 以 systemd/nohup 运行时环境变量
    只来自 /etc/grape/runtime.env，若该文件缺少对应 key → 每个模型调用返回
    "Unauthorized: API key is required"，任务 3 秒即 failed 且无 stderr。

    本函数在 main() 启动早期运行，发现缺失时输出醒目告警和修复指引，
    让运维在任务失败前发现问题（2026-08-28 事故：5 台 worker 全缺 OPENGW key）。
    """
    oc_path = os.path.expanduser("~/.config/opencode/opencode.json")
    if not os.path.isfile(oc_path):
        return
    try:
        with open(oc_path, encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return
    # 匹配 {env:VAR_NAME} 模式
    env_refs = set(re.findall(r'\{env:([A-Z_][A-Z0-9_]*)\}', raw))
    if not env_refs:
        return  # opencode.json 用的是明文 key，不需要环境变量
    missing = [name for name in env_refs if not os.environ.get(name)]
    if not missing:
        log.info(f"opencode.json API key 环境变量检查通过: {', '.join(sorted(env_refs))}")
        return
    log.warning(
        f"⚠ opencode.json 引用了 {len(missing)} 个环境变量但 runtime.env 中缺失: "
        f"{', '.join(sorted(missing))}"
    )
    log.warning(
        "  所有模型调用将返回 'Unauthorized: API key is required'，任务会秒级失败。"
    )
    log.warning(
        "  修复方法（任选一）:"
    )
    from src.runtime.os_adapter import is_windows
    if is_windows():
        log.warning(
            "  ① 追加 key 到 runtime.env: "
            f"Add-Content $HOME\\.grape\\runtime.env '{missing[0]}=<your-key>'; restart-worker"
        )
        log.warning(
            "  ② 重装时传入 key: $env:" + "='xxx'; $env:".join(missing) +
            "='xxx'; irm <server>/api/runtime/install.ps1 | iex"
        )
    else:
        log.warning(
            "  ① 追加 key 到 runtime.env: "
            f"echo '{missing[0]}=<your-key>' >> /etc/grape/runtime.env && systemctl restart runtime-agent"
        )
        log.warning(
            "  ② 重装时传入 key: export " + "=xxx ".join(missing) +
            "=xxx && curl -sSL <server>/api/runtime/install | bash -s -- --token <token>"
        )


def main():
    # Windows: 确保 stderr/stdout 使用 UTF-8 编码（避免 GBK 中文乱码）
    if sys.platform == "win32":
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 单实例保护：杀掉已运行的旧 worker 进程，避免双进程竞争 claim
    from src.runtime.os_adapter import kill_existing_worker, pid_file, config_dir
    try:
        _killed = kill_existing_worker()
        if _killed:
            log.warning("Killed existing worker process (single-instance protection)")
    except Exception as _ke:
        log.warning(f"kill_existing_worker failed: {_ke}")
    # 写入当前进程 PID
    try:
        _cfg_dir = config_dir()
        os.makedirs(_cfg_dir, exist_ok=True)
        with open(pid_file(), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    token = os.environ.get("RUNTIME_TOKEN", "")
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    poll = _env_float("RUNTIME_POLL_INTERVAL", 3)
    heartbeat = _env_float("RUNTIME_HEARTBEAT_INTERVAL", 15)
    max_concurrent = _env_int("RUNTIME_MAX_CONCURRENT_TASKS", 20)

    if not token:
        print("RUNTIME_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    # 启动早期检测 opencode API key 环境变量缺失（2026-08-28 事故预防）
    _check_opencode_api_keys()

    # 清理上次崩溃残留的 codearts 临时目录（方案 G: per-task 独立数据目录）
    import shutil as _sh
    from src.runtime.os_adapter import is_windows, codearts_tasks_dir as _os_codearts_dir
    _codearts_tasks_dir = os.environ.get("CODEARTS_TASKS_DIR", _os_codearts_dir())
    if is_windows():
        # Windows: 直接使用 LOCALAPPDATA 下的目录，无需权限回退
        try:
            os.makedirs(_codearts_tasks_dir, exist_ok=True)
            log.info(f"created codearts tasks dir: {_codearts_tasks_dir}")
        except Exception as e:
            _codearts_tasks_dir = os.path.join(os.path.expanduser("~"), ".cache", "codearts-tasks")
            os.makedirs(_codearts_tasks_dir, exist_ok=True)
            log.info(f"codearts tasks dir fallback: {_codearts_tasks_dir}")
    else:
        # fallback：如果 /var/cache 不可写，改用用户目录
        try:
            if not os.path.isdir(_codearts_tasks_dir):
                os.makedirs(_codearts_tasks_dir, exist_ok=True)
                try:
                    os.chmod(_codearts_tasks_dir, 0o1777)
                except PermissionError:
                    pass
                log.info(f"created codearts tasks dir: {_codearts_tasks_dir}")
        except PermissionError:
            _codearts_tasks_dir = os.path.expanduser("~/.cache/codearts-tasks")
            os.makedirs(_codearts_tasks_dir, exist_ok=True)
            log.info(f"codearts tasks dir fallback (no /var/cache access): {_codearts_tasks_dir}")

    from src.runtime.client import RuntimeClient

    async def _amain():
        client = RuntimeClient(server, token)

        # 版本自检（worker-server-delivery-design.md §2.4）：服务端包版本不一致时提示升级
        # B5: 使用归一化比对，兼容老格式版本串
        try:
            latest = await client.latest_version()
            local = _read_local_worker_version()
            if latest and local and _normalize_worker_version(latest) != _normalize_worker_version(local):
                from src.runtime.os_adapter import is_windows
                if is_windows():
                    log.warning(f"Worker 包版本不一致: 本地={local} 服务端={latest}，"
                                f"请重新执行一键安装命令升级: irm {server}/api/runtime/install.ps1 | iex")
                else:
                    log.warning(f"Worker 包版本不一致: 本地={local} 服务端={latest}，"
                                f"请重新执行一键安装命令升级: curl -sSL {server}/api/runtime/install | bash -s -- --token <token>")
        except Exception as e:
            log.warning(f"Worker 版本自检失败: {e}")

        runtimes = _discover_local_runtimes()
        installed = [r for r in runtimes if r.get("installed")]
        if not installed:
            log.warning("No runtimes installed locally, registering empty")
        device = os.environ.get("RUNTIME_DEVICE_NAME", "")
        if not device:
            from src.runtime.os_adapter import hostname as _hostname
            device = _hostname()
        _local_skills = _scan_local_skills()
        try:
            ids, probe_url = await client.register(installed, device_name=device,
                                                   cli_version=_read_local_worker_version(),
                                                   local_skills=_local_skills)
        except CredentialRevokedError:
            print("RUNTIME_TOKEN rejected (revoked or invalid), worker exiting. "
                  "Reinstall with a new credential to rejoin.", file=sys.stderr)
            log.error("register 401: credential revoked/invalid, exiting (code 42)")
            raise SystemExit(42)
        if not ids:
            log.error("Register failed (check RUNTIME_TOKEN / server URL)")
            return
        # v3.0 启动探测（B）：GET probe_url 验证完整链路（网络+签名+读取）
        global _OBS_SUPPORT
        if probe_url:
            try:
                with _urllib.urlopen(probe_url, timeout=5) as _r:
                    _OBS_SUPPORT = bool(_r.read())
            except Exception as e:
                log.warning(f"[skill] 启动探测失败（走 skill_files 兜底）: {type(e).__name__}")
        log.info(f"OBS 直连能力探测: supports_obs_url={_OBS_SUPPORT}")
        # rid → 引擎实例名（`{type}-{device}`，register 返回顺序与 installed 一致）
        from src.runtime.instances import instance_name
        engine_by_rid = {rid: instance_name(r.get("name", ""), device, 0)
                         for rid, r in zip(ids, installed)}
        log.info(f"RuntimeAgent registered: {ids} -> {server}, engines={list(engine_by_rid.values())}, "
                 f"poll={poll}s heartbeat={heartbeat}s max_concurrent={max_concurrent}")
        for rid in ids:
            await client.recover_orphans(rid)
        # 注册成功后立即执行一轮引擎探测（线程池，不阻塞事件循环），
        # 确保第一次心跳上报时 probe_results 非空，使服务端及时纠正 healthy（ok→升级 / fail→降级）。
        await asyncio.to_thread(_worker_probe_once)

        # sweeper：worker 端定期清理本地磁盘残留（worktree/node_modules/tmp）。
        # 服务端的 sweep_loop 只扫服务端磁盘，远端 worker 的残留无人清理 → 撑满磁盘。
        # worker 启动时也跑一轮 sweep_once（清上次崩溃残留），然后每 15min 循环。
        _worker_sweep_task = None
        try:
            from src.runtime.cleanup import sweep_loop
            _worker_sweep_task = asyncio.create_task(sweep_loop())
            log.info("Worker sweeper loop started")
        except Exception as e:
            log.warning(f"Worker sweeper loop start failed: {e}")

        try:
            await _run_loop(client, ids, poll, heartbeat, max_concurrent, engine_by_rid,
                            installed=installed, device_name=device)
        except CredentialRevokedError:
            # 凭据被主控吊销（revoke）——正常退出而非无限重试：
            # 心跳 401 / 任务执行中 401 都会走到这里。
            # 退出码 42 区分于崩溃退出（daemon/看门狗可据此决定是否重启）。
            print("RUNTIME_TOKEN revoked by server, worker exiting. "
                  "Reinstall with a new credential to rejoin.", file=sys.stderr)
            log.error("credential revoked by server, worker exiting (code 42)")
            raise SystemExit(42)
        finally:
            if _worker_sweep_task and not _worker_sweep_task.done():
                _worker_sweep_task.cancel()
            await client.close()

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
