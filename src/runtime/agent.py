
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
    "dsh": ["dsh", "headless", "hello"],
}
_PROBE_TIMEOUT = 30
_ACP_PROBE_TIMEOUT = 15  # hermes ACP initialize 超时
_ACP_PROMPT_TIMEOUT = 30  # hermes ACP prompt 超时（模型推理需要更长时间）
# Issue #423: _read_response 防御性行数上限（保险丝，防失控流）。真正终止条件是 deadline；
# 会话通知量是动态值（三步探活实测 prompt 响应前 36 行），行数上限不得与其耦合。
_ACP_READ_MAX_LINES = 500

# ===== Windows ACP 探测兼容层（os.set_blocking 为 POSIX-only；Windows 管道不支持 select）=====
# 探测原有实现：fd 置非阻塞 + select + os.read。这两个能力在 Windows 上都不存在
# （os.set_blocking 属性缺失 / select.select 仅支持 socket），导致 hermes/hwcloud
# ACP 探活必然 AttributeError 立即失败 → 引擎被判 unhealthy，服务端不派任务。
# 兼容层：后台读线程把字节推进队列，_read_response 侧带 deadline 消费，语义与原实现一致。
_win_acp_pumps: dict = {}


def _win_acp_queue(proc):
    """按进程惰性启动读线程，返回字节队列（EOF 时投递 b"" 哨兵）。"""
    import queue as _queue
    import threading as _threading

    key = id(proc)
    q = _win_acp_pumps.get(key)
    if q is not None:
        return q
    q = _queue.Queue()
    _win_acp_pumps[key] = q

    def _pump():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                q.put(chunk)
        except Exception:
            pass
        finally:
            q.put(b"")

    _threading.Thread(target=_pump, daemon=True, name="acp-probe-pump").start()
    return q


def _win_acp_read_response(proc, send, buf, req_id: int, timeout: float):
    """JSON-RPC 行读取（Windows 版）：deadline 驱动 + 队列消费，语义对齐 POSIX 实现。"""
    import json as _json
    import queue as _queue

    q = _win_acp_queue(proc)
    deadline = _time_mod.time() + timeout
    _lines = 0
    while _time_mod.time() < deadline and _lines < _ACP_READ_MAX_LINES:
        _remaining = max(deadline - _time_mod.time(), 0.0)
        try:
            _chunk = q.get(timeout=_remaining) if _remaining > 0 else q.get_nowait()
        except _queue.Empty:
            if proc.poll() is not None:
                return None
            continue
        if not _chunk:
            return None  # EOF
        buf.extend(_chunk)
        while buf:
            _nl = buf.find(b"\n")
            if _nl < 0:
                if _time_mod.time() >= deadline:
                    return None
                break
            _raw = bytes(buf[:_nl])
            del buf[:_nl + 1]
            _lines += 1
            _line = _raw.strip().decode("utf-8", errors="replace")
            if not _line.startswith("{"):
                continue
            try:
                _msg = _json.loads(_line)
            except _json.JSONDecodeError:
                continue
            if "id" not in _msg or "method" in _msg:
                if "method" in _msg and "id" in _msg:
                    try:
                        send({"jsonrpc": "2.0", "id": _msg["id"], "result": {"optionId": ""}})
                    except Exception:
                        pass
                continue
            if _msg.get("id") == req_id and ("result" in _msg or "error" in _msg):
                return _msg
    return None


_win_acp_stderr: dict = {}


def _win_acp_drain_stderr(proc):
    """Windows：hermes/hwcloud ACP 启动时向 stderr 大量刷日志，管道缓冲（≈4KB）写满后
    子进程阻塞在 write，探针再也等不到响应（实测 session/new 15s 超时、initialize 正常）。
    Linux 下 PIPE 缓冲大且不影响本次探活，Windows 必须后台抽干。仅保留尾部 16KB 供诊断。"""
    import threading as _threading

    key = id(proc)
    buf = _win_acp_stderr.get(key)
    if buf is not None:
        return buf
    buf = bytearray()
    _win_acp_stderr[key] = buf

    def _drain():
        try:
            while True:
                chunk = proc.stderr.read(65536)
                if not chunk:
                    return
                buf.extend(chunk)
                if len(buf) > 16384:
                    del buf[:-16384]
        except Exception:
            return

    _threading.Thread(target=_drain, daemon=True, name="acp-probe-stderr").start()
    return buf


_PROBE_INTERVAL = int(os.environ.get("RUNTIME_PROBE_INTERVAL", "600"))  # 引擎探测间隔，默认 600s（10 分钟），可通过 runtime.env 配置

# Issue #378: 按 agent_type 差异化静默超时阈值（coding/qa 放宽到 1200s，其余默认 600s）。
# 在 _execute_task 中以 setdefault 语义注入 env，底层各 backend 读 env 生效：
#   - executor.py       _silent_window  读 AGENT_SILENT_WINDOW
#   - hermes_acp.py     _SILENT_TIMEOUT 读 HERMES_SILENT_TIMEOUT
#   - hwcloud_acp.py    _SILENT_TIMEOUT 读 RUNTIME_TOOL_SILENT_TIMEOUT（已支持）
# coding/QA 长任务（编译/构建/PR 审查）常长时间无流式输出，600s 对 coding/QA 长任务仍偏短，会在正常进行中误杀。
_AGENT_SILENT_TIMEOUTS = {"coding": 1200, "qa": 1200}
_DEFAULT_SILENT_WINDOW = 600

_worker_url_probe_cache: dict = {}  # {(rts, task_server_url): (ts, reachable)}
_worker_url_probe_lock = _threading.Lock()
_worker_probe_results: dict = {}  # {engine_name: {status, latency, last_probe, error}}
_worker_probe_lock = _threading.Lock()
_worker_probe_thread = None
_worker_probe_started = False
# Issue #490：已成功发送的探针观测指纹 {engine_name: last_probe}。
# 心跳成功后写回；未变化的观测（last_probe 相同）不再重复发送，避免服务端重放计数。
_last_sent_probe_fingerprint: dict = {}


def _probe_hermes_acp(cli_path: str) -> dict:
    """hermes ACP 模式探活：initialize + session/new + 轻量 prompt，端到端验证引擎可用性。

    1. 启动 hermes acp 子进程（和实际任务执行路径一致）
    2. 发 JSON-RPC initialize → 验证 ACP 协议层
    3. 发 session/new → 获取真实 sessionId（Issue #441：对空/不存在的
       sessionId，hermes ACP 服务端 prompt 直接返回 stopReason=refusal，
       导致升级 #423 口径后监控全量 refused；对齐 _probe_hwcloud_acp 先例）
    4. 发 session/prompt "hi"（真实 sessionId）→ 验证 provider 连接 + 模型推理
    收到 stopReason 响应即表示端到端可用。
    """
    import json as _json
    import shutil as _shutil

    agent_cmd = cli_path or _shutil.which("hermes") or _shutil.which("hermes.cmd")
    if not agent_cmd:
        return {"status": "fail", "latency": 0, "error": "hermes CLI not found"}

    start = _time_mod.time()
    proc = None
    _probe_cwd = ""  # session/new 的临时 cwd（try 内赋值，finally 清理需判空）
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
        if os.name == "nt":
            _win_acp_drain_stderr(proc)

        import select as _sel

        def _send(msg: dict):
            proc.stdin.write((_json.dumps(msg) + "\n").encode("utf-8"))
            proc.stdin.flush()

        # QA 实测复核（Optional#1 升级）：select 就绪后 readline() 遇「半行未换行数据」
        # 会阻塞在 read 系统调用——deadline 只约束 select、管不到 readline（实测
        # deadline=2s 时探活 >10s 不返回，finally 兜底也执行不到，探测线程永久停摆）。
        # 修复：fd 置非阻塞（os.set_blocking）+ os.read 小块读 + 行缓冲拼行，
        # 每轮 select/read 后都重检 deadline。（用 os.set_blocking 而非 os.fcntl：
        # 部分 Python 构建无 os.fcntl 顶层别名，set_blocking 为标准可移植接口。）
        _fd = proc.stdout.fileno()
        if os.name != "nt":
            os.set_blocking(_fd, False)
        _buf = bytearray()

        def _read_response(req_id: int, timeout: float) -> dict | None:
            """读 JSON-RPC 行直到匹配到指定 id 的 response（Issue #423：deadline 驱动）。

            终止条件以 deadline 为主：三步探活会话中 prompt 响应前 agent 会持续输出
            session/update 通知（实测 36 行），固定 10 行上限会在通知流中提前耗尽、
            误判「无响应」。行数仅作防御性上限（_ACP_READ_MAX_LINES，防失控流）。
            select 超时（deadline 耗尽）与 EOF 立即返回 None。
            非阻塞 fd + os.read 小块读 + 行缓冲：半行数据不会卡住 read 系统调用，
            deadline 在每一轮 select 与 read 之后都被重新检查。
            """
            # Windows：无 os.set_blocking，且 select 不支持管道 → 读线程 + 队列
            if os.name == "nt":
                return _win_acp_read_response(proc, _send, _buf, req_id, timeout)
            deadline = _time_mod.time() + timeout
            _lines = 0
            while _time_mod.time() < deadline and _lines < _ACP_READ_MAX_LINES:
                _remaining = max(deadline - _time_mod.time(), 0)
                _ready, _, _ = _sel.select([_fd], [], [], _remaining)
                if not _ready:
                    return None  # deadline 耗尽（select 剩余时间全部用完）
                try:
                    _chunk = os.read(_fd, 65536)
                except BlockingIOError:
                    continue  # select 与 read 之间的就绪窗口已消失，回循环重检 deadline
                if not _chunk:
                    return None  # EOF
                _buf.extend(_chunk)
                while _buf:
                    _nl = _buf.find(b"\n")
                    if _nl < 0:
                        if _time_mod.time() >= deadline:
                            return None  # 半行数据悬置到 deadline（原先在此永久挂起）
                        break  # 行不完整，等待后续数据
                    _raw = bytes(_buf[:_nl])
                    del _buf[:_nl + 1]
                    _lines += 1
                    _line = _raw.strip().decode("utf-8", errors="replace")
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

        # 2. session/new — 获取真实 sessionId（Issue #441，对齐 _probe_hwcloud_acp）：
        # hermes ACP 服务端对空/不存在的 sessionId 的 prompt 返回 stopReason=refusal，
        # 旧探针 sessionId 写死 "" → 升级 #423 refusal 口径后监控全量 refused。
        # cwd 用轻量临时目录（对齐 hwcloud 探针）：家目录在 devspace/网络挂载场景
        # 可能不可写或初始化慢，探活不应受其拖累；mcpServers 必须空数组。
        import tempfile as _tempfile
        _probe_cwd = _tempfile.mkdtemp(prefix="hermes-probe-")
        _send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
               "params": {"cwd": _probe_cwd, "mcpServers": []}})
        _new_resp = _read_response(2, _ACP_PROBE_TIMEOUT)
        if not _new_resp or "error" in _new_resp:
            latency = round(_time_mod.time() - start, 1)
            _err = "no response" if not _new_resp else _new_resp["error"].get("message", "")[:200]
            return {"status": "fail", "latency": latency, "error": f"ACP session/new: {_err}"}
        _sid = (_new_resp.get("result") or {}).get("sessionId", "")
        if not _sid:
            # session/new 成功但无 sessionId → 不把空 sessionId 带进 prompt（重蹈 #441 覆辙）
            latency = round(_time_mod.time() - start, 1)
            return {"status": "fail", "latency": latency,
                    "error": "ACP session/new: response missing sessionId"}

        # 3. session/prompt "hi" — 端到端验证 provider + 模型推理（真实 sessionId）
        _send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
               "params": {"sessionId": _sid,
                          "prompt": [{"type": "text", "text": "hi"}]}})
        _prompt_resp = _read_response(3, _ACP_PROMPT_TIMEOUT)
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
        # Issue #423: refusal 独立标记（协议活着但模型拒绝 ≠ 链路不通）。
        # 服务端心跳落库对非 ok/fail 状态仅记录、不增 probe_fail_count → 不触发连败摘除。
        if _stop == "refusal":
            return {"status": "refused", "latency": latency, "error": "",
                    "output": "ACP initialize+session/new+prompt refused (stopReason=refusal)"}
        return {"status": "ok", "latency": latency, "error": "",
                "output": f"ACP initialize+session/new+prompt ok (stopReason={_stop})"}

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
        # 探针临时 cwd 清理（hwcloud 探针同款；session 随子进程 terminate 终止）
        try:
            _shutil.rmtree(_probe_cwd, ignore_errors=True)
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
        if os.name == "nt":
            _win_acp_drain_stderr(proc)

        import select as _sel

        def _send(msg: dict):
            proc.stdin.write((_json.dumps(msg) + "\n").encode("utf-8"))
            proc.stdin.flush()

        # select + readline 轮询；二进制模式下 readline 返回 bytes。
        # Issue #423 QA 复核 Optional#1 同构修复：readline() 遇半行未换行数据会阻塞在
        # read 系统调用（deadline 管不到 readline）。改非阻塞 fd（os.set_blocking，
        # 标准可移植接口）+ os.read 小块读 + 行缓冲拼行，每轮 select/read 后重检
        # deadline，半行悬置到 deadline 即返回。
        _fd = proc.stdout.fileno()
        if os.name != "nt":
            os.set_blocking(_fd, False)
        _probe_buf = bytearray()

        def _read_response(req_id: int, timeout: float) -> dict | None:
            # Windows：无 os.set_blocking，且 select 不支持管道 → 读线程 + 队列
            if os.name == "nt":
                return _win_acp_read_response(proc, _send, _probe_buf, req_id, timeout)
            # Issue #423 同步审查：本实现已是 deadline 驱动（while deadline，无固定行数上限），
            # 无 hermes 同款 10 行 bug；仅补 _ACP_READ_MAX_LINES 防御上限保持同构。
            deadline = _time_mod.time() + timeout
            _lines = 0
            while _time_mod.time() < deadline and _lines < _ACP_READ_MAX_LINES:
                _ready, _, _ = _sel.select([_fd], [], [], 0.3)
                if not _ready:
                    if proc.poll() is not None:
                        return None
                    continue
                try:
                    _chunk = os.read(_fd, 65536)
                except BlockingIOError:
                    continue
                if not _chunk:
                    return None
                _probe_buf.extend(_chunk)
                while _probe_buf:
                    _nl = _probe_buf.find(b"\n")
                    if _nl < 0:
                        if _time_mod.time() >= deadline:
                            return None  # 半行悬置到 deadline（原先 readline 在此阻塞）
                        break
                    _raw = bytes(_probe_buf[:_nl])
                    del _probe_buf[:_nl + 1]
                    _lines += 1
                    _line = _raw.strip().decode("utf-8", errors="replace")
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
        # Issue #423: refusal 独立标记（与 hermes 探活同构；非 ok/fail 不计入连败摘除）
        if _stop == "refusal":
            return {"status": "refused", "latency": latency, "error": "",
                    "output": "ACP initialize+session/new+prompt refused (stopReason=refusal)"}
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



def _probe_aishell_dsh_acp(cli_path: str) -> dict:
    """aishell-dsh ACP 模式探活：initialize + session/new + set模型 + prompt 四步。

    与 _probe_hwcloud_acp 同构（不做 --version 假探活）；差异（阶段 0 实测）：
    - 启动命令 `aishell acp`
    - session/new 后须按 configOptions 设模型（默认 trial 无凭据必失败），
      value 从 options 原样取（自拼报 -32602），set_config_option 请求键是 configId
    - 无 request_permission/turn_end，PromptResponse stopReason 收口
    """
    import json as _json
    import shutil as _shutil
    import subprocess as _subprocess
    import threading as _threading
    import queue as _queue
    import tempfile as _tempfile

    # CLI 定位统一走 find_cli（PATH → 默认安装目录 → Uninstall 注册表 InstallLocation
    # 三层兜底）；裸 shutil.which 只是历史上 PATH 时期的后备，装在非默认目录的
    # AI Shell 会漏——与 aishell_acp.py 任务链同一寻路源。
    from src.runtime.os_adapter import find_cli as _find_cli
    agent_cmd = cli_path or _find_cli("aishell")
    if not agent_cmd:
        return {"status": "fail", "latency": 0, "error": "aishell CLI not found"}

    start = _time_mod.time()
    proc = None
    try:
        # 二进制模式 + 读线程（避免 TextIOWrapper 缓冲吞字节导致假阴性，#441 同源教训）
        proc = _subprocess.Popen(
            [agent_cmd, "acp"],
            stdin=_subprocess.PIPE,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
        )
        lines: "_queue.Queue" = _queue.Queue()

        def _reader():
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    lines.put(line.decode("utf-8", errors="replace").strip())
            except Exception:
                pass
            finally:
                lines.put(None)

        _threading.Thread(target=_reader, daemon=True).start()

        def _send(msg: dict):
            proc.stdin.write((_json.dumps(msg) + "\n").encode("utf-8"))
            proc.stdin.flush()

        _next_id = [0]

        def _rpc(method: str, params: dict | None, timeout: float) -> tuple[dict | None, str | None]:
            _next_id[0] += 1
            rid = _next_id[0]
            msg = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            try:
                _send(msg)
            except Exception as e:
                return None, f"write {method}: {e}"
            deadline = _time_mod.time() + timeout
            while _time_mod.time() < deadline:
                if proc.poll() is not None:
                    return None, f"{method}: process exited"
                try:
                    item = lines.get(timeout=0.3)
                except _queue.Empty:
                    continue
                if item is None:
                    return None, f"{method}: stdout EOF"
                try:
                    raw = _json.loads(item)
                except _json.JSONDecodeError:
                    continue
                if raw.get("id") != rid:
                    continue
                if "error" in raw:
                    err = raw["error"]
                    return None, err.get("message", str(err))[:200]
                return raw.get("result") or {}, None
            return None, f"{method} timeout after {timeout}s"

        result, err = _rpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "grape-probe", "version": "0.1.0"},
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
        }, 30)
        if err:
            return {"status": "fail", "latency": round(_time_mod.time() - start, 1),
                    "error": f"initialize: {err}"}
        version = ((result or {}).get("agentInfo") or {}).get("version", "")

        result, err = _rpc("session/new", {"cwd": _tempfile.gettempdir(), "mcpServers": []}, 30)
        if err:
            return {"status": "fail", "latency": round(_time_mod.time() - start, 1),
                    "error": f"session/new: {err}"}
        sid = (result or {}).get("sessionId", "")

        # 设模型（默认 trial 无凭据必失败，跳过选第一个非 trial 路由；已配好则跳过）
        for opt in (result or {}).get("configOptions") or []:
            if not isinstance(opt, dict) or opt.get("id") != "model":
                continue
            cur = opt.get("currentValue", "")
            if cur and cur != '["aishell","trial"]':
                break
            picked = ""
            for g in (opt.get("options") or []):
                for item in (g.get("options") or [] if isinstance(g, dict) else []):
                    v = item.get("value", "") if isinstance(item, dict) else ""
                    if v and v != '["aishell","trial"]':
                        picked = v
                        break
                if picked:
                    break
            if picked:
                _, err = _rpc("session/set_config_option", {
                    "sessionId": sid, "configId": "model", "value": picked}, 20)
                if err:
                    return {"status": "fail", "latency": round(_time_mod.time() - start, 1),
                            "error": f"set_config_option: {err}"}
            break

        _prompt_id = _next_id[0] + 1
        _next_id[0] += 1
        _send({"jsonrpc": "2.0", "id": _prompt_id, "method": "session/prompt",
               "params": {"sessionId": sid,
                          "prompt": [{"type": "text", "text": "只回复 ok"}]}})
        deadline = _time_mod.time() + 60
        stop_reason = ""
        while _time_mod.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                item = lines.get(timeout=0.3)
            except _queue.Empty:
                continue
            if item is None:
                break
            try:
                raw = _json.loads(item)
            except _json.JSONDecodeError:
                continue
            if raw.get("id") == _prompt_id:
                if "error" in raw:
                    return {"status": "fail", "latency": round(_time_mod.time() - start, 1),
                            "error": f"prompt: {raw['error'].get('message', '')[:200]}"}
                stop_reason = (raw.get("result") or {}).get("stopReason", "")
                break

        latency = round(_time_mod.time() - start, 1)
        if stop_reason == "end_turn":
            out = {"status": "ok", "latency": latency}
            if version:
                out["version"] = version
            return out
        return {"status": "fail", "latency": latency,
                "error": f"prompt stopReason={stop_reason or 'none'}"}
    except Exception as e:
        return {"status": "fail", "latency": round(_time_mod.time() - start, 1),
                "error": str(e)[:200]}
    finally:
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
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
    # aishell-dsh 使用 ACP 模式探活（与实际任务执行路径一致：aishell acp，四步全真）
    if engine_name == "aishell-dsh":
        return _probe_aishell_dsh_acp(cli_path)


    cmd = _PROBE_COMMANDS.get(engine_name)
    if not cmd:
        return {"status": "skip", "latency": 0, "error": f"unknown engine: {engine_name}"}
    if cli_path:
        cmd = [cli_path] + cmd[1:]
    # Issue #471 C2：codearts 探测走专用数据目录（预写目录信任）——
    # 探测进程无 TTY，folder-trust 闸门未信任 cwd 时会 exit 1 误报引擎不健康。
    # Issue 探活session堆积：opencode/cursor/dsh 探测走独立 XDG_DATA_HOME 临时目录，
    # 探活会话只写临时库，finally 整目录删除——不进生产 opencode.db（此前每次
    # 探活新建一条 Greeting session 且无任何清理路径，DB 堆积至数百 MB）。
    # 认证/配置不受影响：opencode 凭据存 ~/.config（XDG_CONFIG_HOME），不在数据目录。
    _probe_env = None
    _probe_data_tmp = None
    if engine_name == "codearts":
        try:
            from src.runtime.codearts import probe_env as _codearts_probe_env
            _probe_env = {**os.environ, **_codearts_probe_env(os.getcwd())}
        except Exception as _pe:
            log.warning(f"codearts probe env setup failed: {_pe}")
    elif engine_name in ("opencode", "cursor", "dsh"):
        try:
            import tempfile as _tempfile
            _probe_data_tmp = _tempfile.mkdtemp(prefix="grape-probe-data-")
            _probe_env = {**os.environ, "XDG_DATA_HOME": _probe_data_tmp}
        except Exception as _pe:
            log.warning(f"probe data dir setup failed for {engine_name}: {_pe}")
            _probe_data_tmp = None
    start = _time_mod.time()
    try:
        result = _subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT, stdin=_subprocess.DEVNULL,
            env=_probe_env,
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
    finally:
        if _probe_data_tmp:
            _shutil.rmtree(_probe_data_tmp, ignore_errors=True)


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
    """后台探测循环：每 10 分钟对所有本机引擎发起测试对话。

    Issue #490：首轮先 sleep _PROBE_INTERVAL 再探测，与注册时 _worker_probe_once()
    的即时结果间隔 ≥ 一个周期，避免启动阶段连续两轮探测。
    """
    global _worker_probe_started
    while True:
        # 首轮先等待一个周期，复用注册时 _worker_probe_once() 的即时结果
        _time_mod.sleep(_PROBE_INTERVAL)
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
    """返回当前探测结果快照（供心跳上报）。

    Issue #490：按观测指纹去重——已成功发送且 last_probe 未变化的引擎不再重复携带，
    避免心跳重放同一观测导致服务端计数虚增。新观测（last_probe 变化）正常发送。
    """
    with _worker_probe_lock:
        snapshot = dict(_worker_probe_results)
        # 过滤掉已成功发送且未变化的观测
        return {
            eng: data for eng, data in snapshot.items()
            if data.get("last_probe") != _last_sent_probe_fingerprint.get(eng)
        }


def _mark_probe_fingerprint_sent(sent: dict) -> None:
    """心跳成功后写回已发送观测的指纹（engine_name → last_probe）。

    Issue #490：仅心跳成功才更新指纹；失败不更新，下次重试仍可发送。
    """
    with _worker_probe_lock:
        for eng, data in (sent or {}).items():
            lp = (data or {}).get("last_probe") if isinstance(data, dict) else None
            if lp:
                _last_sent_probe_fingerprint[eng] = lp


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


def _watchdog_timeout_for(env: dict | None = None) -> float:
    """worker 任务级看门狗超时（秒，Issue #470 收敛点）。

    取 RUNTIME_TASK_TIMEOUT：任务级 env 优先（服务端 enqueue 时经 env_extra 注入的
    统一配置值——env > 管理台 meta > 配置文件）→ os.environ → 默认 3600；
    新任务经 setdefault 注入后任务级值生效，worker 本地 os.environ 仅在任务 env
    无该键时兜底（本地直跑/旧任务）。
    与 hermes/hwcloud `_global_timeout_for(env)` 同优先级语义。
    """
    _src = (env or {}).get("RUNTIME_TASK_TIMEOUT") or os.environ.get("RUNTIME_TASK_TIMEOUT")
    try:
        return float(_src) if _src not in (None, "") else 3600.0
    except Exception:
        return 3600.0


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return int(default)


def _discover_local_runtimes() -> list[dict]:
    """本机 CLI 列表（复用 discover_runtimes，补齐 version/healthy）。

    hermes 额外做 ACP 依赖自举（hermes-acp-auto-bootstrap-design.md）：
    --version 探测不出 ACP 依赖缺失（可选 extra），须对 `hermes acp` 做真实
    ACP 握手探测才能暴露（`acp --check` 子命令在 hermes v0.9.0 起不存在，
    Issue #613）；缺失时自动补装 agent-client-protocol（版本经兼容性判断，
    不硬钉），补装失败则 healthy=False（引擎照常注册，但服务端 engine_select
    只挑 healthy，不会再把任务派到一台调不通的机器上）。
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
                # Windows 冷启动慢（hermes.exe 实测 8s+，注册表/WMI/插件扫描），5s 会误判
                # healthy=False → 服务端 engine_select 不派任务。放宽到 20s（仍远小于探活超时）。
                res = subprocess.run([cli, "--version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
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
        # Issue #865：配置模型（本机配置解析；失败留空不影响注册/心跳）
        from src.runtime.engine_model import detect_engine_model
        out.append({"name": r["name"], "display_name": (_dn.capitalize() if _dn else r["name"]),
                    "version": version, "healthy": healthy, "cli_path": cli, "installed": r.get("installed", False),
                    "model": detect_engine_model(_et)})
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
    """由引擎实例名推导执行后端——统一走 canonical_engine 查表（前缀最长匹配）。

    hermes-xxx → hermes；codearts-xxx → codearts；codex-xxx → codex；
    aishell-dsh-xxx → aishell-dsh（新引擎，先于 aishell 前缀命中）；
    hwcloud-xxx / aishell-xxx → hwcloud（旧轨不变）；其余默认 opencode。
    """
    from src.runtime.instances import canonical_engine

    return canonical_engine(engine_name)


def _usage_retry_engine(engine_name: str | None) -> bool:
    from src.runtime.instances import usage_retry_engine

    return usage_retry_engine(engine_name)


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
                with open(_os.path.join(detail["path"], "SKILL.md"), "r", encoding="utf-8-sig") as f:
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


def _same_url_host(a: str, b: str) -> bool:
    """比较两个 URL 是否指向同一端点（scheme + host + port + path，忽略尾部斜杠）。

    用于判定单地址部署：业务基址与 worker 回连基址同源时，内网 worker 必须
    继续把业务基址覆盖为本机直连，否则公网/NAT 回环不通（改造前语义）。

    仅比较 netloc 会把「同 host:port、不同 path 前缀」的双地址部署（如业务 API 与
    runtime 分路径承载）误判为单地址，进而把业务基址覆盖为 localhost；因此纳入
    scheme 与 path 比较，任一不同即视为双地址、保留显式业务基址。
    """
    if not a or not b:
        return False
    try:
        from urllib.parse import urlsplit
        ua = urlsplit(a.strip())
        ub = urlsplit(b.strip())
        if (ua.scheme.lower(), ua.netloc.lower()) != (ub.scheme.lower(), ub.netloc.lower()):
            return False
        pa = (ua.path or "").rstrip("/")
        pb = (ub.path or "").rstrip("/")
        return pa == pb
    except Exception:
        return a.strip().lower().rstrip("/") == b.strip().lower().rstrip("/")


def _apply_local_worker_base_override(env: dict, task: dict, local_base: str):
    """本机/内网 worker 修正回连基址（Issue #1577 语义拆分）。

    - `RUNTIME_SERVER_URL` / `task["server_url"]` 覆盖为本 worker 直连地址
      （A2A / tasks 等回连调用，走公网会 NAT 回环不通）。
    - 业务 API 基址 `GRAPE_SERVER_URL`：双地址部署（显式业务基址与 worker 回连
      基址不同主机）时保留不动，任务内 grape 业务命令需要全量端点；单地址部署
      （未提供业务基址，或业务基址与回连基址同主机）时仍覆盖为本机直连地址，
      与改造前等价，避免内网 worker 走公网 NAT 回环不通。
    """
    _business_base = env.get("GRAPE_SERVER_URL") or ""
    _runtime_base = task.get("server_url") or env.get("RUNTIME_SERVER_URL") or ""
    _single_address = (not _business_base) or _same_url_host(_business_base, _runtime_base)
    env["RUNTIME_SERVER_URL"] = local_base
    task["server_url"] = local_base
    if _single_address:
        env["GRAPE_SERVER_URL"] = local_base
        log.info("local worker: single-address deploy, override GRAPE_SERVER_URL -> %s", local_base)
    # Issue #1858：按服务基址跟随生效基址修正——
    # - vcs / issue 跟随「生效的业务基址」`GRAPE_SERVER_URL`：单地址部署时该值为
    #   local_base（避免 NAT 回环）；分端口部署（业务 8081 / runtime 8083）时保留
    #   业务基址，绝不改成 runtime 端口的 local_base，否则 VCS/Issue 404 复现。
    #   仅在缺失或与业务基址同端点时改写，显式分服务基址保留。
    # - a2a 继续跟随 runtime 直连：缺失或与回连基址同端点时覆盖为 local_base。
    _business_effective = env.get("GRAPE_SERVER_URL") or ""
    _svc_refs = (
        ("GRAPE_VCS_BASE_URL", "vcs", _business_base, _business_effective),
        ("GRAPE_ISSUE_BASE_URL", "issue", _business_base, _business_effective),
        ("GRAPE_A2A_BASE_URL", "a2a", _runtime_base, local_base),
    )
    _overridden = {}
    for _key, _svc, _ref, _target in _svc_refs:
        _val = env.get(_key) or ""
        if not _val or (_ref and _same_url_host(_val, _ref)):
            if _target and _target != _val:
                env[_key] = _target
                _overridden[_svc] = "local_worker_override"
    if _overridden:
        try:
            import json as _json_base_src
            _sources = _json_base_src.loads(env.get("GRAPE_BASE_URL_SOURCES") or "{}")
            _sources.update(_overridden)
            env["GRAPE_BASE_URL_SOURCES"] = _json_base_src.dumps(
                _sources, ensure_ascii=False, sort_keys=True)
        except Exception:
            pass
        log.info("local worker: override service base env -> %s (%s)",
                 local_base, ",".join(sorted(_overridden)))


def _current_username() -> "str | None":
    """Resolve the current username without touching a controlling terminal.

    Fallback chain: USER env var -> pwd.getpwuid(os.getuid()).pw_name -> str(os.getuid()).
    os.getlogin() is deliberately avoided: it raises OSError(ENXIO) on TTY-less
    workers, and as a default-argument expression it is evaluated eagerly even
    when USER is set (Issue #522). Returns None only on platforms without
    os.getuid (Windows callers never reach this code path).
    """
    user = os.environ.get("USER", "")
    if user:
        return user
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        pass
    if hasattr(os, "getuid"):
        return str(os.getuid())
    return None


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
        "dsh": ["dsh headless"],
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
    user = _current_username()
    for pattern in patterns:
        try:
            pgrep_args = ["pgrep", "-u", user, "-f", pattern] if user else ["pgrep", "-f", pattern]
            out = subprocess.run(
                pgrep_args,
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


def _fetch_skill_zip_by_key(obs_key: str, task_id: str = "") -> bytes | None:
    """Issue #463 P2 扩展（2026-10-27）：按对象键拉技能 zip（skill_files 引用单写格式的
    消费端）。复用 _fetch_skill_zip 的 ② 服务端代拉通道——服务端按 task 重解析对象键，
    本函数仅在无预签名 URL 的兜底分支调用（①② 主链路不受影响）。

    走 POST /api/runtime/tasks/{task_id}/skill-zip（服务端现查 file_path → zip_cache）。
    """
    timeout = float(os.environ.get("RUNTIME_OBS_FETCH_TIMEOUT", "10"))
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
                log.info(f"[skill] __obs_key__ 服务端代拉成功 task={task_id}")
                return _b64.b64decode(b64)
        except Exception as e:
            log.warning(f"[skill] __obs_key__ 代拉失败: {type(e).__name__}")
    # task_id/token 不可用（极老 worker 无凭据）→ 只能告警，由调用方降级
    log.warning(f"[skill] __obs_key__ 拉取通道不可用（无 task_id/token）key={obs_key[:60]}")
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
            # Issue #752（M2）：清理（卸载 worktree）前保真校验——功能文件指纹 vs
            # 任务开始基线，不一致（复测清理损坏功能文件证据）告警供人工裁决，不阻断清理。
            try:
                from src.runtime.worktree_guard import verify_fidelity
                _fid = verify_fidelity(cwd)
                if not _fid.get("baseline_missing") and (_fid.get("modified") or _fid.get("removed")):
                    log.warning(f"workdir cleanup fidelity alarm task={task.get('id')}: "
                                f"modified={_fid.get('modified')} removed={_fid.get('removed')}")
            except Exception as _fe:
                log.warning(f"workdir cleanup fidelity check failed: {_fe}")
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

        # Issue #993（M1）：分支级写租约必须先于 ensure_feature_branch（其中可能
        # `git checkout -b` 写分支）获取，否则被保护的写动作可能先发生。
        # 与 direct 端共用 branch_lease 上下文，租约获取/释放生命周期不再各写一遍。
        from src.runtime import branch_guard as _branch_guard
        with _branch_guard.branch_lease(
                _rk, _wt_branch, task_id=str(task.get("id") or "")) as _lease:
            if not _lease.get("ok"):
                log.warning(f"linked PR: branch lease blocked {_rk}:{_wt_branch}: "
                            f"{_lease.get('message')}")
                _results.append({"repo_key": _rk, "status": "blocked",
                                 "reason": _lease.get("reason", "branch_locked"),
                                 "message": _lease.get("message", "")})
                continue

            try:
                # 0. 分支护栏：检查当前分支是否是 feat/issue-{N}，禁止直推基线分支
                from src.runtime.repogit import ensure_feature_branch
                ensure_feature_branch(_wt_dir, _wt_branch, _base_branch, logger=log)
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

                # Issue #752（M2）：commit 前冲突标记推送守卫——暂存文件硬命中
                # `<<<<<<< `/`>>>>>>> ` → 阻断提交/推送（防冲突残留被 add -A 整树写入分支，
                # PR #352 实证的损坏通道）；软命中（孤立 =======）仅告警。
                # 保真告警不在推送口（PR #874 审查修正）：agent 正常编码必然修改功能文件，
                # 推送口告警全是噪声；保真校验保留在清理口（remove_issue_worktree 前），
                # 由 cleanup 阶段对照任务开始基线告警。
                try:
                    from src.runtime.worktree_guard import scan_staged_files
                    _g = scan_staged_files(_wt_dir)
                    if _g.get("hard"):
                        log.warning(f"linked PR: conflict markers in staged files, BLOCKED push {_rk}: {_g['hard']}")
                        _results.append({"repo_key": _rk, "status": "blocked",
                                         "reason": f"conflict markers in staged files: "
                                                   f"{list(_g['hard'].keys())[:5]}"})
                        continue
                    if _g.get("soft"):
                        log.warning(f"linked PR: soft conflict markers (allowed) {_rk}: {_g['soft']}")
                except Exception as _ge:
                    log.warning(f"linked PR: worktree guard failed {_rk}: {_ge}")

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
                # Issue #993（M2）：统一走 branch_guard.guarded_push——preflight 阻断
                # PR 已 merged/closed 或远端漂移，push 后记录已推 local sha。
                _push = _branch_guard.guarded_push(_rk, _wt_branch, _wt_dir, token=_token)
                if not _push.get("ok"):
                    log.warning(f"linked PR: push blocked/failed {_rk}:{_wt_branch}: "
                                f"{_push.get('message')}")
                    _status = "blocked" if _push.get("stage") == "preflight" else "failed"
                    _results.append({"repo_key": _rk, "status": _status,
                                     "reason": _push.get("reason", "guard_blocked"),
                                     "message": _push.get("message", "")})
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


def _prepare_codearts_data_dir(tid: str, cwd: str) -> str:
    """方案 G + Issue #471 C2：创建 codearts per-task 独立数据目录并预写配置。

    预写内容（均幂等）：
    1. storage/permission/global.json + config.json（write/edit 等权限 allow）
    2. storage/sandbox/config.json（network_policy=allow_all）
    3. settings.json——目录信任（C2）：把任务 cwd 加入 trustedFolders，
       根治无 TTY 环境 folder-trust 闸门 exit 1。
    4. symlink sandbox_runner / system/skills 指向宿主机（避免每次解压 66MB）

    返回 per-task cli-data 目录（调用方设为 KERNEL_DATA_DIR）。
    """
    import os as _os
    _tasks_base = _os.environ.get("CODEARTS_TASKS_DIR", "/var/cache/codearts-tasks")
    _data_dir = _os.path.join(_tasks_base, tid)
    _cli_data = _os.path.join(_data_dir, "cli-data")
    # 1. 预写 global.json + config.json (write/edit/external_directory_write=allow)
    _perm_dir = _os.path.join(_cli_data, "storage", "permission")
    _os.makedirs(_perm_dir, exist_ok=True)
    for _fname in ("global.json", "config.json"):
        _f = _os.path.join(_perm_dir, _fname)
        if not _os.path.exists(_f):
            with open(_f, "w") as f:
                json.dump(_CODEARTS_ALLOW_PERMS, f, indent=2)
    # 2. 预写 sandbox/config.json (network_policy=allow_all)
    _sandbox_dir = _os.path.join(_cli_data, "storage", "sandbox")
    _os.makedirs(_sandbox_dir, exist_ok=True)
    _sandbox_cfg = _os.path.join(_sandbox_dir, "config.json")
    if not _os.path.exists(_sandbox_cfg):
        with open(_sandbox_cfg, "w") as f:
            f.write('{"network_policy": "allow_all"}')
    # 3. 预写 settings.json：目录信任（Issue #471 C2，无 TTY 不再致命退出）
    from src.runtime.codearts import write_trust_settings
    write_trust_settings(_cli_data, [cwd])
    # 4. symlink sandbox_runner + system/skills 指向宿主机
    _home = _os.path.expanduser("~")
    _host_cli = _os.path.join(_home, ".codeartsdoer", "cli-data")
    _sandbox_runner = _os.path.join(_cli_data, "sandbox_runner")
    if not _os.path.exists(_sandbox_runner) and _os.path.isdir(_os.path.join(_host_cli, "sandbox_runner")):
        _os.symlink(_os.path.join(_host_cli, "sandbox_runner"), _sandbox_runner)
    _sys_skills = _os.path.join(_cli_data, "system", "skills")
    if not _os.path.exists(_sys_skills) and _os.path.isdir(_os.path.join(_host_cli, "system", "skills")):
        _os.makedirs(_os.path.join(_cli_data, "system"), exist_ok=True)
        _os.symlink(_os.path.join(_host_cli, "system", "skills"), _sys_skills)
    return _cli_data


def _materialize_worker_skill(task: dict, env: dict, prompt: str, skill_dir: str) -> str:
    """Issue #607：_execute_task 技能物化块提取（可单测）。返回追加降级说明后的 prompt。

    语义与提取前逐字对齐（v3.0 OBS 直连/代拉 + #564 原子物化/官方包回退），
    仅在失败分支追加 Issue #607 兜底：物化失败（zip/skill_files 均失败、结果为空、
    或物化异常）时向 skill_dir/<skill>/ 写入 FALLBACK.md 降级指引（Agent 首轮
    即可读到 `grape skills detail` 权威取回命令）+ log.warning 结构化留痕；
    物化成功路径不受影响。
    """
    from src.runtime.skill_materialize import write_skill_fallback
    try:
        # Issue #564：技能物化降级/回退的 prompt 降级说明（追加在 prompt 尾部，Agent 可见）
        _skill_notes = []
        # Issue #1635：来源溯源 + 双向漂移校验（scope 由 skill_id 解析，失败则跳过）
        _skill_id = int(task.get("skill_id") or 0)
        _skill_scope = ""
        try:
            if _skill_id:
                from src.user.db import get_skill_by_id
                _skill_scope = (get_skill_by_id(_skill_id) or {}).get("scope") or ""
        except Exception as _sse:
            log.warning(f"[skill] scope resolve failed: {_sse}")
        _skill_meta: dict = {}
        # Issue #1635 review：官方回退成功后的有效来源（物化名/scope/skill_id）。
        # 官方内容 digest/version 必须与 official 来源口径一致，不能沿用任务原始
        # skill_id/_skill_scope（否则构成「主来源 id + 官方 digest」错配）。
        _fb_source: dict = {}

        def _fallback_official_skill(_skill_dir, _task, _env) -> dict:
            """主渠道物化失败 → 官方技能包原子回退；成功则注入 DEVOPS_SKILLS_DIR。

            Issue #617：按名未命中时优先用服务端 claim 下发的 skill_fallback
            （agent_type 对应官方检查器，worker 无库不自行解析），提示带
            `grape skills detail --skill_id <id>`。
            """
            _skill_name = _task.get("skill") or ""
            _fb_info = _task.get("skill_fallback") or {}
            _fb_name = str(_fb_info.get("skill_name") or "")
            _fb_id = int(_fb_info.get("skill_id") or 0)
            try:
                from src.config import resolve_official_skill_path
                _official = resolve_official_skill_path(_skill_name)
            except Exception as _e:
                log.warning(f"[skill] official resolve failed: {_e}")
                _official = ""
            if not _official and _fb_name:
                # 按名未命中 → agent_type 官方检查器（skill_fallback）回退
                try:
                    from src.config import resolve_official_skill_path as _resolve
                    _official = _resolve(_fb_name)
                except Exception as _e:
                    log.warning(f"[skill] checker resolve failed: {_e}")
                    _official = ""
            if _official and os.path.isfile(_official):
                from src.runtime.skill_materialize import materialize_skill_dir_atomic
                # 官方目录名即落盘技能名（checker 回退时为 _fb_name，同名回退为 _skill_name）
                _dst_name = os.path.basename(os.path.dirname(_official)) or _skill_name
                _ok, _r = materialize_skill_dir_atomic(
                    _skill_dir, _dst_name, os.path.dirname(_official))
                if _ok:
                    _env["DEVOPS_SKILLS_DIR"] = _skill_dir
                    # 同名官方包回退 → official/0；agent_type 检查器回退（异名）→
                    # official + 检查器 skill_id（与共享物化实现口径一致）。
                    _fb_source["skill_name"] = _dst_name
                    _fb_source["scope"] = "official"
                    _fb_source["skill_id"] = 0 if _dst_name == _skill_name else _fb_id
                    if _dst_name != _skill_name:
                        # Issue #887（QA 修复）：物化名≠绑定名时回写 task.skill_fallback，
                        # 使 _skill_consistency_gate 将其识别为 declared_fallback
                        # （ok + mismatch 显式标注），而非 local_cache 残留自相矛盾拒绝。
                        try:
                            _task["skill_fallback"] = {
                                "skill_name": _dst_name, "skill_id": _fb_id}
                        except Exception:
                            pass
                        return {"status": "fallback", "detail": (
                            f"skill '{_skill_name}' not found; materialized official checker "
                            f"'{_dst_name}' (fetch via `grape skills detail --skill_id {_fb_id}`) "
                            f"at DEVOPS_SKILLS_DIR/{_dst_name}")}
                    return {"status": "fallback", "detail": "official skill package materialized"}
                return {"status": "degraded", "detail": f"official fallback failed: {_r}"}
            return {"status": "degraded", "detail": (
                f"official skill package not found"
                + (f"; checker '{_fb_name}' unavailable (fetch via "
                   f"`grape skills detail --skill_id {_fb_id}`)" if _fb_name else ""))}

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
            # ===== zip 物化（直连/代拉获取的 zip → 原子解压 + 完整性校验 → 恢复权限）=====
            # Issue #564：tmp + 原子 rename（不留半写 .skills）、按 zip 名单校验
            # SKILL.md/references、失败回退官方技能包、仍失败降级标注（不静默空注入）。
            # 权限恢复见 obs-skill-storage-design.md §5.7（Python 3.12+ zipfile 不再自动恢复
            # Unix 权限位；剥离 setuid/setgid/sticky 防提权）
            from src.runtime.skill_materialize import materialize_skill_zip_atomic
            raw = _b64.b64decode(skill_zip_b64)
            _ok_zip, _skills_ok, _reason = materialize_skill_zip_atomic(
                skill_dir, raw, baseline_dir="", scope=_skill_scope, meta=_skill_meta)
            # Issue #564 review：部分成功也必须校验目标技能是否实际落盘——
            # 多技能 zip 且目标技能校验失败时，不能仅凭部分成功就注入 DEVOPS_SKILLS_DIR
            #（构成"有路径无内容"的静默空注入），需走官方包回退 + 降级标注。
            _target_skill = task.get("skill") or ""
            if _ok_zip and _target_skill and _target_skill not in _skills_ok:
                # Issue #617：目标技能名未命中但官方检查器已物化（skill_fallback）→
                # 视为回退成功，提示读检查器 SKILL.md（含 skills detail --skill_id 路径）
                _fb_info = task.get("skill_fallback") or {}
                _fb_hit = str(_fb_info.get("skill_name") or "")
                if _fb_hit and _fb_hit in _skills_ok:
                    log.info(f"[skill] target '{_target_skill}' miss, checker '{_fb_hit}' materialized")
                    # Issue #1635 review：zip 内命中官方检查器（异名）→ 来源按
                    # official + 检查器 skill_id 构造，供执行前来源行读取。
                    _fb_source["skill_name"] = _fb_hit
                    _fb_source["scope"] = "official"
                    _fb_source["skill_id"] = int(_fb_info.get("skill_id") or 0)
                    _skill_notes.append(
                        f"\n\n[Skill Materialization Notice] DEVOPS_SKILLS_DIR={skill_dir} "
                        f"(fallback): skill '{_target_skill}' not found; materialized official "
                        f"checker '{_fb_hit}' for agent_type '{task.get('agent_type', '')}' "
                        f"instead (fetch via `grape skills detail --skill_id "
                        f"{_fb_info.get('skill_id', 0)}`). Read "
                        f"DEVOPS_SKILLS_DIR/{_fb_hit}/SKILL.md instead.")
                else:
                    _ok_zip = False
                    _reason = (f"target skill '{_target_skill}' not in materialized list "
                               f"[{', '.join(_skills_ok)}]")
                    log.warning(f"[skill] zip partial success misses target: {_reason}")
            if _ok_zip:
                env["DEVOPS_SKILLS_DIR"] = skill_dir
                if _reason:
                    log.warning(f"[skill] partial zip materialize: {_reason}")
            else:
                log.warning(f"[skill] zip materialize failed: {_reason}")
                _fb_status = _fallback_official_skill(skill_dir, task, env)
                _skill_notes.append(
                    f"\n\n[Skill Materialization Notice] DEVOPS_SKILLS_DIR={skill_dir} "
                    f"({_fb_status.get('status')}): zip materialize failed ({_reason}); "
                    f"{_fb_status.get('detail', '')}. If skill files under this path are "
                    f"missing, fetch the skill via `grape skills detail` or the platform "
                    f"skills API before proceeding.")
                # Issue #607：官方包回退也失败 → 写 FALLBACK.md 降级指引（不静默空注入）
                if _fb_status.get("status") == "degraded":
                    write_skill_fallback(skill_dir, _target_skill,
                                         f"zip materialize failed: {_reason}",
                                         stage="zip_b64")
        else:
            # ===== 旧格式：skill_files 文本物化（老任务/老 worker 兼容）=====
            # Issue #564：原子写盘 + 完整性校验 + 官方包回退；仅物化成功才注入
            # DEVOPS_SKILLS_DIR（杜绝"有路径无内容"的空注入）。
            from src.runtime.skill_materialize import materialize_skill_files_atomic
            skill_files = json.loads(task.get("skill_files") or "{}")
            # Issue #463 P2 扩展（2026-10-27）：引用单写格式 {"<skill>": {"__obs_key__": ...}}
            # —— 内容不内联 DB，按对象键现拉原始 zip（含二进制，优于文本物化）。
            # 老 worker 不识别此标记时（物化出一个 __obs_key__ 文件，无害），实际任务
            # 仍走 skill_obs_url 直连 / 服务端代拉，本分支只在 ①② 都失败时到达。
            _ref_key = ""
            for _sn, files in skill_files.items():
                if isinstance(files, dict) and "__obs_key__" in files:
                    _ref_key = str(files["__obs_key__"])
                    break
            if _ref_key:
                raw = _fetch_skill_zip_by_key(_ref_key, task_id=task.get("id", ""))
                if raw:
                    skill_zip_b64 = _b64.b64encode(raw).decode()  # 走下方统一 zip 物化分支
                    skill_files = {}
                else:
                    log.warning(f"[skill] __obs_key__ 拉取失败, task={task.get('id')} key={_ref_key[:60]}")
            # Issue #564 review：按技能粒度收集物化结果，避免逐技能覆盖、
            # 首个失败 break 后丢失其余技能的失败原因（早先成功者已落盘）。
            _per_skill = []  # [(skill_name, ok, reason)]
            for _sn, files in skill_files.items():
                _ok_s, _r_s = materialize_skill_files_atomic(
                    skill_dir, _sn, files or {},
                    baseline_dir="", scope=_skill_scope, meta=_skill_meta)
                _per_skill.append((_sn, _ok_s, _r_s))
                if not _ok_s:
                    log.warning(f"[skill] skill_files materialize failed for {_sn}: {_r_s}")
            _failed = [(_sn, _r) for _sn, _ok_f, _r in _per_skill if not _ok_f]
            _reason = "; ".join(f"{_sn}: {_r}" for _sn, _r in _failed)
            _ok_files = not _failed
            # Issue #607：skill_obs_url 直连/代拉均失败且 skill_files 为空（无任何物化
            # 结果）却计为成功——构成静默空注入；改为按降级处理：官方包回退 +
            # FALLBACK.md 兜底 + 留痕，不再注入空 DEVOPS_SKILLS_DIR。
            if _ok_files and not skill_files and not skill_zip_b64 and (task.get("skill") or ""):
                _ok_files = False
                _reason = "skill obs url fetch failed and skill_files empty"
            if _ok_files:
                env["DEVOPS_SKILLS_DIR"] = skill_dir
            else:
                _fb_status = _fallback_official_skill(skill_dir, task, env)
                _skill_notes.append(
                    f"\n\n[Skill Materialization Notice] DEVOPS_SKILLS_DIR={skill_dir} "
                    f"({_fb_status.get('status')}): skill_files materialize failed ({_reason}); "
                    f"{_fb_status.get('detail', '')}. If skill files under this path are "
                    f"missing, fetch the skill via `grape skills detail` or the platform "
                    f"skills API before proceeding.")
                # Issue #607：官方包回退也失败 → 写 FALLBACK.md 降级指引（不静默空注入）
                if _fb_status.get("status") == "degraded":
                    write_skill_fallback(skill_dir, task.get("skill") or "",
                                         _reason, stage="skill_files")
        if _skill_notes:
            prompt += "".join(_skill_notes)
        # Issue #1635：物化成功（含回退）→ 计算最终来源与漂移，供执行前来源行上报
        # Issue #1635 review：官方回退（同名/agent_type 检查器异名）时，来源必须取
        # 实际落盘的技能名 + official/0（或检查器 id），不得沿用任务原始 skill_id/
        # _skill_scope；否则 digest/version 来自官方内容而 id 指向主来源造成错配，
        # 且异名回退时原技能名无 SKILL.md 会漏掉 source/drift（来源行缺字段）。
        _final_skill = (_fb_source.get("skill_name") or task.get("skill") or "")
        _final_id = _fb_source.get("skill_id", _skill_id) if _fb_source else _skill_id
        _final_scope = (_fb_source.get("scope", _skill_scope)
                        if _fb_source else _skill_scope)
        if _final_skill and os.path.isfile(os.path.join(
                skill_dir, _final_skill, "SKILL.md")):
            try:
                from src.runtime.skill_materialize import build_skill_drift_and_source
                _skill_meta.update(build_skill_drift_and_source(
                    skill_dir, _final_skill, _final_id, _final_scope,
                    task.get("agent_type") or ""))
            except Exception as _sme:
                log.warning(f"[skill] drift/source build failed: {_sme}")
        task["_skill_materialize_meta"] = _skill_meta
    except Exception as e:
        log.warning(f"skill materialize failed: {e}")
        # Issue #607：物化异常分支同样写 FALLBACK.md 降级指引 + 留痕
        write_skill_fallback(skill_dir, task.get("skill") or "",
                             f"materialize exception: {e}", stage="exception")
    return prompt


async def _skill_consistency_gate(client, tid: str, task: dict, skill_dir: str, _res,
                                  env: dict | None = None,
                                  engine_name: str = "") -> dict | None:
    """Issue #887：执行前技能一致性硬校验（fail-fast）+ 来源日志标注。

    校验「即将执行的 skill」与 agent 绑定（任务 skill 字段，仓库级/场景级绑定解析值）
    一致；不一致（引擎本地缓存决定执行技能）→ 拒绝执行并显式报错（返回错误 dict，
    调用方据此置任务 failed，不静默跑错流程）。绑定为空 → 放行（本地缓存仅此时兜底）。

    扫描范围（QA 修复）：任务技能目录（cwd/.skills）+ 引擎原生技能目录
    （engine_skill_dirs，按 engine_name 推导后端）——seq=46108 的残留若落在
    引擎原生缓存而非 ws/.skills，仅扫 .skills 会漏检。

    双源互校（QA 修复）：env GRAPE_BOUND_SKILL（service.enqueue_task 注入的绑定
    基准）与 task["skill"] 不一致 → 拒绝（绑定基准被覆盖/漂移，可观测标注失真）；
    task["skill"] 为空时以 env 值兜底。

    无论放行与否，先向服务端上报技能来源标注日志（绑定值 vs 实际值，落任务日志）。
    校验自身异常不阻断执行（降级放行 + 告警，与物化链路容错口径一致）。
    """
    try:
        from src.runtime.skill_materialize import (verify_skill_execution_consistency,
                                                   skill_mismatch_error)
        from src.runtime.task_log_stream import build_skill_source_message
        # 双源互校：task["skill"]（绑定基准权威源）vs env GRAPE_BOUND_SKILL（下发镜像）
        _bound_task = (task.get("skill") or "").strip()
        _bound_env = ((env or {}).get("GRAPE_BOUND_SKILL") or "").strip()
        if _bound_task and _bound_env and _bound_task != _bound_env:
            _x_reason = (f"bound skill sources disagree: task.skill='{_bound_task}' "
                         f"but env GRAPE_BOUND_SKILL='{_bound_env}'; refusing execution "
                         f"to prevent wrong-skill run (Issue #887)")
            log.error(f"[skill] task {tid} refused to execute: {_x_reason}")
            try:
                await client.report_messages(tid, [build_skill_source_message(
                    _bound_task, _bound_env, "env_conflict",
                    ok=False, reason=_x_reason)])
            except Exception as _rme:
                log.warning(f"task {tid}: skill source log report failed: {_rme}")
            try:
                _res.dispose()
            except Exception:
                pass
            return {"_error": f"[skill_mismatch] {_x_reason}", "_started": False}
        if not _bound_task and _bound_env:
            task = dict(task)
            task["skill"] = _bound_env
        # 引擎原生技能目录（跨平台，见 os_adapter.engine_skill_dirs）
        _extra_dirs: list[str] = []
        try:
            from src.runtime.os_adapter import engine_skill_dirs as _esd
            _extra_dirs = _esd(_backend_for_engine(engine_name))
        except Exception as _ede:
            log.warning(f"task {tid}: engine skill dirs resolve failed: {_ede}")
        _sv = verify_skill_execution_consistency(
            task.get("skill") or "", skill_dir, task.get("skill_fallback") or None,
            extra_scan_dirs=_extra_dirs)
        _mm = task.get("_skill_materialize_meta") or {}
        try:
            await client.report_messages(tid, [build_skill_source_message(
                _sv.get("bound", ""), _sv.get("actual", ""), _sv.get("source", ""),
                ok=_sv.get("ok", True), reason=_sv.get("reason", ""),
                source_info=_mm.get("source") or None,
                drift=_mm.get("drift") or None)])
        except Exception as _rme:
            log.warning(f"task {tid}: skill source log report failed: {_rme}")
        if not _sv.get("ok"):
            _err = skill_mismatch_error(_sv)
            log.error(f"[skill] task {tid} refused to execute: {_err}")
            try:
                _res.dispose()
            except Exception:
                pass
            return {"_error": _err, "_started": False}
    except Exception as _sve:
        log.warning(f"task {tid}: skill consistency verify failed (degraded pass): {_sve}")
    return None


async def _flush_report_batch(client, tid: str, batch: list) -> str:
    """批量上报一批消息（Issue #975 三态处理）。

    返回：
    - "ok"      批次已送达（或已走降级：拆批重发/占位/补报队列），可清空 batch
    - "retry"   网络错误（可重试）——保留 batch，下个 flush 窗口重试，不清空不丢
    - "terminal" 任务已被服务端标终态（410），调用方应中断执行
    """
    from src.runtime.client import ReportBatchError
    from src.runtime.message_report import degrade_failed_batch, truncate_message, build_msg_entry_id
    try:
        _resp = await client.report_messages(tid, batch)
    except ReportBatchError as e:
        # 4xx 批次级失败（413/400）：拆批重发 → 单条仍失败发占位 + 落补报队列
        try:
            await degrade_failed_batch(
                lambda msgs: client.report_messages(tid, msgs), tid, batch, e,
                enqueue=_report_queue().add)
        except Exception as _de:
            # QA 复审修复（Spec #3）：降级链路自身抛错时绝不能仍返回 "ok"
            # 静默清空 batch（原缺陷换入口复现）。兜底：逐条落本地持久化补报队列
            # （截断防 413），由 _report_replay_loop 后续重放；队列也不可用才返回
            # "retry" 保留 batch 下个窗口重试——两条路径都不丢消息。
            log.warning(f"task {tid} report degrade failed: {_de}")
            try:
                import time as _time
                _q = _report_queue()
                _ts = _time.time()
                for i, m in enumerate(batch):
                    _q.add({"kind": "messages", "task_id": build_msg_entry_id(tid, _ts, i),
                            "task": tid, "message": truncate_message(m)})
                log.info(f"task {tid} report degrade fallback: {len(batch)} message(s) queued for replay")
                return "ok"
            except Exception as _qe:
                log.warning(f"task {tid} report degrade fallback enqueue failed: {_qe}")
                return "retry"
        return "ok"
    except CredentialRevokedError:
        raise
    if _resp is None:
        return "terminal"
    if isinstance(_resp, dict) and _resp.get("status") == "ok":
        # 服务端回执带 seq 范围（seq_start/seq_end）——记录累计已确认 seq 供对账
        _seq_end = _resp.get("seq_end")
        if _seq_end is not None:
            log.info(f"task {tid} messages acknowledged: count={len(batch)} "
                     f"seq_range={_resp.get('seq_start')}..{_seq_end}")
        return "ok"
    # 网络错误（{}）：保留 batch，下个窗口重试（不清空，防止静默丢失）
    return "retry"


def _abort_task_terminal(task, engine_name, env, res, started: bool, reason: str) -> dict:
    """任务被服务端标终态（410）→ 统一中断处理（QA 复审：执行中/收尾两处
    flush 的 terminal 处理块逐行重复，提取为单一实现）。"""
    tid = task.get("id", "")
    log.warning(f"task {tid} report_messages returned None (task terminated?), aborting: {reason}")
    _kill_stale_engine(task, _backend_for_engine(engine_name))
    res.dispose()
    _cleanup_task_workdir(task, env)
    return {"_error": f"task terminated by server (aborted {reason})", "_started": started}


async def _execute_task(client, task, engine_name: str = ""):
    """执行单个任务：git worktree 前置 → 落地技能/知识 → 占位符替换 → stream_runtime → 回报。

    engine_name 为该任务所属引擎实例名（如 opencode-worker-123249），用于日志区分。
    """
    import os as _os
    tid = task.get("id", "")
    cwd = _remap_cwd(task.get("cwd") or _os.getcwd())
    task["cwd"] = cwd
    _engine_tag = f"|engine:{engine_name}" if engine_name else ""

    # MR-2（2026-11-05）：任务执行 span——从任务行 trace_context 恢复 enqueue 的 trace
    # （同一 trace 的子段，A→C→worker 全链串联）；OTel 未启用/坏数据 → 跳过零影响。
    # span 存 task["__exec_span__"]，由外层调用的 finally 收口（覆盖超时/重试路径）。
    _exec_span = None
    try:
        from src.observability.otel import start_task_span
        _exec_span, _ = start_task_span(f"task {tid} execute {engine_name or ''}", {
            "task_id": tid, "engine": engine_name, "agent_type": task.get("agent_type", ""),
            "skill": task.get("skill", ""), "repo_key": task.get("repo_key", ""),
            "attempts": task.get("attempts", 0),
        }, trace_context=task.get("trace_context") or "")
        if _exec_span is not None:
            task["__exec_span__"] = _exec_span
    except Exception:
        _exec_span = None

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
    # Issue #516 M2：进度快照——流式事件周期性原子落盘；超时被杀前留有可用快照，
    # 自动重试时加载注入续跑上下文（见下方 resume 段）。成功/异常路径随 dispose 清理。
    from src.runtime.checkpoint import TaskCheckpoint
    _ckpt = TaskCheckpoint(tid)
    _res.track_checkpoint(_ckpt)
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
    # Issue #378: 按 agent_type 注入差异化静默超时 env（coding/qa=1200s，其余默认 600s）。
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
        # Issue #1577：本机/内网 worker 覆盖 worker runtime 基址（RUNTIME_SERVER_URL /
        # task.server_url，A2A/tasks 直连用）；双地址部署保留显式业务 API 基址
        # GRAPE_SERVER_URL，单地址部署仍覆盖为本机直连（改造前等价，避免 NAT 回环不通）。
        _apply_local_worker_base_override(env, task, _local_base)
        log.info(f"local worker: override RUNTIME_SERVER_URL/server_url -> {_local_base}")
    # Issue #1858：执行前三面基址前置校验——缺失即显式告警（禁止静默回退 localhost），
    # 提示服务端派发/worker env 重拉注入，便于 runbook 前置检查定位。
    _missing_svc_bases = [k for k in ("GRAPE_VCS_BASE_URL", "GRAPE_ISSUE_BASE_URL",
                                      "GRAPE_A2A_BASE_URL") if not (env.get(k) or "").strip()]
    if _missing_svc_bases:
        log.warning(
            "task %s missing service base env: %s; server dispatch should inject "
            "GRAPE_VCS_BASE_URL / GRAPE_ISSUE_BASE_URL / GRAPE_A2A_BASE_URL "
            "(worker env refresh covers historical snapshots)", tid, ",".join(_missing_svc_bases))
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
    # Issue #466: 传 env（含 GRAPE_LINKED_REPO_TOKENS，服务端派发时随 env_extra 下发），
    # token 解析 env 优先 → 本地 Repo.bot_token 回退
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
                    token=_get_task_token(task), env=env)
                if _wt_list:
                    env["GRAPE_LINKED_WORKTREES"] = _json_lr.dumps(
                        [{"repo_key": w["repo_key"], "branch": w["branch"], "dir": w["worktree_dir"]}
                         for w in _wt_list], ensure_ascii=False)
                    log.info(f"linked repos: {len(_wt_list)} worktrees initialized for issue={_num_lr} agent={task.get('agent_type','')}")
    except Exception as e:
        log.warning(f"linked repos worktree init failed: {e}")
    skill_dir = _os.path.join(cwd, ".skills")
    prompt = (task.get("prompt") or "").replace("{{SKILL_DIR}}", skill_dir)
    # Issue #1577 / 架构审查 M3：worker 端残留占位符按同一语义兜底——
    # {{SERVER_URL}}=业务 API（env GRAPE_SERVER_URL）、{{RUNTIME_SERVER_URL}}=回连基址，
    # 不再把业务占位符填成 A2A 地址（先替换长占位符，避免子串误命中）。
    _prompt_biz_url = env.get("GRAPE_SERVER_URL") or task.get("server_url") or ""
    _prompt_runtime_url = env.get("RUNTIME_SERVER_URL") or task.get("server_url") or ""
    prompt = (prompt.replace("{{RUNTIME_SERVER_URL}}", _prompt_runtime_url)
                    .replace("{{SERVER_URL}}", _prompt_biz_url))
    # 本机 worker：prompt 里 build_prompt 已把 {{SERVER_URL}} 替换成公网 server_base_url
    # （worker 端此处 .replace("{{SERVER_URL}}") 是空操作），需把 prompt 里已硬编码的公网
    # 地址回改为 localhost，否则本机 worker 的 A2A 走公网 NAT 回环不通（超时）。
    if _is_local_worker and _old_server_url and _local_base and _old_server_url != _local_base:
        prompt = prompt.replace(_old_server_url, _local_base)
        log.info(f"local worker: rewrote prompt server_url {_old_server_url} -> {_local_base}")
    # Issue #516 M2：超时自动重试续跑——_wrap 重试轮次给 task 打了 _grape_resume_attempt，
    # 此处加载上一轮进度快照，把最近事件摘要追加为续跑上下文（避免从零重做已完成步骤）。
    # 快照损坏/缺失 → 降级从零跑（仅记日志）。注入逻辑见 checkpoint.inject_resume（可单测）。
    from src.runtime.checkpoint import inject_resume
    prompt, _resumed = inject_resume(task, env, prompt, _ckpt)
    if _resumed:
        log.info(f"task {tid}: retry resumed from checkpoint {os.path.basename(_ckpt.path)}")
    elif int(task.get("_grape_resume_attempt") or 0) >= 2:
        log.info(f"task {tid}: retry without usable checkpoint, running from scratch")
    # Issue #607：技能物化块提取为 _materialize_worker_skill（可单测）；物化失败分支
    # 向 .skills/<skill>/ 写入 FALLBACK.md 降级指引 + log.warning 结构化留痕。
    prompt = _materialize_worker_skill(task, env, prompt, skill_dir)
    # Issue #887：执行前技能一致性硬校验（fail-fast）+ 来源日志标注——绑定 skill 未物化
    # 而本地缓存存在其它技能时拒绝执行（events seq=46108：绑定 caserun-prep 实际执行
    # casegen-prep，静默跑错流程并跳过环境硬校验）。扫描范围含引擎原生技能目录，
    # 并与 env GRAPE_BOUND_SKILL 双源互校。
    _sv_err = await _skill_consistency_gate(client, tid, task, skill_dir, _res,
                                            env=env, engine_name=engine_name)
    if _sv_err:
        return _sv_err
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
    # + settings.json（Issue #471 C2 目录信任）实现 per-process 权限隔离，
    # 不影响宿主机全局配置。
    # env 是局部变量，并发安全（不用 os.environ 避免 async 交错竞态）。
    # 通过 env_extra 传给 stream_agent → env.update(env_extra) 覆盖 get_env 的默认值。
    _engine = engine_name or ""
    if "codearts" in _engine.lower():
        try:
            env["KERNEL_DATA_DIR"] = _prepare_codearts_data_dir(tid, cwd)
            # 注册清理：task 结束时删除临时目录
            _tasks_base = _os.environ.get("CODEARTS_TASKS_DIR", "/var/cache/codearts-tasks")
            _res.track_tmp_pattern(f"{_tasks_base}/{tid}")
            log.info(f"[Agent:{task.get('agent_type') or ''}] codearts data dir: {env['KERNEL_DATA_DIR']}")
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
            # issue #727：hermes ACP 直报用量（("usage", {...}) 事件）——只喂采集器，
            # 不进消息流/日志（内部观测数据，非任务执行事件）。
            if evt_type == "usage":
                _usage_col.feed(evt_type, data)
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
            elif evt_type == "task:retry":
                # issue #403：模型流瞬时异常自动重试（用户侧可感知，事件流已透出）
                log.warning(f"  [{_esq}] ↻ auto-retry: {data}")
            batch.append({"type": evt_type, "data": data if not isinstance(data, str) else data})
            # Issue #516 M2：进度快照——喂流式事件（内存 tail + 周期原子落盘）
            try:
                _ckpt.feed(evt_type, data)
            except Exception:
                pass  # 快照是尽力而为的优化，绝不影响主执行链路
            # Context compaction: compact batch if it grows too large (prevents context overflow)
            if should_compact(batch):
                batch = compact_messages(batch)
            # 双触发批量上报（实时日志实时性关键）：50 条或 500ms 时间窗任一满足即 flush
            _now = time.monotonic()
            if len(batch) >= 50 or (_last_flush and _now - _last_flush >= 0.5):
                _outcome = await _flush_report_batch(client, tid, batch)
                if _outcome == "terminal":
                    # task 已被服务端标终态（410 Gone）→ 中断执行，杀引擎进程
                    return _abort_task_terminal(task, engine_name, env, _res, started,
                                                "during execution")
                if _outcome == "ok":
                    batch = []
                _last_flush = _now
        if batch:
            _outcome = await _flush_report_batch(client, tid, batch)
            if _outcome == "terminal":
                return _abort_task_terminal(task, engine_name, env, _res, started,
                                            "at final flush")
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
        if not usages and _usage_retry_engine(collector.engine):
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


def _register_failed(ids: list[int], installed: list[dict]) -> bool:
    """Issue #471 C1：注册请求成功（HTTP 200）即注册成功。

    无引擎 worker 的空 ids 是正常状态（服务端按实例键 upsert，无引擎则无行），
    不判失败、不退出——否则 daemon/systemd Restart=always 无限重启、安装脚本
    30s 注册验证失败触发回滚循环（鸡生蛋：引擎未装 → 注册"失败" → 重启 → 永远装不上）。
    仅当本机有引擎而服务端未返回任何 runtime id 时才视为真失败（服务端异常）。
    """
    return (not ids) and bool(installed)


async def _reregister(client, prev_installed, device_name, cli_version: str = "") -> tuple[list[int], dict, list] | None:
    """重新注册获取新 runtime id。先重置连接池清除 CLOSE-WAIT 死连接，再注册。

    Issue #471 C1：
    - 每次重注册前重新发现本机引擎（不用启动时快照）——启动时无引擎的 worker，
      用户后续安装引擎后无需重启即可自动注册上线（≤ RE_REGISTER_INTERVAL 秒）。
    - 返回 (runtime_ids, engine_by_rid, installed)；无引擎时返回 ([], {}, []) 同样算成功。
    - 有引擎但服务端返回空 ids → 返回 None（真失败）。

    Issue #471 QA 建议（S2/S3）：
    - S2：engine_by_rid 按 name 配对（服务端 register 返回 runtimes 明细含
      name；个别引擎被跳过时 zip 位置对齐会错位，按 name 配对根治）。
    - S3：重扫瞬时为空（此前快照有引擎）视为异常扫描——沿用上次快照、
      返回 None（调用方保留原 runtime_ids），不把空列表注册上去清空在线态；
      真实无引擎（快照也为空）仍正常注册空列表（C1 语义不变）。

    凭据被吊销（401 → CredentialRevokedError）时直接上抛——调用方退出进程，
    不再当普通失败无限退避重试。
    """
    from src.runtime.client import CredentialRevokedError
    await client.reset_session()
    log.warning("re-registering with server to refresh runtime ids...")
    try:
        # C1：重新发现本机引擎（启动后新安装的引擎在此被拾起）
        installed = [r for r in _discover_local_runtimes() if r.get("installed")]
        # S3：重扫瞬时为空（快照曾有引擎）→ 沿用快照不注册（异常扫描自愈交给下轮）
        if not installed and prev_installed:
            log.warning("re-scan found no engines while snapshot had "
                        f"{len(prev_installed)}; keeping snapshot (transient scan?)")
            return None
        _ls = _scan_local_skills()
        ids, _probe = await client.register(installed, device_name=device_name, cli_version=cli_version, local_skills=_ls)
        if ids:
            for rid in ids:
                await client.recover_orphans(rid)
            from src.runtime.instances import instance_name
            # S2：按 name 配对（服务端返回顺序/跳过与本地发现列表可能不一致，
            # zip 位置对齐在个别引擎被跳过时会错位）
            _name_by_rid = {}
            for _rt in (_probe_runtimes_detail(client) or []):
                if isinstance(_rt, dict) and _rt.get("id") is not None:
                    _name_by_rid[_rt["id"]] = str(_rt.get("name") or "").strip().lower()
            _local_by_name = {str(r.get("name") or "").strip().lower(): r for r in installed}
            engine_by_rid = {}
            for rid in ids:
                _nm = _name_by_rid.get(rid, "")
                _r = _local_by_name.get(_nm) if _nm else None
                if _r is None:
                    # 兜底：按位置对齐（服务端未返回明细/name 未知时保持旧行为）
                    _idx = ids.index(rid)
                    _r = installed[_idx] if _idx < len(installed) else {}
                engine_by_rid[rid] = instance_name(_r.get("name", ""), device_name, 0)
            log.info(f"re-registered successfully, new runtime ids: {ids}")
            return ids, engine_by_rid, installed
        if not installed:
            # C1：无引擎为正常状态——注册成功，ids 为空
            log.info("re-registered with no engines installed (normal state); "
                     "engines will auto-register once installed")
            return [], {}, []
        log.warning("re-register returned empty ids despite installed engines")
    except CredentialRevokedError:
        raise  # 凭据已吊销——重试无意义，上抛退出
    except Exception as e:
        log.warning(f"re-register failed: {e}")
    return None


def _probe_runtimes_detail(client):
    """S2 辅助：读取最近一次 register 响应中的 runtimes 明细（{id, name}）。

    真实 RuntimeClient.register 只返回 (ids, probe_url)；服务端响应明细
    挂在 client.last_register_detail（无则为 None，兜底按位置对齐）。
    测试 Fake 可自行提供该属性。
    """
    return getattr(client, "last_register_detail", None)


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
            _rr = await _reregister(client, installed, device_name, cli_version=_read_local_worker_version())
            if _rr is not None:
                runtime_ids, _new_ebr, installed = _rr
                if _new_ebr:
                    engine_by_rid = _new_ebr
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
                # Issue #516 M1：超时上限配置化——读取链 per-skill > per-repo > agent 默认
                # （coding=7200）> 全局 env RUNTIME_TASK_TIMEOUT > 3600，见 src/runtime/task_timeout.py。
                # （#470 任务级 env_extra 优先语义已包含：链条第 1 优先级即任务级显式 env。）
                from src.runtime.task_timeout import (resolve_task_timeout, timeout_retry_count,
                                                      retry_backoff_sec)
                task_timeout, _timeout_src = resolve_task_timeout(t)
                # Issue #516 M2：超时失败有限次自动重试（默认 1 次，可配置禁用）
                _retry_max = timeout_retry_count(t)
                _retry_backoff = retry_backoff_sec()
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
                _result = None
                _timeout_attempts = 0  # Issue #516 M2：超时触发的尝试次数（观测/fail 消息用）
                try:
                    if _test_execute is not None:
                        await _test_execute(client, t, e)
                    else:
                        # timeout 只保护流式执行阶段，complete/fail 在 timeout 外处理。
                        # Issue #516 M2：超时失败有限次自动重试——每次超时 kill 引擎 →
                        # 退避 → 从 checkpoint 快照续跑（_execute_task 内注入续跑上下文）；
                        # 重试耗尽仍超时才走 fail 路径。
                        for _attempt in range(1, _retry_max + 2):
                            if _attempt > 1:
                                log.info(f"task {_tid} timeout auto-retry {_attempt - 1}/{_retry_max}"
                                         f" (backoff {_retry_backoff}s, resume from checkpoint)")
                                await asyncio.sleep(_retry_backoff)
                                t["_grape_resume_attempt"] = _attempt
                            try:
                                _result = await asyncio.wait_for(
                                    _execute_task(client, t, e), timeout=task_timeout)
                                break  # 拿到结果（含 _error dict），交给下方 complete/fail 判定
                            except asyncio.TimeoutError:
                                _timeout_attempts = _attempt
                                _will_retry = _attempt <= _retry_max
                                log.warning(
                                    f"task {_tid} timed out after {task_timeout}s (attempt "
                                    f"{_attempt}/{_retry_max + 1}, source={_timeout_src}), killing "
                                    f"child processes and failing" +
                                    (", will retry" if _will_retry else ""))
                                _kill_stale_engine(t, _backend_for_engine(e))
                                _result = None  # timeout → 重试或最终走 fail 路径
                            finally:
                                # MR-2：task.execute span 收口（本轮执行结束，含超时路径）
                                _es = t.pop("__exec_span__", None)
                                if _es is not None:
                                    try:
                                        _es.set_attribute("task.attempt", _attempt)
                                        _es.end()
                                    except Exception:
                                        pass
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
                            # 超时：透传负数 exit_code，服务端死 session 检测（连续3次负数）才能触发。
                            # Issue #516 M2：保留 "timed out after" 签名（M3 观测聚合依赖），
                            # 追加尝试数（含自动重试）便于排障。
                            _to_msg = f"task timed out after {task_timeout}s"
                            if _timeout_attempts > 1:
                                _to_msg += f" (attempts: {_timeout_attempts}, retries: {_retry_max})"
                            await _do_report("failed", _to_msg, -1)
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
                                # issue #416：stderr hint 保尾 2000B（崩溃栈在末尾），
                                # 旧 [:200] 保头会把尾巴上的崩溃栈截掉
                                _stderr_hint = (_result.get("stderr") or "").strip()[-2000:]
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
            # Issue #490：_collect_worker_probe_results 已按指纹去重未变化观测
            _probe_results = _collect_worker_probe_results()
            ok = await client.heartbeat(runtime_ids, log_lines=_recent_logs, metrics=_metrics, probe_results=_probe_results)
            # ⚠️ heartbeat 返回值从 bool 改为 dict——判断逻辑调整
            # dict: None=网络错误 / {"status":"ok",...}=成功 / 其他=异常
            if ok is not None and ok.get("status") == "ok":
                heartbeat_fail_streak = 0
                log.debug(f"heartbeat ok, ids={runtime_ids}")
                # Issue #490：心跳成功后写回已发送观测指纹，未变化的观测下次不再重发
                if _probe_results:
                    _mark_probe_fingerprint_sent(_probe_results)
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
                _rr = await _reregister(client, installed, device_name, cli_version=_read_local_worker_version())
                if _rr is not None:
                    runtime_ids, _new_ebr, installed = _rr
                    if _new_ebr:
                        engine_by_rid = _new_ebr
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
                    os.environ.get("RUNTIME_TOKEN", ""),
                    # 透传设备名：不透传时安装脚本会回落到「worker-<主机名>」，
                    # 设备名/引擎实例名漂移会让服务端按实例名的派发失效。
                    os.environ.get("RUNTIME_DEVICE_NAME", "")
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


async def _perform_upgrade(server: str, token: str, device_name: str = "") -> bool:
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
                 "-Token", token, "-Server", server, "-Dir", _install_dir]
                + (["-Device", device_name] if device_name else []),
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
            # Issue #471 C1：无引擎是正常初始状态（注册成功即成功），不再用 warning 误导
            log.info("No engines installed locally yet, registering with empty engine list "
                     "(install engines later, e.g. `npm install -g opencode-ai`; "
                     "they will auto-register without worker restart)")
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
        if _register_failed(ids, installed):
            log.error("Register failed (check RUNTIME_TOKEN / server URL)")
            return
        # Issue #471 C1：无引擎 worker（ids=[]）注册成功即成功——正常状态进入主循环：
        # 心跳维持凭据在线，周期性 re-register 自动拾起后续安装的引擎。
        # 此前判失败退出 → daemon/systemd 无限重启 + 安装脚本 30s 验证失败回滚循环。
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
