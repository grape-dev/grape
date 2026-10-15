"""release_pack SQL 转义单元测试（QA 审查 Required 发现：meta 种子 INSERT f-string 拼接未转义）。

库内 value 含单引号/反斜杠时，未转义拼接会破坏导出 seed SQL 语法（注入面）。
_sql_escape_str 为所有 INSERT 值生成（含 meta 系统键种子）的统一转义入口。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.release_pack import _sql_escape_str


class TestSqlEscapeStr:
    def test_plain_value_unchanged(self):
        assert _sql_escape_str("plugin") == "plugin"

    def test_single_quote_escaped(self):
        assert _sql_escape_str("it's plugin") == "it\\'s plugin"

    def test_backslash_escaped(self):
        assert _sql_escape_str("a\\b") == "a\\\\b"

    def test_combined(self):
        assert _sql_escape_str("va'lue\\x'") == "va\\'lue\\\\x\\'"

    def test_non_str_coerced(self):
        assert _sql_escape_str(123) == "123"

    def test_meta_seed_line_well_formed(self):
        """模拟 meta 种子 INSERT 生成：转义后的 value 不破坏 SQL 语法。"""
        _mk = "execution_mode"
        _mv_escaped = _sql_escape_str("aut'o")
        line = f"INSERT INTO `meta` (`key`, `value`) VALUES ('{_mk}', '{_mv_escaped}');"
        assert line == "INSERT INTO `meta` (`key`, `value`) VALUES ('execution_mode', 'aut\\'o');"
        # 转义后字符串仍是合法 SQL 字面量：单引号成对（不含转义后的 \'）
        body = line.split("VALUES", 1)[1]
        unescaped = body.replace("\\'", "").replace("\\\\", "\\")
        assert unescaped.count("'") == 4  # 两个字面量的首尾引号
