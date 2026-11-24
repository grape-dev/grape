"""Issue #4203：launcher 预 bind（scripts/start-worker.py）。

seam 级验证（不真启动 uvicorn）：
- listen_socket：bind + listen + SO_REUSEADDR（端口 0 免冲突）；
- main-guard：spawn 子进程重跑本文件（__name__ 不是 __main__）不触发 bind
  （EADDRINUSE 崩溃循环的实测教训，用真子进程断言，不再 `or True` 恒真）；
- 环境变量缺省对齐端口事实源（38083 / 0.0.0.0 / 2 workers）。

真启动 seam（SRE 部署复核 Critical，PR #2092）：上述 seam 级用例全程 mock uvicorn，
不覆盖 argv/sys.path 与真实 app 导入——「脚本路径启动时仓库根不在 sys.path →
ModuleNotFoundError」这条链只有真启动才能拦截。TestRealStartupSeam 以子进程按
生产单元一致的 argv+cwd+env（无 PYTHONPATH）拉起一次，断言 TCP 5s 内可连
（预 bind 先于 app 导入）+ /api/ping 200 + 进程存活，finally 强制回收进程组，
不留长驻服务（服务类命令禁前台长驻的约束不破）。
"""
import importlib.util as _ilu
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_SCRIPT = os.path.join(_ROOT, "scripts", "start-worker.py")

_spec = _ilu.spec_from_file_location("start_worker", _SCRIPT)
sw = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(sw)


class TestListenSocket:
    def test_bind_listen_reuse(self):
        s = sw.listen_socket("127.0.0.1", 0)
        try:
            assert s.getsockname()[0] == "127.0.0.1"
            # 已 listen：连接立刻可排队（不阻塞）
            c = socket.create_connection(s.getsockname()[:2], timeout=2)
            c.close()
        finally:
            s.close()

    def test_bind_conflict_raises(self):
        s = sw.listen_socket("127.0.0.1", 0)
        try:
            with pytest.raises(OSError):
                sw.listen_socket("127.0.0.1", s.getsockname()[1])
        finally:
            s.close()


class TestSpawnReentry:
    """multiprocessing spawn 会用 __mp_main__ 重跑本文件：顶层绝不能 bind。"""

    def test_spawn_reentry_does_not_bind(self, tmp_path):
        code = textwrap.dedent(f"""
            import importlib.util as u, socket, sys

            class _GuardSocket(socket.socket):
                def bind(self, *a, **k):
                    # 顶层 bind = 每个 worker 抢同一端口 → EADDRINUSE 崩溃循环
                    sys.exit(97)

            socket.socket = _GuardSocket
            spec = u.spec_from_file_location("__mp_main__", {_SCRIPT!r})
            mod = u.module_from_spec(spec)
            spec.loader.exec_module(mod)
            print(mod.__name__)
            print(mod._APP_IMPORT)
        """)
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert "__mp_main__" in proc.stdout
        assert sw._APP_IMPORT in proc.stdout

    def test_main_guard_present(self):
        """真 spawn 的入口只由 __main__ 分支驱动（AST 级核对，防守卫被删）。"""
        import ast
        tree = ast.parse(open(_SCRIPT, encoding="utf-8").read())
        guards = [n for n in tree.body if isinstance(n, ast.If)
                  and "__main__" in ast.dump(n.test)]
        assert guards, "缺少 __main__ 守卫：spawn 子进程会重复 bind"


class TestMultiprocessCompat:
    """多 worker 构造要同时适配 uvicorn ≥0.51 与 0.23–0.50（依赖只写了 >=0.23）。"""

    class _Server:
        def __init__(self, config):
            self.config = config
            self.run_called = False

        def run(self):
            self.run_called = True

    class _NewMP:
        """uvicorn ≥0.51：``Multiprocess(config, sockets)``。"""

        def __init__(self, config, sockets):
            self.config = config
            self.sockets = sockets
            self.target = None
            self.ran = False

        def run(self):
            self.ran = True

    class _OldMP:
        """uvicorn 0.23–0.50：``Multiprocess(config, target, sockets)``，target 必填。"""

        def __init__(self, config, target, sockets):
            self.config = config
            self.target = target
            self.sockets = sockets

        def run(self):
            pass

    def test_new_signature_gets_sockets(self):
        sock = socket.socket()
        try:
            mp = sw.build_multiprocess(object(), sock, multiprocess_cls=self._NewMP,
                                       server_cls=self._Server)
        finally:
            sock.close()
        assert isinstance(mp, self._NewMP)
        assert mp.sockets == [sock]
        assert mp.target is None

    def test_old_signature_gets_target(self):
        sock = socket.socket()
        try:
            mp = sw.build_multiprocess(object(), sock, multiprocess_cls=self._OldMP,
                                       server_cls=self._Server)
        finally:
            sock.close()
        assert isinstance(mp, self._OldMP)
        assert mp.sockets == [sock]
        assert callable(mp.target)

    def test_unnamed_signature_falls_back_on_type_error(self):
        """签名读不出来时（C 扩展/装饰器），构造 TypeError 应回退旧签名而非崩溃。"""
        sock = socket.socket()
        calls = []

        class _Opaque:
            def __init__(self, config, *args, **kwargs):
                calls.append(kwargs)
                if "target" not in kwargs:
                    raise TypeError("__init__() missing 1 required positional argument: 'target'")
                self.target, self.sockets = kwargs["target"], kwargs["sockets"]

        try:
            mp = sw.build_multiprocess(object(), sock, multiprocess_cls=_Opaque,
                                       server_cls=self._Server)
        finally:
            sock.close()
        assert mp.sockets == [sock] and callable(mp.target)
        assert len(calls) == 2, "应先按新签名构造、TypeError 后再按旧签名重试"

    def test_run_dispatches_workers_to_build_multiprocess(self, monkeypatch):
        """workers>1 走 build_multiprocess（而不是裸构造），单进程仍走 Server.run。"""
        seen = {}

        class _MP:
            def __init__(self, config, sock):
                seen["sock"] = sock

            def run(self):
                seen["ran"] = True

        called = {}
        monkeypatch.setattr(sw, "build_multiprocess",
                            lambda config, sock: (called.setdefault("argv", (config, sock)),
                                                  _MP(config, sock))[1])
        s = sw.listen_socket("127.0.0.1", 0)
        try:
            sw._run(s, 2)
        finally:
            s.close()
        assert called["argv"][1] is s
        assert seen.get("ran") is True


class TestDefaults:
    def test_app_import_string_matches_unit_app(self):
        assert sw._APP_IMPORT == "src.api.grape_worker_server:app"

    def test_default_port_matches_port_fact_source(self):
        # #3764 端口事实源：mgmt/event/worker/insight = 38081/38082/38083/38084
        assert sw._DEFAULT_PORT == 38083

    def test_defaults_host_and_workers(self):
        assert sw._DEFAULT_HOST == "0.0.0.0"
        assert sw._DEFAULT_WORKERS == 2

    def test_env_overrides_are_read(self, monkeypatch):
        """GRAPE_WORKER_PORT 优先于 GRAPE_PORT（与单元的 Environment= 对齐）。"""
        for var in ("GRAPE_WORKER_PORT", "GRAPE_PORT", "GRAPE_HOST", "GRAPE_UVICORN_WORKERS"):
            monkeypatch.delenv(var, raising=False)
        assert sw.resolve_params() == ("0.0.0.0", 38083, 2)
        monkeypatch.setenv("GRAPE_PORT", "39001")
        assert sw.resolve_params()[1] == 39001
        monkeypatch.setenv("GRAPE_WORKER_PORT", "39002")
        assert sw.resolve_params()[1] == 39002          # 专用变量优先
        monkeypatch.setenv("GRAPE_HOST", "127.0.0.1")
        monkeypatch.setenv("GRAPE_UVICORN_WORKERS", "1")   # grape@ 路径的单进程语义
        assert sw.resolve_params() == ("127.0.0.1", 39002, 1)


class TestRealStartupSeam:
    """SRE 部署复核 Critical（PR #2092）：生产 argv 真启动——真实 app 导入必须成功。

    生产两条 ExecStart 路径都以「脚本路径」启动本 launcher（grape@worker →
    grape-systemd-run.sh ``exec "$PY" .../scripts/start-worker.py``、静态单元直跑），
    且不带 PYTHONPATH：Python 脚本模式 sys.path[0]=scripts/，仓库根不在 sys.path
    （此前 ``python -m uvicorn`` 能导入 app 只因 ``-m`` 会把 cwd 加进 sys.path）。
    缺 sys.path 自举时 uvicorn 在 config.load 阶段导入 ``src.api.grape_worker_server``
    抛 ``ModuleNotFoundError: No module named 'src.api'``，exit 1 → systemd 反复
    status=1/FAILURE，端口无监听（2026-11-24 生产 4 分钟不可用；seam 级用例全程
    mock uvicorn，对这条真实导入路径零覆盖）。本用例不 mock：子进程真启动一次。

    时限依据（本机实测）：TCP 可连 ~0.1s（bind 在 app 导入之前），/api/ping 首响应
    ~8.3s（app import + lifespan 含 DB 初始化）——首响应预算取 30s（≈4x 余量），
    5s 预算只给「端口先于 lifespan 可连」这条预 bind seam；收紧首响应到 5s 会在
    CI 慢机上假红，反而让门禁不可信。
    """

    _TCP_DEADLINE_S = 5.0    # 预 bind seam：端口必须先于 app 导入可连
    _PING_DEADLINE_S = 30.0   # /api/ping 200：含 app import + lifespan（实测 ~8.3s）

    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _spawn_unit_consistent(self, port):
        """与生产 ExecStart 一致：脚本路径 argv、cwd=仓库根、无 PYTHONPATH。

        DB/工作区隔离变量（DEVOPS_DB_BACKEND=sqlite 等）由 tests/conftest.py 在
        收集期注入 os.environ，子进程整体继承——与 pytest 进程内用例同一套隔离语义。
        """
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update({
            "GRAPE_WORKER_PORT": str(port),
            "GRAPE_HOST": "127.0.0.1",        # 测试监听回环即可，不对外暴露
            "GRAPE_UVICORN_WORKERS": "1",     # 生产单进程语义（grape@worker 传 1）
            "GRAPE_PROCESS_ROLE": "worker",   # 对齐静态单元 Environment=
        })
        return subprocess.Popen(
            [sys.executable, _SCRIPT],
            cwd=_ROOT, env=env, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    @staticmethod
    def _terminate_group(proc):
        """收进程组（start_new_session 使 launcher 独立成组），不留长驻服务。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            return proc.communicate(timeout=15)[0]
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return proc.communicate()[0]

    @staticmethod
    def _tail(text, lines=20):
        return "\n".join(text.splitlines()[-lines:])

    def test_unit_consistent_startup_serves_ping(self):
        port = self._free_port()
        proc = self._spawn_unit_consistent(port)
        tcp_ok = ping_ok = alive_at_ping = False
        try:
            tcp_deadline = time.monotonic() + self._TCP_DEADLINE_S
            while time.monotonic() < tcp_deadline:
                try:
                    c = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                    c.close()
                    tcp_ok = True
                    break
                except OSError:
                    time.sleep(0.1)
            ping_deadline = time.monotonic() + self._PING_DEADLINE_S
            while time.monotonic() < ping_deadline:
                if proc.poll() is not None:
                    break  # 进程先死：永远等不到 200（修复前的回归形态）
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
                        if r.status == 200:
                            ping_ok = True
                            alive_at_ping = proc.poll() is None
                            break
                except Exception:
                    time.sleep(0.2)
        finally:
            out = self._terminate_group(proc)

        assert tcp_ok, (
            f"{self._TCP_DEADLINE_S}s 内端口 {port} 不可连：预 bind seam 失效"
            f"（「先监听再 lifespan」的窗口收敛前提不成立）\n{self._tail(out)}")
        assert ping_ok and alive_at_ping, (
            "单元一致启动（脚本路径 argv / 无 PYTHONPATH / cwd=仓库根）未达成 "
            "/api/ping 200——回归形态：ModuleNotFoundError → exit 1 → systemd "
            f"反复重启，端口无监听\n{self._tail(out)}")
        assert "[start-worker] pre-bound" in out, (
            f"日志缺 pre-bound（bind 与 lifespan 的顺序断言）\n{self._tail(out)}")
        assert "ModuleNotFoundError" not in out and "No module named 'src" not in out, (
            f"app 导入失败（仓库根未进 sys.path）\n{self._tail(out)}")