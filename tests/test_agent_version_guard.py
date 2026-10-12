"""RuntimeAgent 版本守卫测试（见 add-new-worker-guide.md 常见问题）。

代码使用 Python 3.10+ 联合类型语法（X | None），worker 若用 <3.10 会在 import 期崩溃。
agent.py 顶部有版本守卫，给清晰报错而非 traceback。
"""
import subprocess, sys, os


def test_agent_module_has_version_guard():
    """agent.py 顶部应含 Python >= 3.10 守卫，且在 import 之前。"""
    p = os.path.join(os.path.dirname(__file__), "..", "src", "runtime", "agent.py")
    src = open(p, encoding="utf-8").read()
    guard_idx = src.find("requires Python >= 3.10")
    first_import = src.find("import asyncio, json")
    assert guard_idx != -1, "agent.py 应含版本守卫"
    assert guard_idx < first_import, "版本守卫应在 import 之前（防 import 期崩溃）"


def test_agent_imports_on_current_python():
    """当前环境（>=3.10）应能正常导入 agent 模块（守卫不误伤）。"""
    import importlib
    m = importlib.import_module("src.runtime.agent")
    assert hasattr(m, "main")


def test_main_requires_token():
    """main() 无 RUNTIME_TOKEN 时清晰报错退出（守卫之后逻辑）。

    ⚠️ 必须 mock 单实例保护：main() 在 token 守卫之前会执行
    kill_existing_worker()（读真实 ~/.grape/worker.pid 并强杀进程树）
    和 pid_file() 写入。dev 机上测试仓库与生产 worker 同机部署时，
    不 mock 会杀掉真实 worker 并把 worker.pid 改写成 pytest 的 PID
    （2026-10-12 build-0005 失联事故根因）。凡调用 main() 的测试都必须过此检查。
    """
    import importlib
    from unittest.mock import patch
    import tempfile
    m = importlib.import_module("src.runtime.agent")
    old_env = os.environ.get("RUNTIME_TOKEN")
    os.environ.pop("RUNTIME_TOKEN", None)
    with tempfile.TemporaryDirectory() as tmp_dir:
        _fake_pid = os.path.join(tmp_dir, "worker.pid")
        try:
            with patch("src.runtime.os_adapter.kill_existing_worker", return_value=False), \
                 patch("src.runtime.os_adapter.pid_file", return_value=_fake_pid):
                with __import__("contextlib").redirect_stderr(__import__("io").StringIO()) as err:
                    m.main()
        except SystemExit as e:
            assert e.code == 1
        finally:
            if old_env is not None:
                os.environ["RUNTIME_TOKEN"] = old_env
