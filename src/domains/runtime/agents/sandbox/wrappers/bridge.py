#!/usr/bin/env python3
"""PTY stdin → fifo 桥——显式打开 PTY slave 路径，不依赖 stdin 继承。

用法：python3 bridge.py /dev/pts/N
启动方在 shell 里用 tty 命令拿到 PTY slave 路径，作为参数传入。

退出条件：
- exit_code 文件出现 → hermes 已退出，桥退出
- PTY 关闭（os.read 返回空）→ break
- 写 fifo 时 BrokenPipeError → break

零第三方依赖，只用 Python 标准库。
"""
import os
import sys
import select

FIFO = "/tmp/_grape_acp/hermes_in"
EXIT = "/tmp/_grape_acp/exit_code"
RX_LOG = "/tmp/_grape_acp/bridge_rx.txt"

def main():
    pty_path = sys.argv[1] if len(sys.argv) > 1 else "/dev/tty"
    pty_fd = os.open(pty_path, os.O_RDONLY | os.O_NONBLOCK)
    fifo_fd = os.open(FIFO, os.O_WRONLY)

    while True:
        if os.path.exists(EXIT):
            break
        r, _, _ = select.select([pty_fd], [], [], 0.2)
        if r:
            try:
                data = os.read(pty_fd, 65536)
            except (OSError, BlockingIOError):
                continue
            if not data:
                break
            # 记录收到的数据（调试用）
            try:
                with open(RX_LOG, "ab") as f:
                    f.write(data)
            except:
                pass
            try:
                os.write(fifo_fd, data)
            except BrokenPipeError:
                break
    os.close(fifo_fd)
    os.close(pty_fd)

if __name__ == "__main__":
    main()
