"""Issue #2923：branch guard PR 查询失败降级不得向 stderr 泄漏原始 ImportError。

背景：部署缺 ``src.vcs`` / ``src.domains.runtime.vcs`` 时，branch guard 的 PR
查询走 fail-open 放行，但旧实现把 ``No module named src.vcs`` 原样写进 stderr，
污染任务日志、易被误判为推送失败。修复后：明细只进 debug 日志，用户侧仅一条
可读中文提示，push 仍成功。
"""
import io
import logging
import sys

from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-2923"
_VCS_PREFIX = "src.domains.runtime.vcs"


def _ref_line(branch=BRANCH, local="local-sha", remote="old-sha"):
    return f"refs/heads/{branch} {local} refs/heads/{branch} {remote}\n"


class _BlockVcsFinder:
    """模拟部署缺 vcs 模块：任何 vcs 子模块 import 都抛 ImportError。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == _VCS_PREFIX or fullname.startswith(_VCS_PREFIX + "."):
            raise ModuleNotFoundError("No module named 'src.vcs'")


def _evict_vcs_modules(monkeypatch):
    for name in [n for n in list(sys.modules)
                 if n == _VCS_PREFIX or n.startswith(_VCS_PREFIX + ".")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_missing_vcs_module_push_succeeds_without_raw_importerror(
        monkeypatch, capsys, caplog):
    """缺 vcs 模块时 hook 退出码 0，stderr 只有一条中文提示，不含 ImportError 原文。"""
    _evict_vcs_modules(monkeypatch)
    finder = _BlockVcsFinder()
    sys.meta_path.insert(0, finder)
    try:
        monkeypatch.setattr(bg, "acquire_branch_lease",
                            lambda *a, **k: {"ok": True})
        monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
        monkeypatch.setattr(bg, "reconcile_pending", lambda *a, **k: "")
        monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
        caplog.set_level(logging.DEBUG)

        assert bg.hook_main([REPO_KEY]) == 0, "fail-open 必须放行 push"
    finally:
        sys.meta_path.remove(finder)

    err = capsys.readouterr().err
    hint_lines = [line for line in err.splitlines() if "[branch-guard]" in line]
    assert len(hint_lines) == 1, err
    assert "分支基线核验暂不可用" in hint_lines[0]
    assert "推送已放行" in hint_lines[0]
    assert "No module named" not in err
    assert "ImportError" not in err
    assert "src.vcs" not in err

    debug_messages = [record.getMessage() for record in caplog.records
                      if record.levelno == logging.DEBUG]
    assert any("PR query failed" in message for message in debug_messages)
    assert any("src.vcs" in message for message in debug_messages)


def test_emit_alert_never_leaks_stored_exception_text(capsys):
    """聚合输出只输出中文结论，收集到的原始 error 文案不得回流到 stderr。"""
    bg._emit_fail_open_alert(
        REPO_KEY,
        [{"stage": "pr_query", "error": "No module named 'src.vcs'"}])

    err = capsys.readouterr().err
    assert "No module named" not in err
    assert "src.vcs" not in err
    assert "分支基线核验暂不可用" in err
    assert "推送已放行" in err


def test_server_side_stage_failure_keeps_warning(caplog):
    """无钩子收集器（服务端 preflight）时维持原有 WARNING 语义，异常保持可见。"""
    caplog.set_level(logging.DEBUG)
    bg._guard_stage_failed(
        "pr_query", "branch guard PR query failed: No module named src.vcs")

    warning_messages = [record.getMessage() for record in caplog.records
                        if record.levelno >= logging.WARNING]
    assert any("No module named src.vcs" in message for message in warning_messages)


def test_fail_open_stderr_hides_raw_reason(capsys):
    """依赖缺失/运行异常的原始文案只进日志，stderr 只给可读中文结论。"""
    bg._fail_open(
        "branch guard dependency missing: src.lock (ModuleNotFoundError: "
        "No module named 'src.lock')",
        "分支守卫依赖缺失，推送已放行（fail-open）")

    err = capsys.readouterr().err
    assert "分支守卫依赖缺失" in err
    assert "推送已放行" in err
    assert "No module named" not in err
    assert "ModuleNotFoundError" not in err
    assert "src.lock" not in err


def test_missing_hook_deps_returns_names_only(monkeypatch):
    """依赖自检返回值只含模块名，异常文案不得回传给用户侧。"""
    real_import = bg.importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "src.kernel.lock":
            raise ModuleNotFoundError("No module named 'src.kernel.lock'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(bg.importlib, "import_module", fake_import)
    missing = bg._missing_hook_deps()
    assert missing == ["src.kernel.lock"]
    assert all("No module named" not in item for item in missing)


def test_hook_entry_code_hides_raw_exception(tmp_path):
    """钩子顶层兜底只输出中文结论，模块加载失败的原始异常不得进 stderr。"""
    import subprocess

    code = bg._hook_entry_code(
        str(tmp_path), str(tmp_path / "does_not_exist.py"))
    assert "%r" not in code
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=False, input="", capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, "守卫自身异常必须 fail-open"
    assert "守卫自身异常" in proc.stderr
    assert "fail-open" in proc.stderr
    assert "FileNotFoundError" not in proc.stderr
    assert "No such file" not in proc.stderr
    assert "Traceback" not in proc.stderr
