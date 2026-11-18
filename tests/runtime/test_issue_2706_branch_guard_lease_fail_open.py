"""Issue #2706：pre-push 守卫租约层 fail-open（缺 lock 模块不再阻断 push）。

背景（user feedback #7415，第 8 次同类复发）：#2292 已把租约三函数的 lock 导入迁到
canonical ``src.kernel.lock``，但导入仍无异常兜底——部署树缺 ``src/kernel/lock.py``
（或 lock 初始化异常）时，``acquire_branch_lease`` 直接抛 ImportError，把守卫自身的
包缺陷升级成「阻断全部 grape git push」，违反模块文档「守卫自身查询异常 → fail-open +
告警」契约；agent 只能 ``--no-verify`` 绕行，分支租约/漂移校验全部丢失。

本用例固化：
1. 三处租约函数的 lock 导入统一走 ``_lock_module()`` 收敛点（源码级断言，防三处漂移）；
2. lock 不可用时 fail-open：``acquire_branch_lease`` 返回 ``{"ok": True, "degraded": True}``
   按无租约放行，且打可定位 WARNING（模块名 + 修复指引）；``release_branch_lease`` /
   ``_lease_holder`` 不上抛；
3. builtins.__import__ 级模拟真实部署形态（`from src.kernel import lock` 抛 ImportError）
   仍 fail-open，而非只测自造桩点；
4. 正常环境租约语义不回归；真实违规（租约冲突）仍 fail-closed 阻断。
"""

import builtins
import io
import os

import pytest

from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-2706"


class _LogRecorder:
    """bg.log 桩：记录 warning 便于断言可定位告警（不依赖真实 config 日志链）。"""

    def __init__(self):
        self.warnings = []

    def warning(self, msg, *a, **k):
        self.warnings.append(msg % a if a else msg)

    def info(self, *a, **k):
        pass


@pytest.fixture
def rec_warn(monkeypatch):
    recorder = _LogRecorder()
    monkeypatch.setattr(bg, "log", recorder)
    return recorder


def _clear_guard_state():
    from src.kernel.session_manager import db

    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.execute("DELETE FROM branch_guard_state")
        db.commit()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_guard_state():
    _clear_guard_state()
    yield
    _clear_guard_state()


def _unavailable(monkeypatch):
    """把收敛点替换成「lock 不可用」形态（模拟缺 src/kernel/lock.py）。"""
    monkeypatch.setattr(bg, "_lock_module", lambda *a, **k: None)


# ===================== 1. 收敛点（AC1） =====================

def test_lease_lock_imports_go_through_single_convergence_point():
    """三处租约函数不得各自直接导入 lock：必须经 ``_lock_module()`` 单点收敛。

    历史同类复发（#1402…#2292）的根因之一是导入与兜底逻辑三处漂移；源码级断言
    防止后续改动把直接导入加回函数体。收敛点内部实现允许两种形态：
    函数内 import 语句或 ``importlib.import_module``（后者是 Issue #2706 合入
    收敛点的实现形态）——二者只允许存在其一，且都在 ``_lock_module`` 函数体内。
    """
    with open(os.path.abspath(bg.__file__), encoding="utf-8") as f:
        source = f.read()
    assert "def _lock_module(" in source
    stmt_count = source.count("from src.kernel import lock as _lock")
    importlib_count = source.count('importlib.import_module(LEASE_LOCK_MODULE)')
    assert max(stmt_count, importlib_count) == 1, (
        "lock 导入只允许存在于 _lock_module() 收敛点内")
    assert "from src import lock" not in source, "#2292：旧顶层导入绝迹"
    # 收敛点函数体内不落任何散落导入形态
    assert source.count("from src.kernel import lock") <= 1


# ===================== 2. fail-open 降级（AC2） =====================

def test_acquire_fail_open_when_lock_module_unavailable(monkeypatch, rec_warn):
    """lock 不可用：按无租约放行（ok=True + degraded），不抛 ImportError。"""
    _unavailable(monkeypatch)
    lease = bg.acquire_branch_lease(REPO_KEY, BRANCH, expected_remote_sha="sha-1")
    assert lease["ok"] is True, "守卫自身缺陷不得阻断 push（fail-open 契约）"
    assert lease.get("degraded") is True
    assert lease.get("reason") == "guard_lock_unavailable"
    assert lease.get("lock_key") == bg.lease_key(REPO_KEY, BRANCH)
    assert "fail-open" in lease.get("message", "")


def test_acquire_fail_open_warning_is_locatable(monkeypatch, rec_warn):
    """降级 WARNING 必须可定位：含缺失模块与可执行修复指引（防只留裸异常）。"""
    real_import = builtins.__import__
    real_import_module = bg.importlib.import_module

    def _fake_import(name, *a, **k):
        if name == "src.kernel":
            raise ImportError("No module named 'src.kernel.lock'")
        return real_import(name, *a, **k)

    def _fake_import_module(name, *a, **k):
        if name in ("src.kernel.lock", "src.kernel"):
            raise ImportError("No module named 'src.kernel.lock'")
        return real_import_module(name, *a, **k)

    # 同时拦截两种导入形态：收敛点无论用 import 语句还是 importlib.import_module，
    # 「真实部署树缺 lock」都必须触发 fail-open 降级 + 可定位告警。
    monkeypatch.setattr(builtins, "__import__", _fake_import)
    monkeypatch.setattr(bg.importlib, "import_module", _fake_import_module)
    lease = bg.acquire_branch_lease(REPO_KEY, BRANCH)
    assert lease["ok"] is True and lease.get("degraded") is True
    assert rec_warn.warnings, "降级必须打告警，不得静默"
    joined = "\n".join(rec_warn.warnings)
    assert "src.kernel.lock" in joined, "告警指名模块（可定位）"
    assert "fail-open" in joined
    assert "install-worker" in joined or "升级" in joined, "告警带可执行修复指引"


def test_release_fail_open_silent_when_lock_module_unavailable(monkeypatch):
    """lock 不可用：release 静默返回不上抛（finally 路径异常不得翻转判定，#2028 教训）。"""
    _unavailable(monkeypatch)
    assert bg.release_branch_lease(REPO_KEY, BRANCH, task_id="t_task") is None


def test_lease_holder_none_when_lock_module_unavailable(monkeypatch):
    """lock 不可用：_lease_holder 返回 None，不抛异常（acquire 内部路径）。"""
    _unavailable(monkeypatch)
    assert bg._lease_holder("t_task") is None


def test_branch_lease_context_degrades_without_raising(monkeypatch):
    """branch_lease 上下文管理器在降级态正常 yield/退出（服务端直调路径不炸）。"""
    _unavailable(monkeypatch)
    with bg.branch_lease(REPO_KEY, BRANCH, task_id="t_task") as lease:
        assert lease["ok"] is True and lease.get("degraded") is True


def test_acquire_fail_open_on_real_import_error(monkeypatch, rec_warn):
    """真实部署形态：lock 导入抛 ImportError 仍 fail-open。

    同时拦截 builtins.__import__（import 语句形态）与 ``importlib.import_module``
    （Issue #2706 收敛点的实现形态），模拟部署树缺 ``src/kernel/lock.py``：
    无论收敛点用哪种导入机制，真缺模块都必须降级放行而非上抛。
    """
    real_import = builtins.__import__
    real_import_module = bg.importlib.import_module

    def _fake_import(name, *a, **k):
        if name == "src.kernel":
            raise ImportError("cannot import name 'lock' from 'src.kernel'")
        return real_import(name, *a, **k)

    def _fake_import_module(name, *a, **k):
        if name in ("src.kernel.lock", "src.kernel"):
            raise ImportError("cannot import name 'lock' from 'src.kernel'")
        return real_import_module(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    monkeypatch.setattr(bg.importlib, "import_module", _fake_import_module)
    lease = bg.acquire_branch_lease(REPO_KEY, BRANCH)
    assert lease["ok"] is True and lease.get("degraded") is True
    joined = "\n".join(rec_warn.warnings)
    assert "src.kernel.lock" in joined or "cannot import name" in joined
    bg.release_branch_lease(REPO_KEY, BRANCH)


# ===================== 3. 正常路径不回归（AC3） =====================

def test_lease_functions_normal_path_not_regressed():
    """lock 可用：acquire/renew/release 语义与 #993/#2292 完全一致，无 degraded。"""
    lease = bg.acquire_branch_lease(REPO_KEY, BRANCH, task_id="t_task",
                                    expected_remote_sha="sha-1")
    assert lease["ok"] is True
    assert not lease.get("degraded"), "正常持租约不得标降级"
    assert lease.get("holder"), "holder 必须回填（争用提示定位依赖）"
    holder = bg._lease_holder("t_task")
    assert holder is not None and holder.startswith("t_task:")
    # 同任务同 holder 重入视为成功（#993 语义）
    again = bg.acquire_branch_lease(REPO_KEY, BRANCH, task_id="t_task")
    assert again["ok"] is True
    bg.release_branch_lease(REPO_KEY, BRANCH, task_id="t_task")
    # 释放后可重新获取
    assert bg.acquire_branch_lease(REPO_KEY, BRANCH)["ok"] is True
    bg.release_branch_lease(REPO_KEY, BRANCH)


# ===================== 4. 真实违规仍 fail-closed（AC4） =====================

def test_hook_still_blocks_on_lease_conflict(monkeypatch, capsys):
    """fail-open 只兜守卫自身异常：租约冲突等正常拦截仍 return 1（#2028 边界）。"""
    monkeypatch.setattr(
        bg, "acquire_branch_lease",
        lambda *a, **k: {"ok": False, "reason": "branch_locked",
                         "message": "分支被他人占用"})
    monkeypatch.setattr(bg.sys, "stdin",
                        io.StringIO(f"refs/heads/{BRANCH} local-sha refs/heads/{BRANCH} old-sha\n"))
    assert bg.hook_main([REPO_KEY]) == 1
    assert "分支被他人占用" in capsys.readouterr().err


# ===================== 5. 发布闭包核验（AC5） =====================

def test_worker_package_manifest_still_ships_lock_and_alias_entry():
    """MANIFEST 继续下发 canonical lock 目录与 ``src/__init__.py`` 别名入口。"""
    from src.kernel.worker_package import MANIFEST, ROOT
    assert "src/__init__.py" in MANIFEST
    assert "src/kernel/" in MANIFEST
    assert os.path.isfile(os.path.join(ROOT, "src", "kernel", "lock.py"))
