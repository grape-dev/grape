"""stage2 TCP→UDS 桥接——沙箱内启动，Agent 的 HTTP_PROXY 指向此进程。

监听 127.0.0.1:LOCAL_PORT，将 TCP 流量通过 UDS 文件转发到宿主机 NetworkProxy。

用法: python3 _grape_stage2.py LOCAL_PORT UDS_PATH

诊断日志写到 stderr（bwrap 的 `2>&1` 重定向到 /dev/null 时不会干扰 stdout）。
但如果需要调试，bwrap.py 的 stage2_script 可以改成不重定向 stderr。
日志格式：stage2[pid] LEVEL message
"""
import socket
import threading
import sys
import os
import signal
import traceback
import time
import errno

listen_port = int(sys.argv[1])
uds_path = sys.argv[2]

_pid = os.getpid()

def _log(level, msg):
    """stderr 日志，带 pid 和时间戳。"""
    ts = time.strftime("%H:%M:%S", time.localtime())
    sys.stderr.write(f"stage2[{_pid}] {ts} {level} {msg}\n")
    sys.stderr.flush()

# ===== signal handler：记录收到什么信号，什么时候 =====
_sig_received = None
def _sig_handler(signum, frame):
    global _sig_received
    _sig_received = signum
    _name = signal.Signals(signum).name if hasattr(signal, 'Signals') else f"signal {signum}"
    _log("WARN", f"received {_name}({signum}), raising KeyboardInterrupt to exit gracefully")
    raise KeyboardInterrupt

for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    try:
        signal.signal(_sig, _sig_handler)
    except (OSError, ValueError):
        pass  # 某些信号在特定环境下不能注册

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', listen_port))
srv.listen(5)

# 写 ready 标记文件到 cwd（沙箱内可写）
ready_file = os.path.join(os.getcwd(), f'_grape_stage2_ready_{listen_port}')
try:
    with open(ready_file, 'w') as f:
        f.write('ok')
except OSError:
    pass  # cwd 可能也不可写，ready 文件不是必须的

_log("INFO", f"listening on 127.0.0.1:{listen_port}, uds={uds_path}")

def forward(src, dst):
    """单向转发：src → dst。

    一边 EOF 时 shutdown dst 写方向（通知对端）。
    对端 forward 的 recv 会收到 EOF，同样 shutdown。
    最后 close 由 GC 回收——close 幂等，重复调用无害。

    参考 anthropics/sandbox-runtime 的 pipe() + close 联动设计：
      upstream.pipe(socket)
      socket.pipe(upstream)
      socket.on('close', () => upstream.destroy())
      upstream.on('close', () => upstream.destroy())
    """
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception as e:
        _log("DEBUG", f"forward ended: {e!r}")
    finally:
        # shutdown 写方向——通知对端我这边发完了
        # 对端 recv 收到 EOF 后也会 shutdown 它的 dst（=我的 src）
        try: dst.shutdown(socket.SHUT_WR)
        except OSError: pass
        # close 两边——幂等，对端可能已经 close 了
        try: src.close()
        except OSError: pass
        try: dst.close()
        except OSError: pass

# ===== 主循环：accept 和 connect 分开处理 =====
_conn_count = 0
while True:
    # --- accept 阶段 ---
    try:
        conn, addr = srv.accept()
    except InterruptedError:
        _log("DEBUG", "accept interrupted (EINTR), retrying")
        continue
    except KeyboardInterrupt:
        # signal handler raise 的
        _log("WARN", f"accept interrupted by signal {_sig_received}, exiting")
        break
    except OSError as e:
        _log("ERROR", f"accept failed: errno={e.errno} {e!r}")
        if e.errno == errno.EINTR:
            continue
        # 其他 accept 错误——退避重试，防忙等
        time.sleep(0.1)
        continue

    _conn_count += 1
    _log("INFO", f"accept conn #{_conn_count} from {addr}")

    # --- connect + 转发阶段（per-connection，异常不影响主循环）---
    upstream = None
    try:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(30)
        upstream.connect(uds_path)
        _log("DEBUG", f"conn #{_conn_count} connected to UDS {uds_path}")
    except Exception as e:
        _log("WARN", f"conn #{_conn_count} UDS connect failed: errno={getattr(e,'errno',None)} {e!r}")
        try: conn.close()
        except OSError: pass
        if upstream:
            try: upstream.close()
            except OSError: pass
        continue  # 跳过这个连接，继续 accept 下一个

    try:
        t1 = threading.Thread(target=forward, args=(conn, upstream), daemon=True)
        t2 = threading.Thread(target=forward, args=(upstream, conn), daemon=True)
        t1.start()
        t2.start()
    except Exception as e:
        _log("ERROR", f"conn #{_conn_count} thread start failed: {e!r}")
        try: conn.close()
        except OSError: pass
        try: upstream.close()
        except OSError: pass
        continue

# ===== 退出时记录原因 =====
_log("WARN", f"exiting: signal={_sig_received}, conn_count={_conn_count}")
try:
    srv.close()
except OSError:
    pass
