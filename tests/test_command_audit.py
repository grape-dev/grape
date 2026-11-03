"""Issue #757 消费方接线：#757 写目标审计（默认关闭开关）测试。

覆盖：
- 开关默认关闭 → 零解析、零日志、返回 enabled=False
- 开启后：heredoc / python -c 内容不误判，重定向目标正常提取
- 受保护路径命中（.git / .env / 凭据 / 系统目录）
- 超长命令跳过（256KB 上限）
- tool_use 桥接与异常安全（非 dict、无 command、解析异常）
"""
import pytest

# M0 兼容层存在环形导入：src.agent/__init__ → base → src.runtime.__init__ → registry →
# src.agent.executor → base（未初始化）。先导入 src.runtime 定序即可（应用侧同样先加载
# src.runtime，此处与运行顺序保持一致）。
import src.runtime  # noqa: F401

from src.agent import command_audit as ca

HARD = "<<<<<<< HEAD"


def _on(monkeypatch):
    monkeypatch.setenv("GRAPE_COMMAND_PATH_AUDIT", "1")


def _off(monkeypatch):
    monkeypatch.delenv("GRAPE_COMMAND_PATH_AUDIT", raising=False)


# ── 开关 ─────────────────────────────────────────────────────────────

def test_default_disabled(monkeypatch):
    _off(monkeypatch)
    assert ca.audit_enabled() is False
    info = ca.audit_command("cat a > .env")
    assert info == {"targets": [], "protected": [], "skipped": False, "enabled": False}


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_enabled_values(monkeypatch, val):
    monkeypatch.setenv("GRAPE_COMMAND_PATH_AUDIT", val)
    assert ca.audit_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "", "no", "off", "2"])
def test_disabled_values(monkeypatch, val):
    monkeypatch.setenv("GRAPE_COMMAND_PATH_AUDIT", val)
    assert ca.audit_enabled() is False


# ── 目标提取（复用 #757 语义） ────────────────────────────────────────

def test_simple_redirect_no_protected(monkeypatch):
    _on(monkeypatch)
    info = ca.audit_command("cd /home/mys/grape && pytest -q > /tmp/test.log 2>&1")
    assert info["targets"] == ["/tmp/test.log"]
    assert info["protected"] == [] and info["skipped"] is False


def test_heredoc_body_paths_not_targets(monkeypatch):
    _on(monkeypatch)
    cmd = ("cat > /tmp/report.md <<'EOF'\n"
           "skills/issue/references/issue-runbook.md\n"
           "cat .env > leaked.txt\n"
           "EOF\n")
    info = ca.audit_command(cmd)
    assert info["targets"] == ["/tmp/report.md"]
    assert info["protected"] == []  # 正文里的 .env 写入不参与


def test_python_c_literal_paths_not_targets(monkeypatch):
    _on(monkeypatch)
    info = ca.audit_command("python3 -c \"open('.env','w').write('x')\" > /tmp/real.log")
    assert info["targets"] == ["/tmp/real.log"]
    assert info["protected"] == []


# ── 受保护路径 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,path,kind", [
    ("echo x > .git/config", ".git/config", "vcs-internal"),
    ("echo SECRET=. > .env", ".env", "env-secret"),
    ("echo k > .env.production", ".env.production", "env-secret"),
    ("echo k > ~/.ssh/id_rsa", "~/.ssh/id_rsa", "credential"),
    ("printf x > /etc/hosts", "/etc/hosts", "system"),
    ("echo x > /root/.bashrc", "/root/.bashrc", "system"),
    ("cat k >> id_rsa", "id_rsa", "credential"),
])
def test_protected_targets(monkeypatch, cmd, path, kind):
    _on(monkeypatch)
    info = ca.audit_command(cmd)
    assert path in info["targets"]
    assert (path, kind) in info["protected"]


def test_multi_target_flags_only_protected(monkeypatch):
    _on(monkeypatch)
    info = ca.audit_command("echo a > /tmp/ok.txt && echo b > /etc/pa")
    assert info["targets"] == ["/tmp/ok.txt", "/etc/pa"]
    assert info["protected"] == [("/etc/pa", "system")]


def test_non_redirect_write_commands_not_covered(monkeypatch):
    """已知边界（#757 Spec 只认重定向）：cp/mv/tee 的写入目标不在提取范围。"""
    _on(monkeypatch)
    info = ca.audit_command("cp k /etc/passwd && mv a .env")
    assert info["targets"] == [] and info["protected"] == []


# ── 大小上限 ────────────────────────────────────────────────────────

def test_size_guard_skips_huge_command(monkeypatch):
    _on(monkeypatch)
    big = "cat > /tmp/x.md <<'EOF'\n" + ("line > /tmp/fake\n" * 30000) + "EOF\n"
    assert len(big) > ca._MAX_AUDIT_CHARS
    info = ca.audit_command(big)
    assert info["skipped"] is True and info["targets"] == []


def test_size_guard_boundary_parses(monkeypatch):
    _on(monkeypatch)
    cmd = "echo x > /tmp/a.log".ljust(ca._MAX_AUDIT_CHARS - 10, " ")
    assert len(cmd) <= ca._MAX_AUDIT_CHARS
    info = ca.audit_command(cmd)
    assert info["skipped"] is False and info["targets"] == ["/tmp/a.log"]


# ── tool_use 桥接 ──────────────────────────────────────────────────

def test_tool_use_disabled_is_noop(monkeypatch):
    _off(monkeypatch)
    info = ca.audit_tool_use({"input": {"command": "echo x > .env"}})
    assert info["enabled"] is False and info["targets"] == []


def test_tool_use_extracts_command_and_flags(monkeypatch, caplog):
    _on(monkeypatch)
    tu = {"input": {"command": "echo A=1 > .env"}}
    with caplog.at_level("WARNING"):
        info = ca.audit_tool_use(tu, repo_key="g/p", agent_type="coding")
    assert info["targets"] == [".env"]
    assert any("protected write target" in r.message for r in caplog.records)


def test_tool_use_invalid_inputs_do_not_raise(monkeypatch):
    _on(monkeypatch)
    for bad in ({}, None, "not-a-dict", {"input": None}, {"input": {"command": None}}):
        info = ca.audit_tool_use(bad if isinstance(bad, dict) else {})
        assert info["targets"] == []


def test_audit_failure_is_swallowed(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(ca, "extract_write_targets", lambda _c: (_ for _ in ()).throw(RuntimeError("boom")))
    info = ca.audit_command("echo x > /tmp/a")
    assert info["targets"] == [] and info["enabled"] is True