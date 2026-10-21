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
                # Issue #404：记录探测前状态，用于 fail→ok 转换检测
                _prev_status = ""
                with _probe_lock:
                    _prev_status = (_probe_results.get(eng["name"], {}) or {}).get("status", "")
                result = _probe_engine(eng["name"], eng.get("cli_path", ""))
                result["last_probe"] = datetime.now(timezone.utc).isoformat()
                with _probe_lock:
                    _probe_results[eng["name"]] = result
                log.info(f"engine probe: {eng['name']} -> {result['status']} ({result['latency']}s)")
                # Issue #404：fail→ok 转换 → 触发场景恢复钩子（标记可重派事件）
                if _prev_status == "fail" and result.get("status") == "ok":
                    try:
                        _on_engine_recovery(eng["name"])
                    except Exception as _re:
                        log.warning(f"engine probe recovery hook failed: {_re}")
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
            # Issue #404：记录探测前状态，用于 fail→ok 转换检测
            _prev_status = ""
            with _probe_lock:
                _prev_status = (_probe_results.get(eng["name"], {}) or {}).get("status", "")
            log.info(f"engine probe (manual): testing {eng['name']}...")
            result = _probe_engine(eng["name"], eng.get("cli_path", ""))
            result["last_probe"] = datetime.now(timezone.utc).isoformat()
            with _probe_lock:
                _probe_results[eng["name"]] = result
            log.info(f"engine probe (manual): {eng['name']} -> {result['status']} ({result['latency']}s)")
            # Issue #404：fail→ok 转换 → 触发场景恢复钩子
            if _prev_status == "fail" and result.get("status") == "ok":
                try:
                    _on_engine_recovery(eng["name"])
                except Exception as _re:
                    log.warning(f"engine probe (manual) recovery hook failed: {_re}")
        _last_probe_time = time.time()
    finally:
        with _probe_lock:
            _probing = False


def _on_engine_recovery(engine_name: str) -> None:
    """Issue #404：引擎恢复 online 钩子——标记场景下可重派事件（不自动批量重派）。

    触发时机：`_probe_loop` / `probe_all_now` 探测到某引擎 fail→ok 转换时调用。
    作用：
    1. 反查绑定该引擎的场景（扫 devops_pipeline_scene.nodes_json）；
    2. 对每个命中场景，调 get_scene_pool_engine_ids 判定 pool 是否非 None（=引擎已恢复）；
    3. 统计该场景下 failed 且 result 含"场景绑定的引擎全部离线"的事件数，log 提示可重派。

    纯主控侧、不改 worker、无 schema 变更；实际重派由用户手动调
    `POST /events/{seq}/redispatch`（events.py 已重读 pool 校验）。

    性能：devops_pipeline_scene 行数有限（场景表通常 <1000 行），仅探测恢复时触发，
    非热路径。异常吞掉只 log（钩子永不影响探测主循环）。
    """
    if not engine_name:
        return
    try:
        from src.session_manager import db, db_lock
        from src.runtime.engine_select import get_scene_pool_engine_ids
        # 1. 反查绑定该引擎名的 runtime_id（按 name 匹配 agent_runtimes）
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM agent_runtimes WHERE name=?", (engine_name,))
            _rid_rows = [int(r[0]) for r in c.fetchall() if r and r[0]]
        if not _rid_rows:
            log.info(f"engine_recovery: {engine_name} 无对应 runtime 行，跳过场景反查")
            return
        _rid_set = set(_rid_rows)
        # 2. 扫场景表，找 nodes_json 中 engine_runtime_ids 含该 runtime_id 的场景
        _hit_scenes: list[tuple[str, str, str, str]] = []  # (scene_id, repo_key, agent_tag, skill)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT scene_id, repo_key, nodes_json, is_active FROM devops_pipeline_scene")
            for _sid, _rk, _nj, _ia in c.fetchall():
                if not _nj:
                    continue
                try:
                    import json as _json
                    _nodes = _json.loads(_nj) or []
                except Exception:
                    continue
                for _n in _nodes:
                    _eids = _n.get("engine_runtime_ids") or []
                    if any(int(e) in _rid_set for e in _eids if e):
                        _tag = _n.get("tag") or _n.get("id") or ""
                        _hit_scenes.append((_sid or "", _rk or "", _tag, _n.get("skill", "")))
                        break  # 一个场景只记一次
        if not _hit_scenes:
            log.info(f"engine_recovery: {engine_name} 无场景绑定，跳过")
            return
        # 3. 逐场景判定 pool 是否恢复（非 None = 引擎已在线）
        _recovered: list[tuple[str, str, str, int]] = []  # (scene_id, repo_key, agent, redispatchable_count)
        for _sid, _rk, _tag, _skill in _hit_scenes:
            try:
                _pool = get_scene_pool_engine_ids(_sid, _tag, repo_key=_rk)
                if _pool is None:
                    continue  # 仍全灭
                # pool 恢复 → 统计可重派事件数
                with db_lock:
                    c = db.cursor()
                    c.execute(
                        "SELECT COUNT(*) FROM events WHERE status='failed'"
                        " AND result LIKE '%场景绑定的引擎全部离线%'"
                        " AND (agent_type=? OR 1=1)", (_tag,))
                    _cnt = int(c.fetchone()[0] or 0)
                _recovered.append((_sid, _rk, _tag, _cnt))
            except Exception as _e:
                log.warning(f"engine_recovery: scene={_sid} agent={_tag} check failed: {_e}")
        if _recovered:
            _summary = "; ".join(
                f"scene={s} agent={a} redispatchable={n}" for s, _, a, n in _recovered)
            log.info(f"engine_recovery: {engine_name} 恢复，可重派事件：{_summary}")
        else:
            log.info(f"engine_recovery: {engine_name} 恢复，但无场景 pool 恢复或无可重派事件")
    except Exception as _e:
        log.warning(f"engine_recovery: {engine_name} hook failed: {_e}")


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
