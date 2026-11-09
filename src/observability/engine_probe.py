"""引擎健康探测：每 10 分钟对各 runtime 引擎发起 CLI 测试对话。

探测方式：
- opencode: opencode run "hello"（超时 30s）
- hermes: hermes chat -q "hello" --yolo（超时 30s）
- codearts: codearts run "hello"（超时 30s）
- codex: codex exec --json --sandbox danger-full-access "hello"（超时 30s）

结果存内存 _probe_results，ops/overview 读取展示。
"""
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("devops-bot")

# 引擎探测命令模板
_PROBE_COMMANDS = {
    "opencode": ["opencode", "run", "hello"],
    "hermes": ["hermes", "chat", "-q", "hello", "--yolo", "-Q", "--source", "probe"],
    "codearts": ["codearts", "run", "hello"],
    "codex": ["codex", "exec", "--json", "--sandbox", "danger-full-access", "--skip-git-repo-check", "hello"],
}

_PROBE_TIMEOUT = 30  # 秒
_PROBE_INTERVAL = 600  # 10 分钟

# 探测结果：{engine_name: {"status": "ok"/"fail"/"pending", "latency": 秒, "last_probe": ISO, "error": str}}
_probe_results: dict = {}
_probe_lock = threading.Lock()
_probe_thread = None
_probe_started = False
_last_probe_time: float = 0  # 上次探测完成的时间戳（用于防抖）
_probing: bool = False  # 是否正在探测中（防止并发探测）


def _probe_engine(engine_name: str, cli_path: str) -> dict:
    """对单个引擎发起测试对话，返回探测结果。"""
    # hermes 使用 ACP 模式探活（和实际任务执行路径一致）
    if engine_name == "hermes":
        return _probe_hermes_acp(cli_path)

    cmd = _PROBE_COMMANDS.get(engine_name)
    if not cmd:
        return {"status": "skip", "latency": 0, "error": f"unknown engine: {engine_name}"}

    # 用完整 CLI 路径替换命令中的引擎名
    if cli_path:
        cmd = [cli_path] + cmd[1:]

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        latency = round(time.time() - start, 1)
        if result.returncode == 0:
            output = (result.stdout or "").strip()[:200]
            return {"status": "ok", "latency": latency, "error": "", "output": output}
        else:
            err = (result.stderr or result.stdout or "").strip()[:200]
            return {"status": "fail", "latency": latency, "error": f"exit={result.returncode}: {err}"}
    except subprocess.TimeoutExpired:
        latency = round(time.time() - start, 1)
        return {"status": "fail", "latency": latency, "error": f"timeout ({_PROBE_TIMEOUT}s)"}
    except FileNotFoundError:
        return {"status": "fail", "latency": 0, "error": "CLI not found"}
    except Exception as e:
        latency = round(time.time() - start, 1)
        return {"status": "fail", "latency": latency, "error": str(e)[:200]}


def _probe_hermes_acp(cli_path: str) -> dict:
    """hermes ACP 模式探活：启动 hermes acp 子进程，发 initialize + ping，验证 ACP 通信。

    和实际任务执行路径一致——CLI 模式（hermes chat）走的是不同的代码路径，
    无法发现 ACP 初始化失败的问题。
    """
    import json as _json
    import shutil as _shutil

    agent_cmd = cli_path or _shutil.which("hermes") or _shutil.which("hermes.cmd")
    if not agent_cmd:
        return {"status": "fail", "latency": 0, "error": "hermes CLI not found"}

    _ACP_TIMEOUT = 15  # ACP 探活超时比 CLI 短

    start = time.time()
    proc = None
    try:
        proc = subprocess.Popen(
            [agent_cmd, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0,
            start_new_session=True,
        )

        # 1. 发 initialize 请求
        _init_req = _json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "grape-probe", "version": "1.0"},
                "capabilities": {},
            }
        }) + "\n"

        proc.stdin.write(_init_req)
        proc.stdin.flush()

        # 2. 读 initialize 响应（行级 JSON-RPC）
        _init_resp = None
        for _ in range(3):  # 最多读 3 行找 JSON-RPC 响应
            import select as _sel
            _ready, _, _ = _sel.select([proc.stdout], [], [], _ACP_TIMEOUT)
            if not _ready:
                break
            _line = proc.stdout.readline()
            if not _line:
                break
            _line = _line.strip()
            if _line.startswith("{"):
                try:
                    _msg = _json.loads(_line)
                    if _msg.get("id") == 1 and "result" in _msg:
                        _init_resp = _msg
                        break
                except _json.JSONDecodeError:
                    continue

        if not _init_resp:
            latency = round(time.time() - start, 1)
            _stderr = ""
            try:
                proc.terminate()
                _stderr = (proc.stderr.read() or "")[:200]
            except Exception:
                pass
            return {"status": "fail", "latency": latency,
                    "error": f"ACP initialize no response: {_stderr}"}

        # initialize 成功即表示 ACP 通信正常（子进程启动 + JSON-RPC 响应正常）
        latency = round(time.time() - start, 1)
        _agent_info = ""
        try:
            _agent_info = _init_resp.get("result", {}).get("agentInfo", {}).get("name", "")
        except Exception:
            pass
        return {"status": "ok", "latency": latency, "error": "",
                "output": f"ACP initialize ok ({_agent_info})"}

    except subprocess.TimeoutExpired:
        latency = round(time.time() - start, 1)
        return {"status": "fail", "latency": latency, "error": f"timeout ({_ACP_TIMEOUT}s)"}
    except FileNotFoundError:
        return {"status": "fail", "latency": 0, "error": "hermes CLI not found"}
    except Exception as e:
        latency = round(time.time() - start, 1)
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
            from src.agent.executor import discover_backends
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
    from src.agent.executor import discover_backends
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
        from src.agent.executor import discover_backends
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


def _cleanup_hermes_probe_session(stdout: str = "", stderr: str = ""):
    """从 hermes chat -q -Q 的输出中提取 session_id 并删除，避免 hello 对话污染历史会话。
    -Q 模式下 session_id 输出到 stderr，格式为行首 'session_id: <timestamp_hex>'。"""
    import re as _re
    combined = (stdout or "") + "\n" + (stderr or "")
    # 只匹配行首的 session_id: + hermes session ID 格式（YYYYMMDD_HHMMSS_xxxxxx）
    m = _re.search(r'^session_id:\s*(\d{8}_\d{6}_[a-f0-9]{6})', combined, _re.MULTILINE)
    if not m:
        return
    sid = m.group(1).strip()
    if not sid:
        return
    try:
        subprocess.run(
            ["hermes", "sessions", "delete", sid, "--yes"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
    except Exception:
        pass
