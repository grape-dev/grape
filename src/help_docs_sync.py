"""help/api markdown ↔ OpenAPI 契约一致性（单一事实源 = /openapi.json）。

api-docs-standardization-design.md R3：供 `scripts/check_api_docs.py`（CLI）与
`tests/test_api_docs_sync.py`（pytest 防回归）共用，保证脚本与测试口径一致。

- 契约侧：`load_contract_ops()` 从当前代码渲染 /openapi.json。
- 文档侧：`load_doc_ops()` 解析 help/api/*.md 的 `METHOD /path` 端点标题。
- 路径参数名归一化：`{owner}` / `{repo_name}` 一律视为 `{}`；剥离 query 片段。
- `DOC_EXEMPT`：允许出现在文档但不在契约的内部/无 token 端点。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HELP_DIR = os.path.join(_REPO_ROOT, "help", "api")

# 契约中允许不被 markdown 覆盖、或文档保留但契约不收录的端点（内部/无 token）。
DOC_EXEMPT: frozenset[tuple[str, str]] = frozenset({
    # 事件流程重置（events.py，内部 worker/前端面板用，不走契约标注）
    ("post", "/api/events/{seq}/flow/reset"),
    # 无 Token 公开端点（UI 用），不进契约但仍属对外可用接口
    ("get", "/api/agents/types"),
    ("get", "/api/skills/template"),
})

_METHOD_HEAD = re.compile(
    r"^#{2,4}\s+((?:GET|POST|PUT|PATCH|DELETE)(?:/(?:GET|POST|PUT|PATCH|DELETE))*)\s+(/api/[^\s`]+)"
    r"(?:\s+[—\-–]?\s*(.*))?$"
)


def _normalize(path: str) -> str:
    """路径参数名归一化：{owner}/{repo_name} → {}/{}；移除 query 片段。"""
    path = path.split("?", 1)[0]
    return re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "{}", path)


def _iter_help_files() -> list[tuple[str, str]]:
    files = []
    for fname in sorted(os.listdir(_HELP_DIR)):
        if fname.endswith(".md"):
            files.append((os.path.join(_HELP_DIR, fname), fname))
    return files


def load_doc_ops() -> set[tuple[str, str]]:
    """解析 help/api/*.md，返回 {(method, normalized_path)} 集合。"""
    ops = set()
    for path, _fname in _iter_help_files():
        text = open(path, encoding="utf-8").read()
        for line in text.splitlines():
            m = _METHOD_HEAD.match(line.strip())
            if not m:
                continue
            for method in m.group(1).split("/"):  # "GET/POST" 联合标题
                ops.add((method.lower(), _normalize(m.group(2))))
    return ops


def _load_app_schema():
    """惰性导入并渲染当前代码的 OpenAPI schema（DB 走临时 sqlite，避免碰生产库）。"""
    os.environ.setdefault(
        "DEVOPS_DB_PATH",
        os.path.join(tempfile.gettempdir(), "grape_openapi_helpcheck.db"),
    )
    os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")
    sys.path.insert(0, _REPO_ROOT)
    from src.api.openapi import custom_openapi
    from src.api.main import app

    return custom_openapi(app)


def load_contract_ops() -> dict[tuple[str, str], str]:
    """渲染契约，返回 {(method, normalized_path): operation_id}。"""
    schema = _load_app_schema()
    return {
        (m.lower(), _normalize(p)): op.get("operationId", "")
        for p, ops_ in (schema.get("paths") or {}).items()
        for m, op in ops_.items()
    }


def compare():
    """返回 (missing, extra)。

    - missing: 契约有、文档缺（且非白名单）→ [(method, path, operation_id)]
    - extra:   文档有、契约无（且非白名单）→ [(method, path)]
    """
    contract = load_contract_ops()
    docs = load_doc_ops()
    exempt = {(m_.lower(), _normalize(p)) for m_, p in DOC_EXEMPT}

    missing = sorted(
        (m, p, op_id)
        for (m, p), op_id in contract.items()
        if (m, p) not in docs and (m, p) not in exempt
    )
    extra = sorted(
        (m, p)
        for m, p in docs
        if (m, p) not in contract and (m, p) not in exempt
    )
    return missing, extra