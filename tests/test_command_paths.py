"""Issue #757 回归：bash 命令目标路径提取（heredoc / python3 -c 语法剥离）。

验收三场景（Spec Acceptance Criteria）：
1. heredoc 内容中含路径字符串 → 仅提取重定向目标，内容路径不误判；
2. python3 -c 字符串字面量中的路径 → 不产生目标；
3. 命令自身重定向目标（> / >>）→ 仍被正确解析。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import src.runtime  # noqa: F401  # 先初始化 runtime，规避 src.agent 包循环导入顺序
from src.agent.command_paths import (
    extract_write_targets,
    mask_heredoc_bodies,
    mask_python_c_literals,
)

# ── 验收场景 1：heredoc 内容路径不误判 ─────────────────────────────────────


class TestHeredocContentPaths:
    def test_acceptance_heredoc_body_path_not_target(self):
        cmd = (
            "cat > /tmp/test.md <<'EOF'\n"
            "内容引用技能路径 skills/issue/references/issue-runbook.md 说明。\n"
            "EOF"
        )
        assert extract_write_targets(cmd) == ["/tmp/test.md"]

    def test_unquoted_delimiter(self):
        cmd = (
            "cat > /tmp/test.md <<EOF\n"
            "skills/issue/references/issue-runbook.md\n"
            "EOF"
        )
        assert extract_write_targets(cmd) == ["/tmp/test.md"]

    def test_dash_delimiter_with_tabs(self):
        cmd = (
            "cat >> /tmp/test.md <<-EOF\n"
            "\t见 skills/issue/references/issue-runbook.md\n"
            "\tEOF"
        )
        assert extract_write_targets(cmd) == ["/tmp/test.md"]

    def test_double_quoted_delimiter(self):
        cmd = (
            "cat > /tmp/out.txt <<\"EOF\"\n"
            "foo > bar && cat /etc/passwd > /tmp/evil\n"
            "EOF"
        )
        # 块内伪造的重定向不得被提取
        assert extract_write_targets(cmd) == ["/tmp/out.txt"]

    def test_unclosed_heredoc_conservative(self):
        # 未闭合：保守不剥离，但命令自身重定向目标仍可提取
        cmd = "cat > /tmp/a.md <<'EOF'\nbody path skills/x/y.md"
        assert extract_write_targets(cmd)[0] == "/tmp/a.md"

    def test_mask_heredoc_bodies_preserves_length_and_newlines(self):
        cmd = "cat <<'EOF'\nabc\ndef\nEOF"
        masked = mask_heredoc_bodies(cmd)
        assert len(masked) == len(cmd)
        assert masked.count("\n") == cmd.count("\n")
        assert "abc" not in masked
        assert "def" not in masked
        # 定界符行保留
        assert masked.endswith("EOF")


# ── 验收场景 2：python3 -c 字符串路径不误判 ────────────────────────────────


class TestPythonCPaths:
    def test_acceptance_python3_c_pathlib(self):
        cmd = (
            "python3 -c \"import pathlib; "
            "p = pathlib.Path('skills/issue/references/issue-runbook.md')\""
        )
        assert extract_write_targets(cmd) == []

    def test_single_quoted_script(self):
        cmd = "python3 -c 'p=\"/tmp/x.md\"; open(p)'"
        assert extract_write_targets(cmd) == []

    def test_python_variant_and_extra_flags(self):
        cmd = "python -c 'x=1' && python3.11 -u -c 'y=2'"
        assert extract_write_targets(cmd) == []

    def test_mask_python_c_literals_keeps_quotes(self):
        cmd = "python3 -c 'print(1)'"
        masked = mask_python_c_literals(cmd)
        assert len(masked) == len(cmd)
        assert masked.startswith("python3 -c '")
        assert masked.endswith("'")
        assert masked[12:-1].strip() == ""  # 字面量内容已全部置空

    def test_unclosed_literal_conservative(self):
        cmd = "python3 -c 'print("
        assert mask_python_c_literals(cmd) == cmd


# ── 验收场景 3：命令自身重定向目标保留 ────────────────────────────────────


class TestRedirectionTargets:
    def test_acceptance_simple_write(self):
        assert extract_write_targets("cat > /tmp/out.txt") == ["/tmp/out.txt"]

    def test_append(self):
        assert extract_write_targets("echo x >> /tmp/a.log") == ["/tmp/a.log"]

    def test_multiple_targets_ordered(self):
        cmd = "echo a > /tmp/1.txt && echo b >> /tmp/2.txt"
        assert extract_write_targets(cmd) == ["/tmp/1.txt", "/tmp/2.txt"]

    def test_fd_prefix_and_stderr(self):
        cmd = "cmd 2> /tmp/err.log 1> /tmp/out.log"
        assert extract_write_targets(cmd) == ["/tmp/err.log", "/tmp/out.log"]

    def test_fd_duplication_not_target(self):
        cmd = "cmd > /tmp/out.txt 2>&1"
        assert extract_write_targets(cmd) == ["/tmp/out.txt"]

    def test_quoted_target_unquoted(self):
        assert extract_write_targets("echo hi > '/tmp/my file.txt'") == ["/tmp/my file.txt"]

    def test_ampersand_redirect(self):
        assert extract_write_targets("cmd &> /tmp/all.log") == ["/tmp/all.log"]

    def test_no_redirection_returns_empty(self):
        assert extract_write_targets("ls -la /tmp") == []
        assert extract_write_targets("cat skills/issue/references/issue-runbook.md") == []

    def test_quoted_string_operator_is_literal(self):
        cmd = 'echo "a > b" > /tmp/real.txt'
        assert extract_write_targets(cmd) == ["/tmp/real.txt"]

    def test_empty_command(self):
        assert extract_write_targets("") == []


# ── 组合场景（Spec 技术方案整体行为）──────────────────────────────────────


class TestCombinedScenarios:
    def test_heredoc_write_with_python_c_in_body(self):
        cmd = (
            "cat > /tmp/spec.md <<'EOF'\n"
            "示例：python3 -c \"import pathlib; pathlib.Path('a/b.md')\"\n"
            "参考 skills/issue/references/issue-runbook.md\n"
            "EOF"
        )
        assert extract_write_targets(cmd) == ["/tmp/spec.md"]

    def test_python_c_then_redirect_in_next_command(self):
        cmd = (
            "python3 -c \"p='skills/x/y.md'\"; echo done > /tmp/out.txt"
        )
        assert extract_write_targets(cmd) == ["/tmp/out.txt"]

    def test_pipeline_with_redirection(self):
        cmd = "cat a.md b.md | grep x > /tmp/grep.out"
        assert extract_write_targets(cmd) == ["/tmp/grep.out"]
