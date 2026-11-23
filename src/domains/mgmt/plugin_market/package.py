"""插件包门禁自检与打包（Issue #1094，总纲 #1078 D3）。

设计依据：docs/issue-1094/plugin-market-import-design.md §7
zip 打包形态对齐 B1 #1084 object_key 规约（{name}-{version}.zip，manifest.json 在包根）。
自检范围（tester 前置门禁，替代不了 plugin-tester 契约校验）：
1. manifest.json 存在且经 schema 校验合法
2. wrapper/*.py 生成代码可解析（ast）
verify_package 面向内存包（PluginPackage duck-type），verify_package_dir 面向物料目录。
"""
from __future__ import annotations

import ast
import io
import json
import zipfile
from pathlib import Path

from src.domains.mgmt.plugin_market.manifest import validate_manifest

REQUIRED_FILES = ("manifest.json",)
_REST_ENTRY_REQUIRED = ("openapi.json", "CAPABILITY.md", "wrapper/rest_client.py")


def _iter_package_files(root: Path):
    """遍历物料目录文件，产出 (相对 posix 路径, Path)，跳过 __pycache__（排序保证确定性）。"""
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path.relative_to(root).as_posix(), path


def verify_package(package) -> list:
    """校验包（须含 .manifest dict 与 .files {相对路径: 文本}），返回错误列表。"""
    errors = []
    files = getattr(package, "files", {}) or {}

    if "manifest.json" not in files:
        errors.append("manifest.json: 缺失（包必备文件）")
    else:
        try:
            manifest = json.loads(files["manifest.json"])
        except (ValueError, TypeError):
            errors.append("manifest.json: 非 JSON 文档")
        else:
            errors.extend(validate_manifest(manifest))

    entry = (getattr(package, "manifest", None) or {}).get("entry") or {}
    if entry.get("kind") == "rest":
        for rel in _REST_ENTRY_REQUIRED:
            if rel not in files:
                errors.append(f"{rel}: kind=rest 包必备文件缺失")

    for rel, content in files.items():
        if rel.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as exc:
                errors.append(f"{rel}: Python 语法错误 {exc.lineno}:{exc.offset}")
    return errors


def verify_package_dir(dir_path: str) -> list:
    """校验落盘物料目录（等价 verify_package 的目录形态）。"""
    root = Path(dir_path)
    files = {}
    for rel, path in _iter_package_files(root):
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            files[rel] = ""
    manifest = {}
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, TypeError):
            pass

    class _DirPackage:
        pass

    pkg = _DirPackage()
    pkg.manifest = manifest
    pkg.files = files
    return verify_package(pkg)


def pack_directory(dir_path: str) -> bytes:
    """把物料目录打包为 zip 字节流（包根对齐 B1 object_key 形态）。"""
    root = Path(dir_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, path in _iter_package_files(root):
            zf.write(path, rel)
    return buf.getvalue()
