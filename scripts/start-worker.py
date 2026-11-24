#!/usr/bin/env python3
"""Issue #4203：worker uvicorn launcher —— 预 bind socket 后再启动（先监听再 lifespan）。

背景：master-004 OOM SIGKILL 后 systemd 重启 + uvicorn 启动共 12.1s，期间网关 502 共
339 次。窗口构成 = RestartSec(5s) + uvicorn bind + lifespan 初始化。systemd 侧已把
RestartSec 降到 1s；本脚本把「bind 端口」从 uvicorn 生命周期里提到前面——socket 先
listen，uvicorn 直接在既有 socket 上 accept，lifespan 再慢端口也不再拒绝连接
（kernel backlog 已就绪，连接排队等待 app 就绪）。

实现要点：
- uvicorn 0.54 的 ``--fd`` 路径按 AF_UNIX 包 socket（TCP fd 会报
  ``OSError: [Errno 88] Socket operation on non-socket``，实测），所以这里不用
  exec + ``--fd``，而是对齐 ``uvicorn.main.run`` 的多进程分支：把预 bind 的
  socket 对象经 ``Multiprocess(config, sockets=[...])`` / ``server.run(sockets=[...])``
  直接交给 uvicorn（这正是 uvicorn 自身 bind_socket 之后做的事，单/多 worker 一致）。
- 多 worker 子进程经 multiprocessing spawn 重跑本文件：所有顶层代码必须 import-safe，
  bind 只在 ``__main__`` 分支执行（缺守卫会让每个 worker 再 bind 一次 → EADDRINUSE
  崩溃循环，实测教训）。
- bind 失败显式退出非 0，交给 systemd Restart 拉起，绝不静默换端口。

环境变量（与 deploy/systemd/grape-worker-server.service / scripts/grape-systemd-run.sh
对齐，均有缺省）：
- GRAPE_WORKER_PORT / GRAPE_PORT   监听端口（缺省 38083，对齐 #3764 端口事实源）
- GRAPE_HOST                        监听地址（缺省 0.0.0.0）
- GRAPE_UVICORN_WORKERS             worker 数（缺省 2；grape@ 启动路径显式传 1 保持
                                    单进程生产语义）
- GRAPE_WORKER_APP                  ASGI 应用导入串（缺省 worker app）
"""
import os
import socket

_BACKLOG = 2048
_APP_IMPORT = "src.api.grape_worker_server:app"
_DEFAULT_PORT = 38083
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_WORKERS = 2


def listen_socket(host: str, port: int) -> socket.socket:
    """预 bind + listen：SO_REUSEADDR 让 systemd 快速重启不撞 TIME_WAIT。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(_BACKLOG)
    return s


def _run(sock: socket.socket, workers: int) -> None:
    """对齐 uvicorn.main.run 的 worker 分支，只是 socket 换成预 bind 的那只。"""
    import uvicorn
    config = uvicorn.Config(os.environ.get("GRAPE_WORKER_APP") or _APP_IMPORT,
                            workers=workers)
    if workers > 1:
        from uvicorn.supervisors import Multiprocess
        Multiprocess(config, sockets=[sock]).run()
    else:
        server = uvicorn.Server(config=config)
        server.run(sockets=[sock])


def resolve_params() -> tuple:
    """(host, port, workers)：环境变量优先，缺省对齐单元/端口事实源（可单测）。"""
    port = int(os.environ.get("GRAPE_WORKER_PORT") or os.environ.get("GRAPE_PORT")
               or _DEFAULT_PORT)
    host = os.environ.get("GRAPE_HOST") or _DEFAULT_HOST
    workers = int(os.environ.get("GRAPE_UVICORN_WORKERS") or str(_DEFAULT_WORKERS))
    return host, port, workers


def main() -> int:
    host, port, workers = resolve_params()
    try:
        sock = listen_socket(host, port)
    except OSError as e:
        # spawn 子进程重跑 main() 不应发生（有 __main__ 守卫）；真 bind 失败=端口被占，
        # 退出非 0 交给 systemd Restart，绝不静默换端口。
        import sys
        print(f"[start-worker] bind {host}:{port} failed: {e}", file=sys.stderr, flush=True)
        return 1
    print(f"[start-worker] pre-bound {host}:{port} (backlog={_BACKLOG}, workers={workers})",
          flush=True)
    _run(sock, workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
