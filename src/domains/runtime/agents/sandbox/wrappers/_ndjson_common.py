#!/usr/bin/env python3
"""NDJSON 公共模块——opencode_wrapper / codearts_wrapper 共享。

只提供共享原语：emit + convert_event + run_once + emit_done。
不含引擎特定逻辑（session 重试由 wrapper 自己编排）。
零第三方依赖，只用 Python 标准库。沙箱内通过 sys.path.insert 导入。
"""
import json
import os
import signal
import subprocess
import sys


def emit(event: dict):
    """输出一个 NDJSON 事件到 stdout，立即 flush。"""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def convert_event(raw: dict) -> dict | None:
    """将 opencode/codearts 原生 NDJSON 转为统一扁平格式。

    opencode 格式：{"type":"text","part":{"text":"...","type":"text"}}
    统一格式：    {"type":"text","content":"..."}

    codearts 是 opencode 的华为 fork，NDJSON 输出格式完全兼容。
    此函数是"同一个协议"的唯一实现，不复制到各 wrapper。
    """
    etype = raw.get("type", "")
    part = raw.get("part", {}) or {}

    if etype == "text":
        return {"type": "text", "content": part.get("text", "")}
    elif etype == "reasoning":
        return {"type": "reasoning", "content": part.get("text", "")}
    elif etype == "tool_use":
        return {"type": "tool_use", "tool": part.get("tool", ""),
                "input": part.get("input", part.get("state", {}).get("input", {}))}
    elif etype == "step_start":
        return {"type": "step_start", "sessionID": part.get("sessionID", raw.get("sessionID", ""))}
    elif etype == "step_finish":
        tokens = part.get("tokens", {})
        return {"type": "step_finish", "tokens": tokens.get("total", 0)}
    elif etype == "error":
        err = raw.get("error", {})
        return {"type": "error", "content": err.get("data", {}).get("message", str(err))}
    else:
        return {"type": etype, "raw": True}


def run_once(cmd: list[str], timeout: int, cwd: str, engine_name: str
             ) -> tuple[int, str, str]:
    """执行一个进程 + 流式读取 NDJSON + 返回结果。

    Args:
        cmd: 启动命令列表
        timeout: 超时秒数
        cwd: 工作目录（~ 自动展开）
        engine_name: 引擎名（错误消息用）

    Returns:
        (exit_code, stderr_text, session_id)
        超时返回 (1, "timeout after {timeout}s", "")
    """
    _cwd = os.path.expanduser(cwd) if cwd else None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=_cwd if _cwd and os.path.isdir(_cwd) else None,
            bufsize=0,
            start_new_session=True,
        )
    except FileNotFoundError:
        sys.stderr.write(f"[{engine_name}_wrapper] {cmd[0]} not found\n")
        sys.exit(127)

    _session_id = ""
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            try:
                raw = json.loads(line_str)
                if not _session_id:
                    _session_id = raw.get("sessionID", "") or raw.get("part", {}).get("sessionID", "")
                converted = convert_event(raw)
                if converted:
                    emit(converted)
            except json.JSONDecodeError:
                pass
        proc.wait(timeout=timeout)
        _exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        proc.wait()
        emit({"type": "done", "exit_code": 1, "session_id": _session_id,
              "errors": [f"timeout after {timeout}s"]})
        sys.exit(1)

    _stderr_text = ""
    try:
        if proc.stderr:
            _stderr_text = proc.stderr.read().decode("utf-8", errors="replace")[:2000]
    except Exception:
        pass

    return (_exit_code, _stderr_text, _session_id)


def emit_done(engine_name: str, exit_code: int, stderr_text: str, session_id: str):
    """生成 done 事件 + sys.exit。"""
    _errors = []
    if exit_code != 0:
        _errors.append(f"{engine_name} exit_code={exit_code}")
        if stderr_text:
            _errors.append(f"stderr: {stderr_text[:500]}")
    emit({"type": "done", "exit_code": exit_code, "session_id": session_id,
          "errors": _errors, "stderr": stderr_text})
    sys.exit(exit_code)
