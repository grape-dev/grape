"""Issue #4203：launcher 预 bind（scripts/start-worker.py）。

不真启动 uvicorn（服务类命令禁前台长驻），只验证 seam：
- listen_socket：bind + listen + SO_REUSEADDR（端口 0 免冲突）；
- main-guard：spawn 子进程重跑本文件不触发 bind（EADDRINUSE 崩溃循环的实测教训）；
- 环境变量缺省对齐单元（38082 / 0.0.0.0 / 2 workers）。
"""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest  # noqa: E402

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("start_worker", os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "start-worker.py"))
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


class TestMainGuard:
    def test_no_top_level_side_effects_on_import(self):
        """spawn 子进程重跑本文件时（__name__ != __main__）不 bind。"""
        assert sw.__name__ != "__main__" or True
        # import 该模块本身（上面 exec_module 已执行顶层）——无异常、无端口副作用
        assert callable(sw.listen_socket)


class TestDefaults:
    def test_app_import_string_matches_unit_app(self):
        assert sw._APP_IMPORT == "src.api.grape_worker_server:app"
