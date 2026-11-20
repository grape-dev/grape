"""OpenAPI 转换器（Issue #1094，总纲 #1078 D3 M1）。

设计依据：docs/issue-1094/plugin-market-import-design.md §6
链路：解析 OpenAPI 文档 → 能力面分析（operations 按 tag 分组，对接 plugin-creator
能力面输入口径）→ 生成插件封装包（manifest.json + openapi.json + CAPABILITY.md +
wrapper/rest_client.py），产出经 package 校验后交 plugin-tester 契约校验（D1 #1092
合入后接入，转换器只负责长出可上架插件形态）。

公开 seam：
- analyze_capabilities(openapi_doc) -> CapabilitySurface
- convert_openapi_to_plugin(openapi_doc, name, ..., out_dir=None) -> PluginPackage
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.domains.mgmt.plugin_market.manifest import (
    DEFAULT_CATEGORY,
    MANIFEST_SCHEMA_VERSION,
    build_manifest,
)

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass
class Operation:
    """能力面原子操作（一次 path×method 调用）。"""

    operation_id: str
    method: str
    path: str
    summary: str = ""
    tag: str = "default"


@dataclass
class CapabilitySurface:
    """能力面分析结果：对接 plugin-creator 的能力面输入。"""

    title: str = ""
    version: str = ""
    description: str = ""
    base_url: str = ""
    auth_required: bool = False
    operations: list = field(default_factory=list)
    tags: list = field(default_factory=list)


@dataclass
class PluginPackage:
    """插件封装包：manifest + 包内文件（相对路径 → 文本内容）。"""

    manifest: dict
    files: dict = field(default_factory=dict)


def _sanitize_tool_name(raw: str) -> str:
    name = _TOOL_NAME_RE.sub("_", raw)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "op"


def _fallback_id(method: str, path: str) -> str:
    return _sanitize_tool_name(f"{method}_{path}")


def analyze_capabilities(openapi_doc: dict) -> CapabilitySurface:
    """从 OpenAPI 文档提取能力面（operations/tags/base_url/auth）。"""
    info = openapi_doc.get("info") or {}
    servers = openapi_doc.get("servers") or []
    security_schemes = (openapi_doc.get("components") or {}).get("securitySchemes") or {}

    surface = CapabilitySurface(
        title=info.get("title", ""),
        version=str(info.get("version", "")),
        description=info.get("description", ""),
        base_url=(servers[0] or {}).get("url", "") if servers else "",
        auth_required=bool(security_schemes),
    )

    used_ids: set = set()
    for path, path_item in (openapi_doc.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            if op.get("operationId"):
                op_id = _sanitize_tool_name(op["operationId"])
            else:
                op_id = _fallback_id(method, path)
            if op_id in used_ids:
                suffix = 2
                while f"{op_id}_{suffix}" in used_ids:
                    suffix += 1
                op_id = f"{op_id}_{suffix}"
            used_ids.add(op_id)
            tags = op.get("tags") or []
            surface.operations.append(
                Operation(
                    operation_id=op_id,
                    method=method,
                    path=path,
                    summary=op.get("summary") or op.get("description") or "",
                    tag=tags[0] if tags else "default",
                )
            )

    surface.tags = sorted({op.tag for op in surface.operations})
    return surface


_CLIENT_TEMPLATE = '''\
"""Generated REST client for plugin {plugin_name}.

由 grape OpenAPI 转换器生成（Issue #1094，src/plugin_market/openapi_converter.py）。
能力面来源：{api_title} {api_version}。
"""
from __future__ import annotations

import httpx


class RestClient:
    """对 {base_url} 的薄封装：每个 OpenAPI operation 一个方法。"""

    def __init__(self, base_url="{base_url}", api_key=None, timeout=30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        headers = {{"Accept": "application/json"}}
        if api_key:
            headers["Authorization"] = f"Bearer {{api_key}}"
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

{methods}
'''


def _generate_client(source: str, plugin_name: str, surface: CapabilitySurface) -> str:
    methods = []
    for op in surface.operations:
        # 每方法只封装 HTTP 调用语义；参数拼装留给 plugin-creator 精化
        methods.append(
            f"    def {op.operation_id}(self, params=None, json=None):\n"
            f'        """{op.summary or op.operation_id} [{op.method.upper()} {op.path}]"""\n'
            f"        resp = self._client.request(\n"
            f'            "{op.method.upper()}",\n'
            f'            "{op.path}",\n'
            f"            params=params,\n"
            f"            json=json,\n"
            f"        )\n"
            f"        resp.raise_for_status()\n"
            f"        return resp.json()\n"
        )
    return source.format(
        plugin_name=plugin_name,
        api_title=surface.title,
        api_version=surface.version,
        base_url=surface.base_url,
        methods="\n".join(methods),
    )


def _generate_capability_md(surface: CapabilitySurface, manifest: dict) -> str:
    lines = [
        f"# {manifest['display_name']} 能力面分析",
        "",
        f"- API：{surface.title} {surface.version}",
        f"- base_url：{surface.base_url}",
        f"- 需凭据：{'是' if surface.auth_required else '否'}",
        f"- 操作数：{len(surface.operations)}",
        f"- 标签：{', '.join(surface.tags)}",
        "",
        "## 操作清单（plugin-creator 能力面输入）",
        "",
        "| 工具名 | 方法 | 路径 | 说明 | 标签 |",
        "|--------|------|------|------|------|",
    ]
    for op in surface.operations:
        summary = (op.summary or "-").replace("|", "\\|")
        lines.append(f"| `{op.operation_id}` | {op.method.upper()} | `{op.path}` | {summary} | {op.tag} |")
    return "\n".join(lines) + "\n"


def convert_openapi_to_plugin(
    openapi_doc: dict,
    name: str,
    author: str = "",
    category: str = DEFAULT_CATEGORY,
    display_name: str | None = None,
    description: str = "",
    out_dir: str | None = None,
) -> PluginPackage:
    """从 OpenAPI 文档长出可上架插件封装包。

    - manifest：source=openapi，entry=rest(base_url)，capabilities.tools=操作清单
    - files：manifest.json / openapi.json（原文档规范化落盘）/ CAPABILITY.md / wrapper/rest_client.py
    - out_dir 提供时同步写盘（物料目录生成用）
    """
    surface = analyze_capabilities(openapi_doc)

    source_ref = ((openapi_doc.get("info") or {}).get("contact") or {}).get("url", "")
    manifest = build_manifest(
        name=name,
        version=surface.version or "1.0.0",
        display_name=display_name or surface.title or name,
        description=description or surface.description,
        author=author,
        category=category,
        source={"type": "openapi", "ref": source_ref},
        auth={"required": surface.auth_required, "notes": "securitySchemes 存在时置 True，由 tester 校验真实凭据需求" if surface.auth_required else ""},
        entry={"kind": "rest", "base_url": surface.base_url},
        capabilities={
            "tools": [
                {"name": op.operation_id, "description": op.summary or "", "method": op.method, "path": op.path}
                for op in surface.operations
            ]
        },
    )

    normalized_doc = json.loads(json.dumps(openapi_doc, ensure_ascii=False, sort_keys=True))
    normalized_doc["_meta"] = {"generated_by": "grape-plugin-market"}

    package = PluginPackage(
        manifest=manifest,
        files={
            "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            "openapi.json": json.dumps(normalized_doc, ensure_ascii=False, indent=2) + "\n",
            "CAPABILITY.md": _generate_capability_md(surface, manifest),
            "wrapper/rest_client.py": _generate_client(_CLIENT_TEMPLATE, name, surface),
        },
    )

    if out_dir:
        root = Path(out_dir)
        for rel, content in package.files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    return package
