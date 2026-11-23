"""插件上传 L0 组合入口（Issue #1502 项2，总纲 #1078 A1 × #1084 B1 交汇）。

适配层（``src/api/plugin_l0.py``）期望的
``validate_plugin_manifest(manifest, file_names) -> (bool, errors)`` 三合一入口，
面向上传端点的 B1 市场包（manifest.json，#1094 schema）串联：

① manifest schema 字段级校验 —— ``src.plugin_market.manifest.validate_manifest``
   （B1 manifest.json 字段：schema_version/name/version/category/entry/...，
   错误定位到字段名）；
② 禁二进制 —— 按包内 file_names 后缀黑名单判定（对齐 ``validator.BINARY_SUFFIXES``；
   纯名称判定不触盘，适配 ``(manifest, file_names)`` 签名），并附带命令路径
   白名单核验与 bundled_skills 路径安全复核（复用 validator 静态检查）；
③ 命令免凭据静态探测 —— 上传语义对齐 A1 ``validate_for_publish(probe=False)``：
   只做静态核验（entry.command 路径穿越/entry.url scheme 白名单），不执行子进程、
   不发起网络请求；真实方法名探测（401 分层）属上架前发布门禁，不在上传期执行。

返回 ``(ok, errors)``：errors 为字符串列表（字段级错误，空列表 = 通过）；
warning 级发现不阻断上传、不进入 errors。

软提示 seam（Issue #1795）：``plugin_warnings(manifest) -> list[str]`` ——
新插件首版非 0.0.1 的建议提示（硬校验通过后才输出，不阻断、不进 errors）。
"""
from __future__ import annotations

from src.domains.mgmt.plugin_market import manifest as manifest_mod
from src.domains.mgmt.plugin_market import validator as validator_mod


def _fmt(findings) -> list:
    """ValidationError 列表 → 字段级错误字符串列表（path: reason（hint））。"""
    return [f"{e.path}: {e.reason}（{e.hint}）" for e in findings]


def _binary_file_errors(file_names) -> list:
    """② 禁二进制：按 file_names 后缀黑名单判定（纯名称，不触盘）。"""
    errors = []
    for rel in file_names or []:
        name = str(rel).replace("\\", "/").lstrip("/")
        lower = name.lower()
        for suffix in validator_mod.BINARY_SUFFIXES:
            if lower.endswith(suffix):
                errors.append(
                    f"{name}: 检测到二进制产物（后缀 {suffix}）——"
                    f"插件包仅允许文本产物（脚本/manifest/skills），"
                    f"请移除二进制文件并在安装期由用户环境提供可执行依赖")
                break
    return errors


def _entry_probe_static_errors(manifest) -> list:
    """③ 命令免凭据静态探测（上传语义）：entry 命令/端点静态核验。

    - mcp_stdio：command 必填 + 禁路径分隔符/穿越（会在用户机执行，包内不得内嵌路径）；
    - rest / mcp_http / mcp_remote：url/base_url 仅允许 http/https scheme。
    不发起任何网络/子进程操作（真实探测属上架门禁，见模块 docstring）。
    """
    if not isinstance(manifest, dict):
        return []
    entry = manifest.get("entry")
    if not isinstance(entry, dict):
        return []
    errors = []
    kind = entry.get("kind")
    command = entry.get("command")
    if kind == "mcp_stdio" or (command and kind in (None, "mcp_stdio")):
        if not isinstance(command, str) or not command.strip():
            errors.append("entry.command: kind=mcp_stdio 时必填")
        elif "/" in command or "\\" in command or ".." in command:
            errors.append(
                f"entry.command: 不允许路径分隔符/路径穿越: {command}"
                f"（command 只允许可执行文件名，如 demo-weather）")
    url = entry.get("url") or entry.get("base_url")
    if isinstance(url, str) and url.strip():
        scheme = url.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            errors.append(
                f"entry.url: 仅支持 http/https 端点（实际 scheme: {scheme or '缺失'}）")
    return errors


def _schema_errors(manifest) -> list:
    """① schema 字段级校验（B1 manifest schema），容忍 @owner 命名空间名。

    B1 #1085 上传契约允许 manifest.name 为 ``@owner/name``（归属校验在
    plugin_security.ensure_namespace_ownership，上传路由 L0 之后执行）；
    manifest.validate_manifest 的裸名正则不识别该形态 → 校验前按
    parse_plugin_name 归一为裸名（仅用于 schema 判定，不改写原 manifest）。
    """
    schema_manifest = manifest if isinstance(manifest, dict) else {}
    name = schema_manifest.get("name") if isinstance(schema_manifest, dict) else None
    if isinstance(name, str) and name.startswith("@"):
        try:
            from src.api.plugin_security import parse_plugin_name
            _owner, bare = parse_plugin_name(name)
            if bare:
                schema_manifest = dict(schema_manifest, name=bare)
        except Exception:
            pass
    return list(manifest_mod.validate_manifest(schema_manifest))


def validate_plugin_manifest(manifest, file_names) -> tuple:
    """上传 L0 三合一组合入口（适配层接入点）。

    ① manifest schema 字段级校验 + ② 禁二进制（file_names 后缀黑名单）
    + 命令路径/捆绑技能路径静态核验 + ③ 命令免凭据静态探测。
    返回 (ok, errors)：errors 为字段级错误字符串列表，空列表 = 通过。
    """
    errors = _schema_errors(manifest)
    errors += _binary_file_errors(file_names)
    safe_manifest = manifest if isinstance(manifest, dict) else {}
    errors += _fmt(validator_mod._check_command_paths(safe_manifest))
    errors += _fmt(validator_mod._check_bundled_skill_paths(safe_manifest))
    errors += _entry_probe_static_errors(manifest)
    return (not errors, errors)


def plugin_warnings(manifest) -> list:
    """L0 软提示（Issue #1795）：新插件首版非 0.0.1 的建议提示（不阻断上传）。

    在 schema/category 硬校验通过后才输出（硬错误路径 400 已拒，提示无意义）；
    语义与文案见 manifest.first_version_warnings。
    """
    if _schema_errors(manifest):
        return []
    return list(manifest_mod.first_version_warnings(manifest))
