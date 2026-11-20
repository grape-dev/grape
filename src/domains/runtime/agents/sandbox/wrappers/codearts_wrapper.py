#!/usr/bin/env python3
"""codearts Wrapper — 在 CubeSandbox 沙箱内执行 codearts 命令。

NDJSON 共享原语由 _ndjson_common.py 提供。
本文件只定义 codearts 的启动命令。

差异（vs opencode_wrapper）：
1. 启动命令：codearts run --format json --auto（--auto = bash_mode=always_allow）
2. 无 --session 支持（codearts 用 KERNEL_DATA_DIR 做 per-task 隔离，不用 session）
3. 用绝对路径启动（~/.codeartsdoer/installers/codearts），不依赖 PATH
4. 支持 CODEARTS_MODEL 环境变量传 --model
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ndjson_common import run_once, emit_done


def main():
    prompt = os.environ.get("GRAPE_TASK_PROMPT", "")
    if not prompt:
        sys.exit(1)

    cwd = os.environ.get("GRAPE_TASK_CWD", "")
    timeout = int(os.environ.get("GRAPE_TASK_TIMEOUT", "600"))

    # 用绝对路径启动——PTY 内 subprocess.Popen 不走 login shell，PATH 可能没配
    codearts_bin = os.path.expanduser("~/.codeartsdoer/installers/codearts")
    cmd = [codearts_bin, "run", "--format", "json", "--auto"]
    # 模型名以 CODEARTS_MODEL 环境变量为准（与 codearts.py build_args 一致）
    model = os.environ.get("CODEARTS_MODEL")
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    _exit, _stderr, _sid = run_once(cmd, timeout, cwd, "codearts")
    emit_done("codearts", _exit, _stderr, _sid)


if __name__ == "__main__":
    main()
