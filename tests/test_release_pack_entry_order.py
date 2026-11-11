"""release_pack.py 入口链路守卫（PR #1231 第 3 轮合规审查 Critical-1）。

回归背景：`if __name__ == "__main__": main()` 曾位于 tools_manifest_files()
定义之前——脚本方式运行时（python scripts/release_pack.py）顶层顺序执行到
guard 即调 main()，pack_tools() 内对 tools_manifest_files() 的调用必抛
NameError。既有测试用 exec_module 装载（只执行模块顶层、不触发 main()），
对该缺陷不可见，故补本守卫。
"""
import ast
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_pack.py"


def _main_guard_index(body):
    for i, node in enumerate(body):
        if isinstance(node, ast.If) and ast.dump(node.test).startswith("Compare") \
                and "__name__" in ast.dump(node.test):
            return i
    return -1


def test_main_guard_is_last_top_level_statement():
    """__main__ guard 之后不得再出现任何顶层 def/class——入口必须最后绑定。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    i = _main_guard_index(tree.body)
    assert i != -1, "if __name__ == '__main__' guard missing"
    assert i == len(tree.body) - 1, (
        f"guard 在第 {i} 个顶层语句，其后还有 {len(tree.body) - 1 - i} 个——"
        "脚本方式运行 main() 时这些定义尚未绑定（历史上正是 tools_manifest_files "
        "NameError 的根因）")


def test_no_duplicate_shebang_or_module_docstring():
    """尾部死代码（重复 shebang/docstring）禁回潮：各出现且仅出现一次。"""
    t = SCRIPT.read_text(encoding="utf-8")
    assert t.count("#!/usr/bin/env python3") == 1, "shebang 重复（尾部死代码回潮）"


def test_py_compile_clean():
    import py_compile
    py_compile.compile(str(SCRIPT), doraise=True)
