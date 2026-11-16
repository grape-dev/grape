#!/usr/bin/env python3
"""opencode Wrapper — 在 CubeSandbox 沙箱内执行 opencode 命令。

NDJSON 共享原语由 _ndjson_common.py 提供。
本文件只定义 opencode 的启动命令 + session 降级重试逻辑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ndjson_common import run_once, emit_done, emit


def main():
    prompt = os.environ.get("GRAPE_TASK_PROMPT", "")
    if not prompt:
        sys.exit(1)

    cwd = os.environ.get("GRAPE_TASK_CWD", "")
    timeout = int(os.environ.get("GRAPE_TASK_TIMEOUT", "600"))
    session_id = os.environ.get("GRAPE_TASK_SESSION", "")

    cmd = ["opencode", "run", "--format", "json", "--dangerously-skip-permissions"]
    if session_id:
        cmd.extend(["--session", session_id])
    cmd.append(prompt)

    _exit, _stderr, _sid = run_once(cmd, timeout, cwd, "opencode")

    # session not found 降级重试——opencode 专属逻辑，不进入公共模块
    if _exit != 0 and session_id and "session not found" in (_stderr or "").lower():
        emit({"type": "text", "content": "[opencode_wrapper] Session not found, retrying..."})
        cmd_retry = ["opencode", "run", "--format", "json", "--dangerously-skip-permissions", prompt]
        _exit, _stderr, _sid = run_once(cmd_retry, timeout, cwd, "opencode")

    emit_done("opencode", _exit, _stderr, _sid)


if __name__ == "__main__":
    main()
