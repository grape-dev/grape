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
# S112（obs-log-writer SIGSEGV 根治，S67 同族）：关掉主控日志落库的后台写线程。
# 根因链（faulthandler 两次实锤，基线 43%/分支 35% 同点崩）：多个测试以
# `with TestClient(app)` 触发 lifespan → install_master_logging() 启动
# obs-log-writer daemon 线程（每秒批量 ingest 写 session_manager.db）→ 后续
# 测试的换库 fixture close/重绑连接 → 在飞 DB 语句与 close 并发 → sqlite C 层
# UAF → SIGSEGV。测试直接同步调 lg.ingest() 的用例（test_obs_logs 全部）不依赖
# 该线程——OBS_LOG_DB=0 后 install_master_logging no-op，线程不启动，断言零影响。
os.environ["OBS_LOG_DB"] = "0"

# S106（孤儿 inode 根因修复）：删除残留测试库必须发生在"任何模块建立 db 连接之前"。
# 旧实现把删除放在 session fixture（_ensure_test_db）里——pytest 先收集/导入测试
# 模块（73 个模块 module-level `from src.kernel.session_manager import db` 已绑连接），
# 再删文件 → 已绑定连接写进被删文件的孤儿 inode；凡开新连接的代码（fresh
# sqlite3.connect / 新引擎）看到的是重建后的空库 → "no such table: skill" 型
# E 残留。conftest 导入期先于所有测试模块导入，在此删除即无孤儿窗口。
#
# D100 混跑序污染修复（2026-11-08 audit-hook 实锤）：pytest.main() 内嵌套启动
# （uc40_6 缝迁移用例）会创建新 Session 并**重新 exec 本模块**（assertion rewrite
# 层独立 loader，不受 sys.modules 缓存影响）→ 本删除段在内层再执行一次 →
# 删掉外层正在使用的测试库文件 → 外层后续新开连接的用例（n4 usage_log flush
# 的 fresh sqlite3.connect）拿到空库 → "no such table" 三连挂。守卫：嵌套
# 执行（GRAPE_PYTEST_NESTED=1）跳过删除——内层建库交给外层已初始化的库文件。
if not os.environ.get("GRAPE_PYTEST_NESTED"):
    for _suffix in ("", "-wal", "-shm"):
        try:
            os.remove(os.environ["DEVOPS_DB_PATH"] + _suffix)
        except OSError:
            pass

# 测试库重建必须在**任何模块 import src.kernel.session_manager 之前**完成。
# 反例（踩过）：把删除放在 session 级 fixture 里 → 收集阶段 test 模块已 module-level
# `from src.kernel.session_manager import db` 打开并持有旧 inode，随后删文件 → 之后所有
# init_database 建表都写进「已 unlink 的旧文件」，磁盘路径上是一个零表的空库。
# 结果：engine_wiring（自己 _connect(default_db_path()) 新开连接）看不到任何表，
# 表现为 `no such table: chat_agent_session / grape_t_channel_installation`。
# conftest.py 先于测试模块被 import，故此处删除是安全且唯一正确的时机。
# （QA 复审 C-2：此处曾有一段**无守卫**的重复删库——嵌套 pytest
# （GRAPE_PYTEST_NESTED=1，见 test_uc40）重新 exec conftest 时会把外层
# 正在使用的库文件 unlink，D100 混跑序污染复发；上方带守卫版本已覆盖
# 外层场景，重复段删除。）

# tests/api/playwright/*.py 是手工 E2E 脚本，不是 pytest 用例。
# 这些脚本在 import 期就启动浏览器并访问 localhost:3000，pytest 收集会直接执行。
# 手工运行方式：python tests/api/playwright/test_r12_fullscreen_zindex.py
collect_ignore_glob = ["api/playwright/*.py"]


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_db():
    """确保测试库存在且已初始化（创建表）。

    库文件的重建（删除历史残留）已上移到本文件模块级、import 期执行——必须早于任何
    测试模块 module-level 打开 src.kernel.session_manager.db，否则建表会落进被 unlink 的旧
    inode，磁盘路径上留下零表空库（详见模块顶部注释）。此处只负责建表。
    """
    # S106：文件删除已上移到 conftest 导入期（见上），此处只做建表初始化
    from src.api.db_setup import init_database
    init_database()
    yield


@pytest.fixture(scope="session", autouse=True)
def _ensure_help_page_generated():
    """测试前先把 help/ markdown 渲染为 src/api/static/help.html。

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
            _gone = _sys.modules.pop(k, None)
            # Issue #1739 复跑：弹出用例内新建的模块后，父包上的同名属性必须
            # 一并清掉。否则 `from src.api import git_repo_api` 仍命中指向已弹出
            # 模块的旧属性（不在 sys.modules 里），随后 `from src.api.git_repo_api
            # import x` 又新建一个模块对象——patch.object 打在旧对象、被测函数
            # 来自新对象，monkeypatch 静默失效，用例会真的打到上游 API。
            _parent, _, _child = k.rpartition(".")
            _pkg = _sys.modules.get(_parent) if _parent else None
            if _pkg is not None and getattr(_pkg, _child, None) is _gone:
                try:
                    delattr(_pkg, _child)
                except Exception:  # noqa: BLE001 - 只读包/属性别名时忽略
                    pass
    # 根因补偿（#1811/#1745 实测）：上面逐键清 attr 只覆盖「本用例新建且父包仍是同一
    # 对象」的情形；若父包自身也被重建，残留属性仍会指向不在 sys.modules 的模块对象，
    # 导致下一个用例的字符串式 monkeypatch 打在陈旧副本上（补丁对被重导入的函数无效）。
    # 收尾时对全部 src.* 包做一次同口径清扫：属性指向的模块名不在 sys.modules 或
    # 已非同一对象 → 清属性。只清属性，不动 sys.modules 与断言口径。
    import types as _types
    for _pname, _pkg in list(_sys.modules.items()):
        if not (_pname == "src" or _pname.startswith("src.")):
            continue
        _ns = getattr(_pkg, "__dict__", None)
        if not isinstance(_ns, dict):
            continue
        for _attr, _val in list(_ns.items()):
            if _attr.startswith("__") or not isinstance(_val, _types.ModuleType):
                continue
            _modname = getattr(_val, "__name__", "") or ""
            if _modname and _modname != _pname and _sys.modules.get(_modname) is not _val:
                try:
                    delattr(_pkg, _attr)
                except Exception:  # noqa: BLE001 - 只读包/属性别名时忽略
                    pass
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
