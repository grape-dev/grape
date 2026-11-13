"""Issue #2462 B1：A3 门禁（scripts/check_lib_query_names.py）单测。

验证「跨域读查询名 ⊆ 目标域 lib 白名单」的静态判定：
句柄变量、行内调用、命名查询字面量、动态查询名、作用域隔离、
未登记名字、注册表缺失、fail-closed 退出码。
"""
import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_lib_query_names.py"

spec = importlib.util.spec_from_file_location("check_lib_query_names", _SCRIPT)
assert spec is not None and spec.loader is not None
clqn = importlib.util.module_from_spec(spec)
sys.modules["check_lib_query_names"] = clqn
spec.loader.exec_module(clqn)


_HANDLE_SNIPPET = '''
from src.kernel.domain_read import runtime_lib as _rt


def read_skill(name):
    lib = _rt(foreign_only=True)
    if lib is not None:
        return lib.read_one("skill_x", (name,))
    return None
'''

_INLINE_SNIPPET = '''
from src.kernel.domain_read import mgmt_lib


def repo_field(key):
    return mgmt_lib().get_repo_field(key, "bot_username")
'''


class TestCollectUsage:
    def test_handle_variable_named_query_detected(self):
        usage = clqn.collect_usage(_HANDLE_SNIPPET, "sample.py")
        assert [(u.domain, u.kind, u.name) for u in usage] == [("runtime", "query", "skill_x")]

    def test_inline_call_attribute_detected(self):
        usage = clqn.collect_usage(_INLINE_SNIPPET, "sample.py")
        assert [(u.domain, u.kind, u.name) for u in usage] == [("mgmt", "attr", "get_repo_field")]

    def test_named_query_method_is_not_counted_as_plain_attribute(self):
        """`read_one(...)` 只记一次（记成命名查询），不重复记成函数引用。"""
        usage = clqn.collect_usage(_HANDLE_SNIPPET, "sample.py")
        assert [u.name for u in usage] == ["skill_x"]

    def test_dynamic_query_name_is_flagged(self):
        snippet = '''
from src.kernel.domain_read import runtime_lib as rt


def f(name, params):
    lib = rt()
    return lib.read_all(name, params)
'''
        usage = clqn.collect_usage(snippet, "sample.py")
        assert [(u.kind, u.name) for u in usage] == [("dynamic_query", "")]

    def test_file_without_domain_read_import_is_ignored(self):
        snippet = 'def f(lib):\n    return lib.read_one("x")\n'
        assert clqn.collect_usage(snippet, "sample.py") == []

    def test_scopes_are_attributed_independently(self):
        """同一文件里同名句柄变量指向不同域时，各自归属自己的域（不串味）。"""
        snippet = '''
from src.kernel.domain_read import runtime_lib as rt
from src.kernel.domain_read import mgmt_lib as mg


def a():
    lib = rt()
    return lib.list_runtimes()


def b():
    lib = mg()
    return lib.fetch_rows("managed_repos", ["repo_key"])
'''
        usage = {(u.domain, u.name) for u in clqn.collect_usage(snippet, "sample.py")}
        assert usage == {("runtime", "list_runtimes"), ("mgmt", "fetch_rows")}

    def test_nested_function_body_is_not_folded_into_outer_scope(self):
        """外层函数不把内层函数里的句柄当成自己的（曾因作用域 walk 折入而重复计数）。"""
        snippet = '''
from src.kernel.domain_read import runtime_lib as rt


def outer():
    def inner():
        lib = rt()
        return lib.read_all("runtime_x")

    return inner()
'''
        usage = clqn.collect_usage(snippet, "sample.py")
        assert [(u.kind, u.name) for u in usage] == [("query", "runtime_x")]


class TestRegistry:
    def test_registry_exposes_functions_and_named_queries(self):
        registry = clqn.collect_registry("runtime")
        assert registry is not None
        assert "read_one" in registry.functions and "read_all" in registry.functions
        assert "available" not in registry.functions
        assert registry.queries, "runtime lib 必须有 _READ_QUERIES 命名查询表"

    def test_registry_named_queries_are_usable(self):
        registry = clqn.collect_registry("mgmt")
        assert registry is not None
        assert "managed_repos_b939bc" in registry.queries

    def test_missing_domain_module_yields_none(self):
        """不存在的域 → 注册表缺失（insight 目前没有 public 模块）。"""
        assert clqn.collect_registry("no_such_domain_xyz") is None


class TestCheckVerdict:
    def _write(self, tmp_path, snippet):
        (tmp_path / "mod.py").write_text(snippet, encoding="utf-8")

    def test_registered_query_passes(self, tmp_path):
        self._write(tmp_path, _HANDLE_SNIPPET)
        reports = clqn.check([tmp_path], registry={
            "runtime": clqn.DomainRegistry(functions={"read_one", "read_all"}, queries={"skill_x"})})
        assert reports[0].ok

    def test_unregistered_query_fails(self, tmp_path):
        self._write(tmp_path, _HANDLE_SNIPPET)
        reports = clqn.check([tmp_path], registry={
            "runtime": clqn.DomainRegistry(functions={"read_one", "read_all"}, queries={"other_x"})})
        assert not reports[0].ok
        assert reports[0].unregistered_queries == {"skill_x"}

    def test_unregistered_function_fails(self, tmp_path):
        self._write(tmp_path, _INLINE_SNIPPET)
        reports = clqn.check([tmp_path], registry={
            "mgmt": clqn.DomainRegistry(functions={"fetch_rows"}, queries=set())})
        assert reports[0].unregistered_names == {"get_repo_field"}

    def test_dynamic_query_fails_even_when_registry_is_complete(self, tmp_path):
        self._write(tmp_path, '''
from src.kernel.domain_read import runtime_lib as rt


def f(name):
    return rt().read_one(name)
''')
        reports = clqn.check([tmp_path], registry={
            "runtime": clqn.DomainRegistry(functions={"read_one"}, queries={"skill_x"})})
        assert not reports[0].ok
        assert len(reports[0].dynamic) == 1


class TestCli:
    def test_repo_src_is_closed(self, capsys):
        """对仓库真实 src/ 的判定必须通过（A3 的回归锚点）。"""
        assert clqn.main([str(_REPO / "src")]) == 0
        assert "PASS" in capsys.readouterr().out

    def test_violation_exits_one(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "mod.py").write_text(_HANDLE_SNIPPET, encoding="utf-8")
        monkeypatch.setattr(clqn, "collect_registry",
                            lambda domain: clqn.DomainRegistry(functions={"read_one"}, queries=set()))
        assert clqn.main([str(tmp_path)]) == 1
        assert "unregistered named query" in capsys.readouterr().out

    def test_empty_scan_fails_closed(self, tmp_path, capsys):
        assert clqn.main([str(tmp_path)]) == 2
        assert "fail-closed" in capsys.readouterr().out