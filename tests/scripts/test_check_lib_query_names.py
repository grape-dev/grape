"""Issue #2462 B1：A3 门禁（scripts/check_lib_query_names.py）单测。

验证「跨域读查询名 ⊆ 目标域 lib 白名单」的静态判定：
句柄变量、直连 public 模块的隐式句柄、工厂返回的句柄、行内调用、
命名查询字面量、动态查询名、作用域隔离、未登记名字、注册表缺失、
探活口跳过、同行多域去重、fail-closed 退出码。
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

#: 直连 public 模块（`GRAPE_*_VIA_LIB` 开关分支的真实写法）
_DIRECT_SNIPPET = '''
from src.domains.runtime import public as _rt


def skill_by_id(skill_id):
    if _rt.available():
        return _rt.get_skill_by_id(skill_id)
    return None
'''

#: 工厂返回隐式句柄，调用方另取变量（settings.py `_creds_via_lib` 的写法）
_FACTORY_SNIPPET = '''
from src.domains.runtime import public as _rt


def _creds_via_lib():
    return _rt if _rt.available() else None


def build():
    _rt = _creds_via_lib()
    return _rt.list_credential_rows()
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

    def test_direct_public_module_import_is_tracked(self):
        """`from src.domains.<domain> import public as _rt` 是隐式句柄，跨域读引用必须可见。"""
        usage = clqn.collect_usage(_DIRECT_SNIPPET, "sample.py")
        assert [(u.domain, u.kind, u.name) for u in usage] == [
            ("runtime", "attr", "get_skill_by_id")]

    def test_probe_name_is_skipped_on_usage_side(self):
        """`_rt.available()` 是探活口，经隐式句柄调用同样不计成未注册函数。"""
        assert all(u.name != "available" for u in clqn.collect_usage(_DIRECT_SNIPPET, "sample.py"))

    def test_direct_public_module_named_query(self):
        snippet = '''
from src.domains.runtime import public as _rt


def f(name):
    return _rt.read_one("skill_x", (name,))
'''
        usage = clqn.collect_usage(snippet, "sample.py")
        assert [(u.domain, u.kind, u.name) for u in usage] == [("runtime", "query", "skill_x")]

    def test_function_local_import_does_not_leak_to_other_scopes(self):
        """函数内 import 的隐式句柄不外泄：别的函数里同名局部变量不是句柄。"""
        snippet = '''
def reader():
    from src.domains.runtime import public as _rt
    return _rt.list_runtimes_rows()


def other(cached):
    for _rt in cached:
        yield _rt.get("last_seen_at")
'''
        usage = clqn.collect_usage(snippet, "sample.py")
        assert [(u.domain, u.name) for u in usage] == [("runtime", "list_runtimes_rows")]

    def test_factory_returning_module_alias_resolves_handle(self):
        """同文件单跳工厂（`return _rt if ... else None`）返回的句柄可用。"""
        usage = clqn.collect_usage(_FACTORY_SNIPPET, "sample.py")
        assert [(u.domain, u.name) for u in usage] == [("runtime", "list_credential_rows")]

    def test_same_line_multi_domain_is_not_deduped(self):
        """同一行两个域的同名引用各自保留（dedup key 含 domain）。"""
        snippet = '''
from src.domains.runtime import public as rt
from src.domains.mgmt import public as mg


def f():
    return rt.read_one("shared_x"), mg.read_one("shared_x")
'''
        usage = clqn.collect_usage(snippet, "sample.py")
        assert {(u.domain, u.kind, u.name) for u in usage} == {
            ("runtime", "query", "shared_x"), ("mgmt", "query", "shared_x")}


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

    def test_unregistered_direct_import_name_fails(self, tmp_path):
        """直连 public 模块引用的未注册名必须判 FAIL（A3 覆盖面缺口回归锚点）。"""
        self._write(tmp_path, _DIRECT_SNIPPET)
        reports = clqn.check([tmp_path], registry={
            "runtime": clqn.DomainRegistry(functions={"read_one", "read_all"}, queries=set())})
        assert not reports[0].ok
        assert reports[0].unregistered_names == {"get_skill_by_id"}

    def test_probe_call_does_not_add_unregistered_name(self, tmp_path):
        """`available()` 探活不进入未注册名集合，注册面只需覆盖真正的查询面。"""
        self._write(tmp_path, _DIRECT_SNIPPET)
        reports = clqn.check([tmp_path], registry={
            "runtime": clqn.DomainRegistry(functions={"get_skill_by_id"}, queries=set())})
        assert reports[0].ok
        assert reports[0].names == {"get_skill_by_id"}

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