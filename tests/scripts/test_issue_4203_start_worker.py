"""Issue #4203：launcher 预 bind（scripts/start-worker.py）。

不真启动 uvicorn（服务类命令禁前台长驻），只验证 seam：
- listen_socket：bind + listen + SO_REUSEADDR（端口 0 免冲突）；
- main-guard：spawn 子进程重跑本文件（__name__ 不是 __main__）不触发 bind
  （EADDRINUSE 崩溃循环的实测教训，用真子进程断言，不再 `or True` 恒真）；
- 环境变量缺省对齐端口事实源（38083 / 0.0.0.0 / 2 workers）。
"""
import importlib.util as _ilu
import os
import socket
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "start-worker.py")

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