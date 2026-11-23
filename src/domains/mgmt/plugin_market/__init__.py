"""Grape 插件市场（plugin market）模块。

L0 校验器：插件清单（plugin.json）schema 校验 / 禁二进制扫描 / 免凭据命令探测，
作为上传/上架前的发布门禁（Issue #1079）。
"""
from src.domains.mgmt.plugin_market.l0_entry import validate_plugin_manifest as validate_plugin_manifest
from src.domains.mgmt.plugin_market.validator import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    ProbeResult,
    ValidationReport,
    ValidationError,
    check_binaries,
    check_commands,
    check_schema,
    validate,
    validate_for_publish,
    validate_manifest,
)

__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "ProbeResult",
    "ValidationReport",
    "ValidationError",
    "check_binaries",
    "check_commands",
    "check_schema",
    "validate",
    "validate_for_publish",
    "validate_manifest",
    "validate_plugin_manifest",
]
