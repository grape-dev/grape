"""Issue #2479：禁止矩阵静态门禁（scripts/check_gsb_route_matrix.py）单测。

覆盖 QA 复审指出的 fail-open 缺口（负例先红后绿）：
客户端类别名导入后的直连调用、apps 三个同域进程入口纳入扫描根、
单文件根目录扫描与原有短名判定不回退。
"""
import ast
import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_gsb_route_matrix.py"

spec = importlib.util.spec_from_file_location("check_gsb_route_matrix", _SCRIPT)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
sys.modules["check_gsb_route_matrix"] = gate
spec.loader.exec_module(gate)


_ALIAS_SNIPPET = '''
from src.kernel.gsb.client import GsbClient as GC


def route():
    return GC("mgmt", timeout=1)
'''

_DIRECT_SNIPPET = '''
from src.kernel.gsb.client import GsbClient


def route():
    return GsbClient("mgmt")
'''

_ALIAS_OTHER_DOMAIN_SNIPPET = '''
from src.kernel.gsb.client import ServiceHttpClient as SHC


def route():
    return SHC("runtime")
'''


def _scan(snippet: str) -> list:
    return gate.scan_tree(ast.parse(snippet), Path("sample.py"), "sample")


class TestClientAliasDetection:
    def test_aliased_client_class_call_is_detected(self):
        violations = _scan(_ALIAS_SNIPPET)
        assert [(v.kind, v.line) for v in violations] == [("mgmt-remote-client", 6)]

    def test_aliased_client_class_for_other_domain_is_allowed(self):
        assert _scan(_ALIAS_OTHER_DOMAIN_SNIPPET) == []

    def test_direct_client_class_call_still_detected(self):
        violations = _scan(_DIRECT_SNIPPET)
        assert [v.kind for v in violations] == ["mgmt-remote-client"]

    def test_bare_registry_access_still_detected(self):
        violations = _scan("def f(provider):\n    return _PROVIDERS\n")
        assert [v.kind for v in violations] == ["registry-bypass"]


class TestScanRoots:
    def test_apps_entrypoints_are_scanned(self):
        expected = {
            "apps/runtime/worker/main.py",
            "apps/runtime/webhook/main.py",
            "apps/insight/main.py",
        }
        assert expected <= set(gate.SCAN_ROOTS)

    def test_single_file_root_is_scanned(self, tmp_path):
        entry = tmp_path / "main.py"
        entry.write_text(_ALIAS_SNIPPET, encoding="utf-8")
        violations, files = gate.scan_roots([entry])
        assert files == 1
        assert [v.kind for v in violations] == ["mgmt-remote-client"]