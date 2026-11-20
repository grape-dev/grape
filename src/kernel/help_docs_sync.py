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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HELP_DIR = os.path.join(_REPO_ROOT, "help", "api")

# 契约中允许不被 markdown 覆盖、或文档保留但契约不收录的端点（内部/无 token）。
# grape CLI 本地命令（客户端实现，无 HTTP 语义，不进契约 operation 集）。
# 单一事实源（Issue #2237，#1302 移植，#2086 两半交付条件 A）：仅在此模块定义一处，
# 禁止落兼容别名/shim 或第二份副本；`/openapi.json` 经
# `info["x-grape-local-cli-commands"]` 原样发布，grape-cli `grape commands`
# 据此派生列示并带 `[local]` 标注，不存第二份清单；
# tests/test_api_docs_sync.py 断言其与契约 operation 集合不相交、info 字段与常量一致。
def cli_local_command_to_operation_id(command: str) -> str:
    """本地 CLI 命令名 → 契约 operation_id 口径（空格转下划线），供不相交断言复用。

    （Issue #2237，#1302 移植；tests 借此把 KNOWN_LOCAL_CLI_COMMANDS 映射进
    operation_id 空间做与契约集合的不相交断言。）"""
    return command.strip().replace(" ", "_")

KNOWN_LOCAL_CLI_COMMANDS: tuple[str, ...] = (
    "git",         # 原始 git 透传（grape git <raw git command>）
    "refresh",     # 本地契约刷新（重新拉取 /openapi.json 缓存）
    "commands",    # 本地命令发现（列契约命令与本地命令，自身不属契约）
    "tasks wait",  # 本地阻塞等待（仅见 `grape tasks --help`）
)

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

_DOCS_OPENAPI_DIR = os.path.join(_REPO_ROOT, "docs", "openapi")

# 挂牌态文档标注的扫描面：help/api 是用户面契约参考，docs/openapi 是源设计文档
# （Issue #2837 Spec 要求两处同步标注；缺任一处即判未联动）。
_DOC_SOURCES = (("help/api", _HELP_DIR), ("docs/openapi", _DOCS_OPENAPI_DIR))


def normalize_path(path: str) -> str:
    """公开的路径归一口径（与 load_doc_ops 同一实现，供文档面门禁复用）。"""
    return _normalize(path)


def _iter_md_files(directory: str) -> list[str]:
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    return [os.path.join(directory, name) for name in names if name.endswith(".md")]


def load_doc_blocks() -> dict[tuple[str, str], dict[str, str]]:
    """解析 help/api 与 docs/openapi 的端点块，返回 {(method, path): {来源: 正文}}。

    端点块 = `METHOD /path` 标题行起、至下一个任意级别 markdown 标题止的正文。
    两处来源分别保留正文（挂牌态标注须两处同步，Issue #2837），同源重复端点
    取首个块。
    """
    blocks: dict[tuple[str, str], dict[str, str]] = {}
    heading_re = re.compile(r"^#{2,4}\s+")
    for source, directory in _DOC_SOURCES:
        for path in _iter_md_files(directory):
            text = open(path, encoding="utf-8").read()
            fname = f"{source}/{os.path.basename(path)}"
            current: list[tuple[str, str]] = []
            body_lines: list[str] = []

            def _flush() -> None:
                for method, p in current:
                    blocks.setdefault((method, p), {}).setdefault(
                        fname, "\n".join(body_lines))

            for line in text.splitlines():
                m = _METHOD_HEAD.match(line.strip())
                if m:
                    _flush()
                    current = [(method.lower(), _normalize(m.group(2)))
                               for method in m.group(1).split("/")]
                    body_lines = []
                    continue
                if heading_re.match(line.strip()):
                    _flush()
                    current = []
                    body_lines = []
                    continue
                if current:
                    body_lines.append(line)
            _flush()
    return blocks
