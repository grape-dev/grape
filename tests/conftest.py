"""Shared fixtures for all tests."""
from __future__ import annotations

import pytest
import os
import sys

# Ensure src/ is in path（唯一权威路径引导；pytest.ini pythonpath=src 兜底）
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

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

# 测试库重建必须在**任何模块 import src.session_manager 之前**完成。
# 反例（踩过）：把删除放在 session 级 fixture 里 → 收集阶段 test 模块已 module-level
# `from src.session_manager import db` 打开并持有旧 inode，随后删文件 → 之后所有
# init_database 建表都写进「已 unlink 的旧文件」，磁盘路径上是一个零表的空库。
# 结果：engine_wiring（自己 _connect(default_db_path()) 新开连接）看不到任何表，
# 表现为 `no such table: chat_agent_session / grape_t_channel_installation`。
# conftest.py 先于测试模块被 import，故此处删除是安全且唯一正确的时机。
_TEST_DB_PATH = os.environ["DEVOPS_DB_PATH"]
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(_TEST_DB_PATH + _suffix)
    except OSError:
        pass

# tests/playwright/*.py 是手工 E2E 脚本，不是 pytest 用例。
# 这些脚本在 import 期就启动浏览器并访问 localhost:3000，pytest 收集会直接执行。
# 手工运行方式：python tests/playwright/test_r12_fullscreen_zindex.py
collect_ignore_glob = ["playwright/*.py"]


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_db():
    """确保测试库存在且已初始化（创建表）。

    库文件的重建（删除历史残留）已上移到本文件模块级、import 期执行——必须早于任何
    测试模块 module-level 打开 src.session_manager.db，否则建表会落进被 unlink 的旧
    inode，磁盘路径上留下零表空库（详见模块顶部注释）。此处只负责建表。
    """
    from src.api.db_setup import init_database
    init_database()
    yield


@pytest.fixture(scope="session", autouse=True)
def _ensure_help_page_generated():
    """测试前先把 help/ markdown 渲染为 src/api/static/help.html。

    help.html 已是生成物（.gitignore）；全新检查/克隆环境未启动服务时文件可能缺失，
    测试前调用生成器保证 test_help_directory.py 等对 help.html 的断言有确定输入。
    """
    from src.help_page_gen import generate as _gen_help_page

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
