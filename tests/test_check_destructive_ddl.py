"""Issue #536 S5：危险 DDL CI 卡点（scripts/check-destructive-ddl.py）单测。

验证行级判定规则：新增 DROP/RENAME/MODIFY COLUMN 必须在 _destructive_ddl(...)
调用行或 cleanup 声明块内，否则 FAIL。
"""
import importlib.util
import os

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCRIPT = os.path.join(_REPO, "scripts", "check-destructive-ddl.py")

spec = importlib.util.spec_from_file_location("check_destructive_ddl", _SCRIPT)
assert spec is not None and spec.loader is not None
cdd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cdd)


class TestLineClassification:
    def test_bare_drop_column_is_destructive(self):
        assert cdd._DESTRUCTIVE_RE.search('c.execute("ALTER TABLE t DROP COLUMN c")')

    def test_bare_rename_column_is_destructive(self):
        assert cdd._DESTRUCTIVE_RE.search("ALTER TABLE t RENAME COLUMN a TO b")

    def test_bare_modify_column_is_destructive(self):
        assert cdd._DESTRUCTIVE_RE.search("ALTER TABLE t MODIFY COLUMN c INT")

    def test_add_column_is_not_destructive(self):
        assert not cdd._DESTRUCTIVE_RE.search("ALTER TABLE t ADD COLUMN c INT")

    def test_collected_call_line_passes(self):
        line = '_destructive_ddl(c, "ALTER TABLE t DROP COLUMN c", kind_hint="cleanup")'
        assert cdd._COLLECTED_RE.search(line)

    def test_cleanup_decl_line_passes(self):
        assert cdd._CLEANUP_DECL_RE.search(
            'register_migration("202611010001", "x", "cleanup", fn)')


class TestMainVerdict:
    def test_current_diff_passes(self, capsys):
        """对本仓库真实基线（upstream）diff 的判定必须 PASS——覆盖多行调用/docstring/探测行。"""
        rc = cdd.main(["check-destructive-ddl", cdd._detect_base()])
        out = capsys.readouterr().out
        assert rc == 0
        assert "PASS" in out or "WARN" in out

    def test_violation_lines_reported(self, monkeypatch, capsys):
        monkeypatch.setattr(cdd, "_added_lines",
                            lambda base: (['c.execute("ALTER TABLE t DROP COLUMN c")'], None))
        rc = cdd.main(["check-destructive-ddl", "fake-base"])
        assert rc == 1
        assert "FAIL" in capsys.readouterr().out

    def test_collected_violation_lines_pass(self, monkeypatch, capsys):
        monkeypatch.setattr(cdd, "_added_lines", lambda base: ([
            '_destructive_ddl(c, "ALTER TABLE t DROP COLUMN c", kind_hint="cleanup")',
            'c.execute("ALTER TABLE t ADD COLUMN c INT")',
        ], None))
        rc = cdd.main(["check-destructive-ddl", "fake-base"])
        assert rc == 0
        assert "PASS" in capsys.readouterr().out


class TestClassifierFalsePositives:
    """状态机分类器：真实仓库 diff 里出现过的三类非违规形态。"""

    def test_multiline_collected_call_passes(self):
        added = [
            "try:",
            "    c = db.cursor()",
            "    _destructive_ddl(",
            '        c, "ALTER TABLE grape_accounts MODIFY COLUMN email VARCHAR(255) DEFAULT NULL",',
            '        kind_hint="cleanup")',
        ]
        assert cdd._classify_added_lines(added) == []

    def test_docstring_mention_passes(self):
        added = [
            "def _destructive_ddl(c, ddl: str, *, kind_hint: str = \"cleanup\"):",
            '    """危险 DDL（DROP/RENAME/MODIFY COLUMN）唯一入口（Issue #536 D4-2）。',
            "",
            "    收口说明。",
            '    """',
        ]
        assert cdd._classify_added_lines(added) == []

    def test_membership_check_line_passes(self):
        added = [
            '                if "DROP COLUMN" in sql or "RENAME COLUMN" in sql or "MODIFY COLUMN" in sql:',
        ]
        assert cdd._classify_added_lines(added) == []

    def test_comment_line_passes(self):
        added = [
            "    # Issue #536：MODIFY COLUMN 属破坏性 DDL，统一走收口。",
        ]
        assert cdd._classify_added_lines(added) == []

    def test_real_violation_still_flagged(self):
        added = [
            '    c.execute("ALTER TABLE t DROP COLUMN c")',
        ]
        assert cdd._classify_added_lines(added) == [
            'c.execute("ALTER TABLE t DROP COLUMN c")']

    def test_uncollected_multiline_ddl_flagged(self):
        added = [
            "    c.execute(",
            '        "ALTER TABLE t RENAME COLUMN a TO b")',
        ]
        assert len(cdd._classify_added_lines(added)) == 1
class TestBaseResolution:
    """QA-Impt：base 解析优先级 + 不可解析时 fail-closed（退出码 2）。"""

    def test_detect_base_env_override(self, monkeypatch):
        """CI 环境变量（目标分支）优先于 upstream/main。"""
        monkeypatch.setenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "dev-branch")
        assert cdd._detect_base() == "dev-branch"
        monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
        monkeypatch.setenv("GRAPE_WORK_BRANCH", "local-liufei")
        assert cdd._detect_base() == "local-liufei"
        monkeypatch.delenv("GRAPE_WORK_BRANCH")

    def test_unresolvable_base_fails_closed(self, capsys):
        """base 不可解析（origin 与本地均不存在）→ FAIL（退出码 2），不做 fail-open。"""
        rc = cdd.main(["check-destructive-ddl", "no-such-base-xyz-536"])
        out = capsys.readouterr().out
        assert rc == 2
        assert "FAIL" in out and "base" in out

    def test_explicit_base_wins_over_env(self, monkeypatch, capsys):
        """显式命令行 base 优先于环境变量（CI 显式传目标分支）。"""
        monkeypatch.setenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "some-other-branch")
        rc = cdd.main(["check-destructive-ddl", "no-such-base-xyz-536"])
        out = capsys.readouterr().out
        assert rc == 2
        assert "no-such-base-xyz-536" in out