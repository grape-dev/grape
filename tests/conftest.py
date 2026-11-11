"""Shared fixtures for all tests."""
from __future__ import annotations

import pytest
import os
import sys

# Ensure src/ is in path（唯一权威路径引导；pytest.ini pythonpath=src 兜底）
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
# 测试目录自身入 path：tests/ 被按 domain 分目录后，共享 helper（mock_db 等）
# 仍以裸模块名 import；pytest prepend 模式只加用例所在子目录。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 测试库隔离：所有本地测试使用独立测试库，绝不触碰生产库 data/sessions.db
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")
# 测试强制 SQLite 后端：即使 configs/db-backend.json 或 .env 配了 mysql，测试也绝不连 RDS
# 使用 os.environ[...] = ... 强制覆盖（不使用 setdefault，防止 .env 已设置 mysql 时失效）
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
# 清除可能被 .env 设置的 MySQL 连接参数，防止 mysql_compat 误连生产库
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
    os.environ.pop(_k, None)
# 测试隔离工作区（config.WORKSPACE_DIR 解析优先级 env > 家目录 > 代码目录）
os.environ["DEVOPS_WORKSPACE_DIR"] = os.path.join(ROOT, "data", "test_workspaces")

# tests/playwright/*.py 是手工 E2E 脚本，不是 pytest 用例。
# 这些脚本在 import 期就启动浏览器并访问 localhost:3000，pytest 收集会直接执行。
# 手工运行方式：python tests/playwright/test_r12_fullscreen_zindex.py
collect_ignore_glob = ["playwright/*.py"]


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_db():
    """确保测试库存在且已初始化（创建表）。

    会话开始重建干净测试库：删除历史残留的 data/test_sessions.db，
    保证每次 pytest 从确定的空库基线开始（消除跨运行残留脏行/回收 id 冲突，
    是随机顺序确定性的前提之一）。
    """
    import sqlite3 as _sqlite3
    _db_path = os.environ.get("DEVOPS_DB_PATH")
    if _db_path and os.path.exists(_db_path):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_db_path + suffix)
            except OSError:
                pass
    from src.api.db_setup import init_database
    init_database()
    yield


@pytest.fixture(scope="session", autouse=True)
def _ensure_help_page_generated():
    """测试前先把 help/ markdown 渲染为 src/api/frontend/legacy/help.html。

    help.html 已是生成物（.gitignore）；全新检查/克隆环境未启动服务时文件可能缺失，
    测试前调用生成器保证 test_help_directory.py 等对 help.html 的断言有确定输入。
    """
    from src.kernel.help_page_gen import generate as _gen_help_page

    _gen_help_page()
    yield


@pytest.fixture(scope="function", autouse=True)
def _reset_db_path():
    """每个测试前重置 DEVOPS_DB_PATH。

    防止其他测试模块 import 时改 env 导致后续测试拿到错误路径。
    注意：不重绑 session_manager.db 连接——73 个模块在 module level 导入了 db，
    重绑 session_manager.db 不会更新它们的引用，反而导致写读不一致。
    依赖 _ensure_test_db session fixture 在 session 开始时用正确 env 初始化表。
    """
    _test_db = os.path.join(ROOT, "data", "test_sessions.db")
    os.environ["DEVOPS_DB_PATH"] = _test_db
    yield

@pytest.fixture(autouse=True)
def _isolate_src_modules_and_env():
    """逐用例隔离 sys.modules 中的 src.* 与数据库环境变量。

    背景：部分用例直接改写 sys.modules["src"] / 注入 stub 模块（如 autopilot 通知、
    ACP 探测），或把 DEVOPS_DB_PATH 指向临时库；旧布局（tests 平铺）靠执行顺序侥幸通过。
    tests/ 按 domain 分目录后收集顺序变化，污染会外溢成后续用例的
    "No module named 'src.domains...'" / sqlite unable to open 采集错误。
    这里在每个用例前后快照并恢复，保证用例间互不影响。
    """
    import sys as _sys

    keys = ("DEVOPS_DB_PATH", "DEVOPS_DB_BACKEND")
    saved_mods = {k: v for k, v in list(_sys.modules.items())
                  if k == "src" or k.startswith("src.")}
    saved_env = {k: os.environ.get(k) for k in keys}
    yield
    for k in [k for k in list(_sys.modules) if k == "src" or k.startswith("src.")]:
        if k not in saved_mods:
            _sys.modules.pop(k, None)
    _sys.modules.update(saved_mods)
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_gsb_lb_state():
    """GSB 负载均衡的进程级缓存/轮询游标在用例之间必须重置（否则用例互相污染）。"""
    try:
        from src.kernel.gsb import client as _gsb_client
        from src.kernel.gsb.client import reset_instance_cache

        _gsb_client._RR_CURSOR.clear()
        reset_instance_cache()
    except Exception:  # noqa: BLE001 - 指标/注册表未初始化时不影响用例
        pass
    yield
