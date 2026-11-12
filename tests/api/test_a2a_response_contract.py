"""Issue #2050 契约门禁：POST /api/a2a 的每条响应都必须回显 handoff_result / source_seq。

上一轮评审的复发形态是：契约把两个字段定为 required，实现却靠人工逐条 return 补——
补一处漏一处，QA 每轮都能再找到几条。这里用 AST 静态扫描端点内所有 return，
把覆盖面锁死：新增返回路径忘了带字段，本用例直接红，不必等下一轮评审。

间接返回（返回别处构造好的响应体）登记在 INDIRECT 中，并由第二个用例
核对该构造函数确实带了两个字段。
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src", "domains", "runtime", "routes", "webhook.py")

# 这些 helper 内部统一补字段，端点只要 return 它们的调用即可。
NORMALIZED_HELPERS = ("_a2a_guard_skip_response(", "_augment_a2a_dispatch_response(")

# 端点里返回「别处构造好的响应体」的写法 → 保证字段的函数。
INDIRECT = {
    '_redirect["response"]': "_cross_pipeline_handoff",
}

REQUIRED_FIELDS = ("handoff_result", "source_seq")


def _source() -> str:
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {SRC}")


def _returns(fn_name: str):
    src = _source()
    fn = _function(ast.parse(src), fn_name)
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Return):
            seg = ast.get_source_segment(src, node.value if node.value is not None else node) or ""
            out.append((node.lineno, seg))
    return out


def _missing(fn_name: str, allow_indirect: bool):
    missing = []
    for lineno, seg in _returns(fn_name):
        if all(f in seg for f in REQUIRED_FIELDS):
            continue
        if any(h in seg for h in NORMALIZED_HELPERS):
            continue
        if allow_indirect and seg.strip() in INDIRECT:
            continue
        missing.append((lineno, seg.strip().splitlines()[0][:100]))
    return missing


def test_every_a2a_return_carries_contract_fields():
    segs = _returns("a2a_endpoint")
    assert len(segs) >= 30, f"a2a_endpoint 返回点数量异常（{len(segs)}），扫描可能失效"
    missing = _missing("a2a_endpoint", allow_indirect=True)
    assert not missing, (
        "以下 return 未回显 handoff_result / source_seq（契约 required）：\n"
        + "\n".join(f"  webhook.py:{ln} {code}" for ln, code in missing)
    )


def test_indirect_return_targets_are_contract_complete():
    for seg_text, fn_name in INDIRECT.items():
        targets = [(ln, s) for ln, s in _returns(fn_name) if '"response"' in s]
        assert targets, f"{fn_name} 未找到构造 response 的 return"
        for ln, seg in targets:
            for field in REQUIRED_FIELDS:
                assert field in seg, f"{fn_name}:{ln} 的 response 缺 {field}（{seg_text} 依赖它）"