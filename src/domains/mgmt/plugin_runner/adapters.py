"""四型 adapter 的统一调用实现：cli / http / mcp-stdio / mcp-remote。

每个 adapter 只负责「执行并取回结果」；路由、凭据解析、超时、审计由 Runner 编排。
本模块同时提供通道调度层（try_call_with_fallback）与通道健康管理（ChannelHealth）：
同命令多通道按优先级降序逐通道 try，超时/401/5xx 触发回落（Issue #1311，补录 #1083）。
"""
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .credentials import redact
from .errors import CallTimeout, TransportError

# 通道连续失败摘除阈值（连续 N 次回落型失败即摘除，摘除后半开被动探测回接）
FALLBACK_REMOVAL_THRESHOLD = 3
# 半开探测位预算（拍板项 1 退避常量）：摘除通道每条调用链至多放行
# HALF_OPEN_PROBE_SLOTS 个末位被动探测位——「摘除即退避、探测即松绑」，
# 退避粒度 = 调用链频率（不做时间窗退避/常驻心跳，口径见 docs/adr/ADR-0001）。
# 多通道同时摘除时每链仅探测优先级最高的一个，其余维持退避。
HALF_OPEN_PROBE_SLOTS = 1
# script 路径段白名单（与 plugin.schema.json script pattern 同一口径，
# 跨实现对拍钉扎：tests/test_plugin_market_l0.py::TestScriptPathRuleParity）
SCRIPT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def kill_process_group(proc) -> None:
    """终止子进程及其进程组，防止超时残留。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def params_to_cli_flags(params: dict | None) -> list:
    """params -> CLI 旗标约定：--name value；bool 为开关旗标；None 跳过。"""
    flags = []
    for k, v in (params or {}).items():
        if v is None:
            continue
        if v is True:
            flags.append(f"--{k}")
        elif v is False:
            continue
        elif isinstance(v, (list, tuple)):
            for item in v:
                flags += [f"--{k}", str(item)]
        else:
            flags += [f"--{k}", str(v)]
    return flags


def _resolve_command(adapter, venv_dir) -> str:
    """cli/mcp-stdio 共用：venv 存在时优先在插件 venv bin/ 下解析命令（D8）。"""
    command = adapter["command"]
    if venv_dir:
        from .venv import venv_bin_command
        resolved = venv_bin_command(venv_dir, command)
        if resolved:
            command = resolved
    return command


def _child_env(inject_env=None, venv_dir=None) -> dict:
    """cli/mcp-stdio 共用：子进程环境；venv 存在时把插件 venv bin/ 前置到 PATH。"""
    env = {**os.environ, **(inject_env or {})}
    if venv_dir:
        from .venv import venv_bin_dir
        bin_dir = venv_bin_dir(venv_dir)
        if bin_dir:
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


def _initialize_request() -> dict:
    """MCP initialize 请求（mcp-stdio/mcp-remote 共用）。"""
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "grape-plugin-runner", "version": "1.0.0"},
    }}


def _extract_text(result: dict) -> str:
    """MCP tools/call 结果文本提取（mcp-stdio/mcp-remote 共用）。"""
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and content[0].get("text") is not None:
        return content[0]["text"]
    return json.dumps(result, ensure_ascii=False)


def call_cli(adapter, params, *, inject_env=None, inject_argv=None, timeout_seconds=None,
             workdir=None, venv_dir=None):
    """cli adapter：子进程执行，返回 (exit_code, stdout, stderr)。

    - 凭据仅在此刻进入子进程 env/argv（执行时注入）。
    - timeout_seconds 为墙钟超时；超时杀整个进程组并抛 CallTimeout。
    - venv_dir 存在时优先在插件 venv bin/ 下解析命令（D8 每插件独立 venv）。
    """
    command = _resolve_command(adapter, venv_dir)
    args = list(adapter.get("args") or [])
    argv = [command] + args + params_to_cli_flags(params) + list(inject_argv or [])
    proc = subprocess.Popen(
        argv,
        env=_child_env(inject_env, venv_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workdir or None,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        raise CallTimeout(f"cli 调用超时终止（>{timeout_seconds}s）") from None
    return proc.returncode, out, err


def call_http(adapter, params, *, inject_headers=None, inject_query=None,
              timeout_seconds=None):
    """http adapter：按 adapter.method 调远端端点。

    GET（缺省）：params 并入 query——与历史行为完全一致。
    非 GET：params 作为 JSON body 发送（REST 写端点），query 仍并入注入位。
    返回 (status, body_text, error)。

    仅 Timeout 归一为 CallTimeout；其余网络失败归一 TransportError，
    不允许裸 requests 异常绕过 Runner 的审计与掩码出口。
    """
    import requests

    method = str((adapter.get("method") or "GET")).upper()
    query = {**(inject_query or {})}
    try:
        if method == "GET":
            query.update(params or {})
            resp = requests.get(
                adapter["url"],
                params=query,
                headers=inject_headers or {},
                timeout=timeout_seconds,
            )
        else:
            resp = requests.request(
                method,
                adapter["url"],
                params=query,
                json=params or {},
                headers=inject_headers or {},
                timeout=timeout_seconds,
            )
    except requests.Timeout as e:
        raise CallTimeout(str(e)) from e
    except requests.RequestException as e:
        raise TransportError(f"HTTP 请求失败: {e}") from e
    err = None if resp.ok else f"HTTP {resp.status_code}"
    return resp.status_code, resp.text, err


def call_mcp_stdio(adapter, params, *, command_name, inject_env=None, timeout_seconds=None,
                   workdir=None, venv_dir=None):
    """mcp-stdio adapter：子进程 MCP 服务器，newline-delimited JSON-RPC 2.0。

    流程：initialize -> notifications/initialized -> tools/call。
    返回 (exit_code, result_text, error)。
    timeout_seconds 是整段会话的墙钟预算（initialize + tools/call），
    任一步骤超预算即杀进程组并抛 CallTimeout，宿主不冻结。
    """
    command = _resolve_command(adapter, venv_dir)
    args = list(adapter.get("args") or [])
    deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
    proc = subprocess.Popen(
        [command] + args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(inject_env, venv_dir),
        cwd=workdir or None,
        start_new_session=True,
    )

    def send(obj):
        # 对端关闭（如 BrokenPipeError）归一为 TransportError：
        # 不允许裸 OSError 绕过 Runner 的审计与掩码出口（每次调用必须留一行审计）。
        try:
            proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            proc.stdin.flush()
        except OSError as e:
            raise TransportError(f"mcp-stdio 会话中断（写入失败）: {e}") from e

    def recv():
        # stderr tail 可能回显凭据明文：用 env 注入位的明文值做掩码
        return _recv_json(proc, deadline, timeout_seconds,
                          redact_secrets=list((inject_env or {}).values()))

    result_text, error = None, None
    try:
        send(_initialize_request())
        recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": command_name,
            "arguments": params or {},
        }})
        resp = recv()
        if "error" in resp:
            error = f"MCP error: {resp['error'].get('message')}"
        else:
            result_text = _extract_text(resp.get("result") or {})
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
    return proc.returncode, result_text, error


def _recv_json(proc, deadline, timeout_seconds, redact_secrets=()):
    """在墙钟预算内从 MCP 服务器 stdout 读一行 JSON（select 有界读，不阻塞宿主）。

    超预算：杀进程组并抛 CallTimeout；流提前 EOF：杀进程组并抛 TransportError，
    stderr 尾部先经 redact 掩码（崩溃输出可能回显凭据明文，不允许裸抛带明文）。
    """
    fd = proc.stdout.fileno()
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    buf = bytearray()
    try:
        while b"\n" not in buf:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                kill_process_group(proc)
                raise CallTimeout(f"mcp-stdio 调用超时终止（>{timeout_seconds}s 墙钟）")
            if not sel.select(timeout=remaining):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                kill_process_group(proc)
                tail = b""
                try:
                    tail = (proc.stderr.read() or b"")[-2000:]
                except Exception:
                    pass
                raise TransportError(
                    "mcp-stdio 服务器提前退出: "
                    + redact(tail.decode("utf-8", "replace"), redact_secrets)
                )
            buf += chunk
    finally:
        sel.close()
    line = bytes(buf).split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise TransportError(f"mcp-stdio 响应非法 JSON: {e}") from e


def call_mcp_remote(adapter, params, *, command_name, inject_headers=None, timeout_seconds=None):
    """mcp-remote adapter：远端 MCP（streamable HTTP）。

    POST JSON-RPC：initialize（取会话头）-> tools/call。返回 (status, result_text, error)。
    - inject_headers：凭据经 header 注入位送达远端（与 http 同语义）。
    - timeout_seconds 是两段 POST 共享的墙钟预算：tools/call 只用 initialize
      之后的剩余预算，总耗时不超 timeout_seconds；initialize 非 2xx 不继续。
    - 仅 Timeout 归一为 CallTimeout；其余网络/协议失败归一为 TransportError。
    """
    import requests

    url = adapter["url"]
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               **(inject_headers or {})}
    deadline = time.monotonic() + timeout_seconds if timeout_seconds else None

    def remaining():
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        return left if left > 0 else 0.001

    with requests.Session() as session:
        try:
            resp = session.post(
                url,
                json=_initialize_request(),
                headers=headers,
                timeout=remaining(),
            )
            if not resp.ok:
                raise TransportError(
                    f"mcp-remote initialize 失败: HTTP {resp.status_code}"
                )
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                headers["Mcp-Session-Id"] = sid
            resp2 = session.post(
                url,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": command_name,
                    "arguments": params or {},
                }},
                headers=headers,
                timeout=remaining(),
            )
            try:
                body = resp2.json()
            except ValueError as e:
                raise TransportError(
                    f"mcp-remote 响应非法 JSON: HTTP {resp2.status_code}: {e}"
                ) from e
        except requests.Timeout as e:
            raise CallTimeout(f"mcp-remote 调用超时终止（>{timeout_seconds}s 墙钟）") from e
        except requests.RequestException as e:
            raise TransportError(f"mcp-remote 请求失败: {e}") from e
        if "error" in body:
            return resp2.status_code, None, f"MCP error: {body['error'].get('message')}"
        return resp2.status_code, _extract_text(body.get("result") or {}), None


# ============================== 第五型：script（签名脚本通道） ==============================

def script_path_allowed(script) -> bool:
    """script 路径白名单：插件目录内相对路径。

    禁止：绝对路径、`..` 穿越、`.` 与空段、反斜杠、盘符，以及段内非法字符
    （每段须匹配 SCRIPT_SEGMENT_RE，与 plugin.schema.json script pattern /
    plugin_market 校验器同口径，TestScriptPathRuleParity 对拍钉扎；
    script 是子进程执行入口，路径越界 = 任意代码执行面，加载期与运行时双重把关）。
    """
    if not isinstance(script, str) or not script:
        return False
    if script[0] in "/\\" or "\\" in script:
        return False
    if len(script) >= 2 and script[1] == ":":
        return False
    return all(SCRIPT_SEGMENT_RE.fullmatch(p) for p in script.split("/"))


def resolve_script_path(script, plugin_dir) -> Path:
    """白名单校验 + 解析为插件目录内的绝对路径；越界/缺失即 TransportError。

    运行时复核（纵深防御）：即使加载期校验被绕过（如内存篡改 manifest），
    执行前仍以 resolve 后的相对关系拒绝越目录访问。
    """
    if not plugin_dir:
        raise TransportError("script 通道需要插件目录（plugin_dir）以解析脚本白名单")
    if not script_path_allowed(script):
        raise TransportError(f"script 路径非法（须为插件目录内相对路径）: {script!r}")
    base = Path(plugin_dir).resolve()
    target = (base / script).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise TransportError(f"script 越出插件目录: {script!r}") from None
    if not target.is_file():
        raise TransportError(f"script 文件不存在: {script!r}")
    return target


def call_script(adapter, params, *, command_name=None, inject_env=None,
                timeout_seconds=None, workdir=None, plugin_dir=None):
    """script adapter：子进程执行插件目录内签名脚本，返回 (exit_code, stdout, stderr)。

    - stdin 收 JSON {"command", "params"}；stdout 须回 JSON 请求产物
      （如 {"params": {...签名后...}, "headers": {...}}），由 Runner 交 call_http 等传输通道。
    - 凭据经 inject_env（env 注入位）仅在执行瞬间进入子进程环境（与 cli 同语义）。
    - 复用 _child_env/kill_process_group：超时杀整个进程组，宿主不冻结。
    """
    target = resolve_script_path(adapter.get("script"), plugin_dir)
    payload = json.dumps(
        {"command": command_name, "params": params or {}}, ensure_ascii=False
    )
    if target.suffix == ".py":
        argv = [sys.executable, str(target)]
    else:
        argv = [str(target)]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(inject_env),
        cwd=workdir or str(target.parent),
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=payload, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        raise CallTimeout(f"script 调用超时终止（>{timeout_seconds}s）") from None
    return proc.returncode, out, err


# ============================== 通道调度层（failover） ==============================

def is_fallback_worthy(outcome: str, exit_code: int | None) -> bool:
    """回落触发口径（与 VOD 收敛口径一致）：超时 / 退出码或 HTTP 状态 401、5xx。

    exit_code 是通道「状态位」：http/mcp-remote 传 HTTP 状态码，
    cli/mcp-stdio/script 传进程退出码——故 cli 进程退出码恰为 401/500-599
    时同样触发回落（同一状态位，同一口径）。
    其余失败（如 400 等 4xx 业务错误、一般非零退出码）不回落：可能是调用方
    错误，盲目回落会放大副作用（如重复下单）。
    """
    if outcome == "timeout":
        return True
    if outcome == "error" and exit_code is not None:
        return exit_code == 401 or 500 <= exit_code <= 599
    return False


def try_call_with_fallback(channels, call_one, *, health=None, on_fallback=None):
    """通道调度层：按优先级降序逐通道 try，回落型失败按序回落。

    - channels：通道（adapter dict）列表，优先级降序（摘除通道由
      ChannelHealth.build_chain 编入末位半开探测位后传入）。
    - call_one(adapter) -> CallResult：单通道执行；凭据/引擎等配置类错误直接抛出，
      不参与回落（确定性配置问题回落只会复制同一样错误）。
    - health：ChannelHealth；on_fallback(failed, nxt, reason, duration_ms)：回落审计回调。
    - 成功返回 (CallResult, adapter)；全链失败返回首个（原始主通道）失败结果。
    """
    first_failure = None
    for idx, adapter in enumerate(channels):
        t0 = time.monotonic()
        result = call_one(adapter)
        duration_ms = int((time.monotonic() - t0) * 1000)
        if result.outcome == "ok":
            if health is not None:
                health.record_success(adapter.get("id"))
            return result, adapter
        if first_failure is None:
            first_failure = result
        worthy = is_fallback_worthy(result.outcome, result.exit_code)
        if health is not None and worthy:
            health.record_failure(adapter.get("id"))
        if worthy and idx + 1 < len(channels):
            if on_fallback is not None:
                on_fallback(adapter, channels[idx + 1],
                            result.error or result.outcome, duration_ms)
            continue
        break
    return first_failure, (channels[0] if channels else None)


class ChannelHealth:
    """通道健康管理：连续回落型失败摘除 + 半开被动探测回接。

    - 连续失败达 removal_threshold（默认 3）即摘除该通道；
    - 摘除后不参与常规优先级排序，但在同命令调用链末位半开放行至多
      HALF_OPEN_PROBE_SLOTS（=1）个探测位（被动探测），成功即回接清零，
      失败维持摘除；
    - 仅回落型失败（超时/401/5xx）计入连续失败：业务错误不是通道健康信号；
    - 退避口径（拍板项 1）：摘除即退避、探测即松绑——探测位预算即退避节流
      常量，退避粒度 = 调用链频率（详见 docs/adr/ADR-0001 退避口径一节）。
    """

    def __init__(self, removal_threshold: int = FALLBACK_REMOVAL_THRESHOLD):
        if removal_threshold < 1:
            raise ValueError("removal_threshold 必须 >= 1")
        self._threshold = removal_threshold
        self._consecutive: dict = {}
        self._removed: set = set()
        self._lock = threading.Lock()

    def record_success(self, adapter_id) -> None:
        with self._lock:
            self._consecutive.pop(adapter_id, None)
            self._removed.discard(adapter_id)

    def record_failure(self, adapter_id) -> None:
        with self._lock:
            count = self._consecutive.get(adapter_id, 0) + 1
            self._consecutive[adapter_id] = count
            if count >= self._threshold:
                self._removed.add(adapter_id)

    def is_removed(self, adapter_id) -> bool:
        with self._lock:
            return adapter_id in self._removed

    def build_chain(self, channels) -> list:
        """健康过滤：摘除通道移出常规优先级序，末位放行至多 HALF_OPEN_PROBE_SLOTS 个探测位。

        多通道同时摘除时每链仅探测优先级最高的一个（removed[:HALF_OPEN_PROBE_SLOTS]，
        探测位预算即退避节流：其余摘除通道维持退避，本链不尝试）。
        注意半开探测只在链实际走到探测位时执行：前序（未摘除）通道成功提前
        返回时本链不探测，回接延迟至前序通道也回落型失败的调用（被动探测
        相对主动心跳的固有取舍，见 ADR-0001 回接活性口径）。
        """
        with self._lock:
            active = [a for a in channels if a.get("id") not in self._removed]
            removed = [a for a in channels if a.get("id") in self._removed]
        return active + removed[:HALF_OPEN_PROBE_SLOTS]
