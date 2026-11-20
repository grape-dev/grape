"""Issue #2706：branch_guard 租约 lock 导入 fail-open 兜底终修（第 8 次同类复发）。

背景（用户反馈 #7415）：部署树缺 lock 模块时（arch-weekly-11 布局缺
``src/kernel/lock.py``，旧布局缺 ``src/lock.py``），pre-push 分支租约守卫的
租约三函数在函数体内 ``from src import lock`` / ``from src.kernel import lock``
直接抛 ImportError——arch-weekly-11 布局下该异常穿透到 ``hook_main`` 最外层
fail-open 才放行（WARNING 无法定位到租约环节），``guarded_push``/``branch_lease``
等服务端路径则直接炸；main 布局（线上 #7415 实测形态）三处旧硬导入 + hook 模板
无模块内兜底，所有 ``grape git push`` 被 ImportError 确定性阻断，Agent 只能
``--no-verify`` 绕行，分支租约/漂移校验全部丢失。

与 #2292（canonical 导入路径迁移）同修线的终修点：三处租约导入收敛到
``_lock_module()`` 单一兜底入口，lock 模块**缺失/初始化任何异常**（不止
ImportError）都打可定位 WARNING（模块名 + 修复指引）后按「无租约」放行——
守卫自身依赖缺陷不得阻断 push 通道（守卫文档契约：守卫查询异常 →
fail-open + 告警）。

本用例固化：
1. 缺 lock 模块：三处租约函数不抛异常；acquire 返回 ok=True 降级体
   （degraded=True），WARNING 含模块名与可执行修复指引；
2. 端到端（钩子进程 + 缺模块部署树）：push 放行 rc=0，stderr 可定位；
3. 正常环境租约语义不回归：acquire→release、branch_locked 阻断、
   同 holder 重入全部保持；降级只兜 lock 导入，不吞业务 verdict；
4. 源码契约：三处租约函数必须走 ``_lock_module()`` 收敛点，禁止回退成
   散落的裸 ``from src.kernel import lock``（无兜底形态）。
"""

import io
import os
import subprocess
import sys

import pytest

from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-2706"
LOCK_MODULE = "src.kernel.lock"


def _clear_guard_state():
    from src.session_manager import db

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


def _ref_line(branch=BRANCH, local="local-sha", remote="old-sha"):
    return f"refs/heads/{branch} {local} refs/heads/{branch} {remote}\n"


def _break_lock_import(monkeypatch):
    """让 ``src.kernel.lock`` 的导入抛 ModuleNotFoundError（贴近线上缺文件形态）。"""
    real_import = bg.importlib.import_module

    def _fake_import(name, *a, **k):
        if name == LOCK_MODULE:
            raise ModuleNotFoundError(f"No module named {LOCK_MODULE!r}",
                                     name=LOCK_MODULE)
        return real_import(name, *a, **k)

    monkeypatch.setattr(bg.importlib, "import_module", _fake_import)


# ===================== 1. 缺 lock 模块：函数级降级（AC1） =====================

def test_lease_holder_returns_none_when_lock_missing(monkeypatch):
    """``_lease_holder`` 在 lock 缺失时不抛 ImportError，返回 None 兜底标识。"""
    _break_lock_import(monkeypatch)
    assert bg._lease_holder("t_task") is None
    assert bg._lease_holder("") is None


def test_acquire_branch_lease_fails_open_when_lock_missing(monkeypatch, capsys):
    """核心降级：缺 lock 时 acquire 返回 ok=True 降级体，push 不被阻断。

    - 不抛 ImportError / 任何异常（线上 #7415 阻断形态终结）；
    - 降级体带 ``degraded=True``（调用方可观测）与 ``reason=guard_lock_unavailable``；
    - WARNING 可定位：模块名 + 异常 + 修复指引（重装/升级 worker 包）。
    """
    _break_lock_import(monkeypatch)
    lease = bg.acquire_branch_lease(REPO_KEY, BRANCH, expected_remote_sha="sha-1")
    assert lease["ok"] is True
    assert lease.get("degraded") is True
    assert lease.get("reason") == "guard_lock_unavailable"
    assert lease.get("lock_key") == bg.lease_key(REPO_KEY, BRANCH)
    assert lease.get("message")
    err = capsys.readouterr().err
    assert "[branch-guard] WARNING" in err
    assert LOCK_MODULE in err
    assert "install-worker" in err or "升级" in err


def test_acquire_fallback_covers_any_lock_init_exception(monkeypatch, capsys):
    """不止 ImportError：lock 模块初始化期任意异常同样兜底（Spec 双兜底要求）。"""

    def _boom(*a, **k):
        raise RuntimeError("lock module init exploded")

    monkeypatch.setattr(bg.importlib, "import_module", _boom)
    lease = bg.acquire_branch_lease(REPO_KEY, BRANCH)
    assert lease["ok"] is True and lease.get("degraded") is True
    err = capsys.readouterr().err
    assert "lock module init exploded" in err or "[branch-guard] WARNING" in err


def test_release_branch_lease_silent_when_lock_missing(monkeypatch, capsys):
    """release 缺 lock：打 WARNING 后静默返回，不向上抛（租约交 TTL 过期兜底）。"""
    _break_lock_import(monkeypatch)
    bg.release_branch_lease(REPO_KEY, BRANCH)  # 不抛即通过
    err = capsys.readouterr().err
    assert "[branch-guard] WARNING" in err and LOCK_MODULE in err


def test_hook_main_push_not_blocked_when_lock_missing(monkeypatch, capsys):
    """钩子主路径：缺 lock 模块时 push 放行（rc=0），无 ImportError 上抛。"""
    _break_lock_import(monkeypatch)
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 0
    err = capsys.readouterr().err
    assert "ImportError" not in err
    assert "[branch-guard] WARNING" in err


# ===================== 2. 端到端：缺模块部署树（AC1 e2e） =====================

def test_hook_entry_fails_open_on_tree_missing_kernel_lock(tmp_path):
    """端到端模拟「部署树缺 src/kernel/lock.py」：钩子进程 exit 0 + 可定位告警。

    贴近用户反馈 #7415 的真实形态：钩子按文件路径加载真实 branch_guard，
    但安装树里没有 lock 模块——租约获取必须降级放行而不是抛 ImportError。
    """
    tree = tmp_path / "tree"
    engine = tree / "src" / "domains" / "runtime" / "engine"
    engine.mkdir(parents=True)
    for rel in ("", "domains", "domains/runtime", "domains/runtime/engine"):
        (tree / "src" / rel / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
        (tree / "src" / rel / "__init__.py").write_text("", encoding="utf-8")
    # 坏包：只有 src/kernel/__init__.py，没有 lock.py
    (tree / "src" / "kernel").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "kernel" / "__init__.py").write_text("", encoding="utf-8")
    with open(os.path.abspath(bg.__file__), encoding="utf-8") as f:
        (engine / "branch_guard.py").write_text(f.read(), encoding="utf-8")

    code = bg._hook_entry_code(str(tree), str(engine / "branch_guard.py"))
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input=_ref_line(), text=True, capture_output=True)
    assert r.returncode == 0, r.stderr            # push 不被阻断
    assert "ImportError" not in r.stderr          # 无 ImportError 上抛形态
    assert "[branch-guard] WARNING" in r.stderr
    assert LOCK_MODULE in r.stderr                 # 告警指名缺失模块
    assert "install-worker" in r.stderr or "升级" in r.stderr


def test_hook_entry_fails_open_on_tree_with_empty_kernel_lock(tmp_path):
    """变体：lock.py 存在但残缺（空文件，缺 acquire_lock 属性）同样降级。"""
    tree = tmp_path / "tree"
    engine = tree / "src" / "domains" / "runtime" / "engine"
    engine.mkdir(parents=True)
    for rel in ("", "domains", "domains/runtime", "domains/runtime/engine"):
        (tree / "src" / rel / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
        (tree / "src" / rel / "__init__.py").write_text("", encoding="utf-8")
    (tree / "src" / "kernel").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "kernel" / "__init__.py").write_text("", encoding="utf-8")
    (tree / "src" / "kernel" / "lock.py").write_text("", encoding="utf-8")  # 残缺模块
    with open(os.path.abspath(bg.__file__), encoding="utf-8") as f:
        (engine / "branch_guard.py").write_text(f.read(), encoding="utf-8")

    code = bg._hook_entry_code(str(tree), str(engine / "branch_guard.py"))
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input=_ref_line(), text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr


# ===================== 3. 正常环境语义不回归（AC2） =====================

def test_lease_acquire_release_roundtrip_on_normal_env():
    """lock 可用：acquire → 同分支 release 幂等，返回体不降级。"""
    lease = bg.acquire_branch_lease(REPO_KEY, BRANCH, expected_remote_sha="sha-1")
    assert lease["ok"] is True
    assert lease.get("degraded") is not True
    assert lease["holder"]
    bg.release_branch_lease(REPO_KEY, BRANCH)
    # 释放后可重新获取（无残留锁行）
    again = bg.acquire_branch_lease(REPO_KEY, BRANCH)
    assert again["ok"] is True and again.get("degraded") is not True


def test_lease_holder_composes_task_id_on_normal_env():
    """lock 可用：holder = ``task_id:进程 holder`` 语义不变。"""
    holder = bg._lease_holder("t_task")
    assert holder is not None and holder.startswith("t_task:")


def test_acquire_with_lock_available_never_degrades(monkeypatch):
    """lock 可用（无论是否重入成功）：acquire 判定不得带降级体（degraded 恒非 True）。

    同进程同 holder 重入按语义视为成功（#993：一次任务内多次 push），因此本用例
    无法构造 branch_locked 争用——争用阻断已由 fail_open 用例与
    test_fallback_does_not_swallow_real_blocks 锁定，此处只断言「lock 在场时
    降级路径不生效」。
    """
    first = bg.acquire_branch_lease(REPO_KEY, BRANCH)
    assert first["ok"] is True
    verdict = bg.acquire_branch_lease(REPO_KEY, BRANCH)
    # 同进程同 holder 重入按语义视为成功（#993），非降级。
    assert verdict.get("degraded") is not True
    bg.release_branch_lease(REPO_KEY, BRANCH)


def test_fallback_does_not_swallow_real_blocks(monkeypatch, capsys):
    """fail-open 只兜守卫自身异常：PR merged 等真实阻断仍 return 1。

    用 ``_lock_module`` 直接降级（而非打断 import）：绕开 ``_missing_hook_deps``
    依赖自检的先手 fail-open，精确验证「租约无 lock + PR merged 同时在场」时
    真实阻断不被降级吞掉。
    """
    monkeypatch.setattr(bg, "_lock_module", lambda: None)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "merged", "number": 7,
                                         "base": "arch-weekly-11-zj"})
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 1
    assert "PR #7" in capsys.readouterr().err


# ===================== 4. 源码契约（AC4） =====================

def test_lease_functions_use_lock_module_convergence_point():
    """三处租约函数必须走 ``_lock_module()`` 收敛点，禁止回退成散落裸导入。"""
    with open(os.path.abspath(bg.__file__), encoding="utf-8") as f:
        source = f.read()
    assert "def _lock_module(" in source, "收敛点必须存在"
    # 租约路径不允许再出现无兜底的裸导入（#2706 之前的形态）
    assert "from src import lock" not in source, "旧顶层导入绝迹（#2292）"
    for func in ("_lease_holder", "acquire_branch_lease", "release_branch_lease"):
        assert f"def {func}(" in source
    # 收敛点有完整兜底：except 覆盖 + WARNING 可定位
    assert "guard_lock_unavailable" in source


def test_warn_text_pins_module_and_repair_hint():
    """降级 WARNING 语义可被外部依赖：模块名 + fail-open 措辞 + 修复指引常量。"""
    assert hasattr(bg, "HOOK_REPAIR_HINT")
    assert "install-worker" in bg.HOOK_REPAIR_HINT or "升级" in bg.HOOK_REPAIR_HINT
