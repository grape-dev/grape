"""插件市场 manifest.json 模式与校验（Issue #1094，总纲 #1078 D3）。

设计依据：docs/issue-1094/plugin-market-import-design.md §2
对齐 B1 #1084 plugin 表：name/version/display_name/description/category/author，
分类对齐 B1 种子 plugin_category（slug），object_key 规约
{PREFIX}/plugins/{scope}/{owner}/{name}/{name}-{version}.zip。

公开 seam：
- build_manifest(**fields) -> dict：填充默认值并校验，非法抛 ManifestValidationError
- validate_manifest(manifest) -> list[str]：返回错误列表（空列表 = 通过）
- first_version_warnings(manifest) -> list[str]：新插件首版非 0.0.1 的软提示
  （Issue #1795 规约：不阻断上传、不进 errors，仅建议语义）
- manifest_hash(manifest) -> str：canonical JSON sha256（对齐 plugin.manifest_hash）
"""
from __future__ import annotations

import hashlib
import json
import re

MANIFEST_SCHEMA_VERSION = "1.0"
DEFAULT_CATEGORY = "other"
FIRST_PLUGIN_VERSION = "0.0.1"  # Issue #1795 规约：新插件初始版本从 0.0.1 起

# B1 #1084 plugin_category 种子 slug（docs：src/api/db_setup.py _PLUGIN_DEFAULT_CATEGORIES）
KNOWN_CATEGORIES = (
    "dev-tools",
    "deploy-ops",
    "testing",
    "data-processing",
    "collaboration",
    "other",
)

_SOURCE_TYPES = ("manual", "mcp_registry", "openapi")
_ENTRY_KINDS = ("mcp_http", "mcp_stdio", "rest")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ManifestValidationError(ValueError):
    """manifest 校验失败；errors 为逐条错误描述。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def validate_manifest(manifest) -> list:
    """校验 manifest dict，返回错误列表（空列表 = 合法）。"""
    errors = []
    if not isinstance(manifest, dict):
        return ["manifest: 必须是 JSON 对象"]

    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"schema_version: 必须为 {MANIFEST_SCHEMA_VERSION!r}")

    name = manifest.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name or "") or len(name) > 64:
        errors.append("name: 必须匹配 ^[a-z0-9][a-z0-9._-]*$ 且长度<=64")

    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        errors.append("version: 必须为非空字符串")

    for field in ("display_name", "description", "author"):
        value = manifest.get(field)
        if value is not None and not isinstance(value, str):
            errors.append(f"{field}: 必须为字符串")

    category = manifest.get("category")
    if not isinstance(category, str) or category not in KNOWN_CATEGORIES:
        errors.append(f"category: 必须是 B1 分类 slug 之一 {list(KNOWN_CATEGORIES)}")

    source = manifest.get("source")
    if source is not None:
        if not isinstance(source, dict) or source.get("type") not in _SOURCE_TYPES:
            errors.append(f"source.type: 必须是 {_SOURCE_TYPES} 之一")
        elif not isinstance(source.get("ref", ""), str):
            errors.append("source.ref: 必须为字符串")

    auth = manifest.get("auth")
    if auth is not None:
        if not isinstance(auth, dict) or not isinstance(auth.get("required", False), bool):
            errors.append("auth: 必须是对象且 required 为布尔")

    entry = manifest.get("entry")
    if entry is not None:
        if not isinstance(entry, dict) or entry.get("kind") not in _ENTRY_KINDS:
            errors.append(f"entry.kind: 必须是 {_ENTRY_KINDS} 之一")
        else:
            kind = entry["kind"]
            if kind == "mcp_http" and not isinstance(entry.get("url"), str):
                errors.append("entry.url: kind=mcp_http 时必填")
            if kind == "rest" and not isinstance(entry.get("base_url"), str):
                errors.append("entry.base_url: kind=rest 时必填")
            if kind == "mcp_stdio" and not isinstance(entry.get("command"), str):
                errors.append("entry.command: kind=mcp_stdio 时必填")

    capabilities = manifest.get("capabilities")
    if capabilities is not None:
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools", []), list):
            errors.append("capabilities: 必须是对象且 tools 为数组")

    return errors


def build_manifest(**fields) -> dict:
    """按 schema 组装 manifest：填默认值，非法抛 ManifestValidationError。"""
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "name": fields.get("name", ""),
        "version": fields.get("version", "1.0.0"),
        "display_name": fields.get("display_name", fields.get("name", "")),
        "description": fields.get("description", ""),
        "author": fields.get("author", ""),
        "category": fields.get("category", DEFAULT_CATEGORY),
        "source": fields.get("source"),
        "auth": {"required": False, "notes": "", **(fields.get("auth") or {})},
        "entry": fields.get("entry"),
        "capabilities": {"tools": [], **(fields.get("capabilities") or {})},
    }
    # None 值字段不落盘（source/entry 可选）
    manifest = {k: v for k, v in manifest.items() if v is not None}

    errors = validate_manifest(manifest)
    if errors:
        raise ManifestValidationError(errors)
    return manifest


def first_version_warnings(manifest) -> list:
    """新插件首版规约软提示（Issue #1795）：version != 0.0.1 → 建议提示。

    - 纯建议语义：不阻断上传、不进 validate_manifest 的 errors；
    - 缺失/非法 version 属 schema 硬校验错误（400 路径），此处不重复报；
    - 存量插件版本升级可忽略（软提示，L0 静态层不判插件新旧）。
    """
    if not isinstance(manifest, dict):
        return []
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        return []
    if version.strip() == FIRST_PLUGIN_VERSION:
        return []
    return [
        f"version: 新插件初始版本建议从 {FIRST_PLUGIN_VERSION} 开始"
        f"（当前 {version.strip()}；软提示不阻断上传，存量插件升级可忽略）"
    ]


def manifest_hash(manifest: dict) -> str:
    """canonical JSON（排序键、紧凑分隔符）sha256，对齐 B1 变更检测口径。"""
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
