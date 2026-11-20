"""插件 L0 校验适配层（Issue #1084，总纲 #1078 B1 → 上游 A1 #1079）。

职责边界：本模块**只做接入调用**，不实现 manifest schema / 禁二进制 / 命令探测等
L0 校验器本体（属 A1 #1079）。适配层在运行时探测 A1 校验器模块：

- A1 已合入（模块可导入且暴露 ``validate_plugin_manifest(manifest, file_names)
  -> (bool, errors)``）：委托 A1，``validator="a1"``、``degraded=False``；
- A1 未合入：回退**内置最小校验**（仅 manifest 结构 + 必填字段 name/version，
  非法不入库），``validator="builtin"``、``degraded=True``——降级语义：上传可用，
  完整 L0 保证待 A1 交付。

A1 模块路径由环境变量 ``PLUGIN_L0_VALIDATOR_MODULE`` 指定（默认
``src.domains.mgmt.plugin_market.l0_entry``，Issue #1502 项2：A1 组合入口已合入
``src/plugin_market/``，默认即接入完整 L0；如需停用可显式置空该环境变量）。
"""
import importlib
import os

from src.kernel.config import log

# A1 校验器模块路径（环境变量可覆盖；路径导入失败 → 走内置降级）
# Issue #1502 项2：默认指向 A1 实际组合入口（原 src.plugin_manifest.l0 不存在 → 恒降级）
A1_VALIDATOR_MODULE_ENV = "PLUGIN_L0_VALIDATOR_MODULE"
A1_VALIDATOR_MODULE_DEFAULT = "src.domains.mgmt.plugin_market.l0_entry"

# 内置降级校验：manifest 必填字段（完整 schema 属 A1）
_BUILTIN_REQUIRED_FIELDS = ("name", "version")

_cache_a1 = None


def _load_a1_validator():
    """探测 A1 校验器模块。返回 (module_name, module) 或 (None, None)。

    探测结果缓存（模块导入稳定）；测试可 monkeypatch 本函数注入伪模块。
    """
    global _cache_a1
    if _cache_a1 is not None:
        return _cache_a1
    module_path = (os.environ.get(A1_VALIDATOR_MODULE_ENV)
                   or A1_VALIDATOR_MODULE_DEFAULT).strip()
    if not module_path:
        return (None, None)
    try:
        mod = importlib.import_module(module_path)
        fn = getattr(mod, "validate_plugin_manifest", None)
        if not callable(fn):
            log.warning(f"插件 L0 适配层：模块 {module_path} 未暴露 validate_plugin_manifest，"
                        f"视为 A1 未合入（走内置降级校验）")
            _cache_a1 = (None, None)
            return _cache_a1
        _cache_a1 = (module_path, mod)
        log.info(f"插件 L0 适配层：已接入 A1 校验器（module={module_path}）")
        return _cache_a1
    except Exception:
        # A1 未合入属预期常态（导入失败静默降级，不打扰日志）
        _cache_a1 = (None, None)
        return _cache_a1


def _builtin_validate(manifest, file_names) -> tuple[bool, list[str]]:
    """内置最小校验（降级模式）：manifest 为 dict + 必填字段 name/version 非空。

    仅保证"非法 manifest 不入库"的最小门槛；完整 L0（schema 校验/禁二进制/
    命令探测）属 A1 #1079，不在本任务范围。
    """
    errors = []
    if not isinstance(manifest, dict):
        return (False, ["manifest.json 内容必须是 JSON 对象"])
    for field in _BUILTIN_REQUIRED_FIELDS:
        val = manifest.get(field)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"manifest 缺少必填字段 {field}（或类型非字符串）")
    return (not errors, errors)


def _a1_warnings(mod, manifest) -> list:
    """A1 软提示能力探测（Issue #1795，可选）：模块暴露 plugin_warnings 则委托。

    旧版 A1 契约（仅 validate_plugin_manifest）无该函数 → 恒空列表，不影响校验；
    执行异常也只降级为空提示（软提示不承担阻断语义）。
    """
    fn = getattr(mod, "plugin_warnings", None)
    if not callable(fn):
        return []
    try:
        return list(fn(manifest) or [])
    except Exception as e:
        log.warning(f"插件 L0 适配层：软提示计算异常（忽略）: {e}")
        return []


def validate_plugin_l0(manifest: dict, file_names: list[str]) -> dict:
    """插件上传 L0 校验统一入口（上传时同步校验，非法不入库）。

    返回 {"ok": bool, "errors": [..], "l0_warnings": [..],
          "validator": "a1"|"builtin", "degraded": bool}。
    l0_warnings 为软提示（Issue #1795 新插件首版 0.0.1 规约建议），
    不影响 ok/errors（不阻断上传）。
    """
    module_name, mod = _load_a1_validator()
    if mod is not None:
        try:
            ok, errors = mod.validate_plugin_manifest(manifest, file_names)
            errors = list(errors or [])
            return {"ok": bool(ok) and not errors, "errors": errors,
                    "l0_warnings": _a1_warnings(mod, manifest),
                    "validator": "a1", "degraded": False}
        except Exception as e:
            # A1 校验器运行时异常：fail-closed（拒绝入库）并标注 A1 错误，
            # 防止适配层异常被误当成"校验通过"
            log.warning(f"插件 L0 适配层：A1 校验器执行异常（fail-closed）: {e}")
            return {"ok": False, "errors": [f"L0 校验器执行异常: {e}"],
                    "l0_warnings": [], "validator": "a1", "degraded": False}
    ok, errors = _builtin_validate(manifest, file_names)
    return {"ok": ok, "errors": errors, "l0_warnings": [],
            "validator": "builtin", "degraded": True}


def l0_degraded_hint() -> str:
    """降级提示文案（A1 未合入时随上传响应返回，明确告知完整 L0 未生效）。"""
    module_path = (os.environ.get(A1_VALIDATOR_MODULE_ENV)
                   or A1_VALIDATOR_MODULE_DEFAULT).strip()
    return (f"L0 校验器（A1 #{1079}）未接入，当前仅启用内置最小校验"
            f"（manifest 结构 + 必填字段）；完整校验待配置 {module_path} 后自动生效")
