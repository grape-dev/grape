"""plugin-tester（QA 节点）— 插件自带评测基准（Issue #1093）。

将 QA/plugin-tester 节点已验证的免凭据自检技巧固化为随插件包管理的评测基准：
- L0 校验（l0_check）：包结构/清单完整性等离线检查
- 方法名探测（method_probe）：探测插件入口暴露的预期方法名
- 401 分层（auth_401_layer）：免凭据调用受保护方法，校验鉴权错误分层
  （401/403 + 凭据缺失 missing / 凭据失效 invalid / 凭据过期 expired 可分类，而非 5xx/超时）
- 双引擎路由实测（dual_engine_route）：同一调用经双引擎路由各自完成并按需比对

基准清单随插件包分发（benchmarks/plugin.benchmark.json|yaml），本模块负责
解析与校验；执行出分见 src/plugin_tester/runner.py，门禁见 gate.py，
卡片分数数据源见 card.py。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

# ===== 四类基准用例类别（Spec 固化，随插件包管理）=====

CATEGORY_L0_CHECK = "l0_check"
CATEGORY_METHOD_PROBE = "method_probe"
CATEGORY_AUTH_401_LAYER = "auth_401_layer"
CATEGORY_DUAL_ENGINE_ROUTE = "dual_engine_route"

BENCHMARK_CATEGORIES = (
    CATEGORY_L0_CHECK,
    CATEGORY_METHOD_PROBE,
    CATEGORY_AUTH_401_LAYER,
    CATEGORY_DUAL_ENGINE_ROUTE,
)

# 401 分层名（auth_401_layer expect_layers 的合法取值，与 runner.AuthLayer 一一对应）
AUTH_LAYERS = ("missing", "invalid", "expired")

# 各类别必填字段（category/id 全局必填，此处为类别特有字段）
_CATEGORY_REQUIRED_FIELDS = {
    CATEGORY_L0_CHECK: ("check",),
    CATEGORY_METHOD_PROBE: ("expect_methods",),
    CATEGORY_AUTH_401_LAYER: ("method", "expect_layers"),
    CATEGORY_DUAL_ENGINE_ROUTE: ("method", "engines"),
}

# 默认用例权重（可被清单内 per-case weight 覆盖）
DEFAULT_CASE_WEIGHT = 1


class BenchmarkValidationError(Exception):
    """基准清单非法（结构/类别/字段不满足），message 定位到具体问题。"""


class BenchmarkCase:
    """单条基准用例（只读视图）。"""

    __slots__ = ("id", "category", "weight", "retries", "spec")

    def __init__(self, id: str, category: str, weight: float, retries: int, spec: Dict[str, Any]):
        self.id = id
        self.category = category
        self.weight = weight
        self.retries = retries
        self.spec = spec

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "BenchmarkCase(id=%r, category=%r, weight=%s)" % (self.id, self.category, self.weight)


class BenchmarkManifest:
    """插件自带评测基准清单（一个插件包一份）。"""

    __slots__ = ("plugin", "benchmark_version", "cases")

    def __init__(self, plugin: str, benchmark_version: str, cases: List[BenchmarkCase]):
        self.plugin = plugin
        self.benchmark_version = benchmark_version
        self.cases = cases

    def cases_by_category(self) -> Dict[str, List[BenchmarkCase]]:
        by_cat: Dict[str, List[BenchmarkCase]] = {}
        for case in self.cases:
            by_cat.setdefault(case.category, []).append(case)
        return by_cat


def _validate_case(case: Any, index: int) -> BenchmarkCase:
    if not isinstance(case, dict):
        raise BenchmarkValidationError("cases[%d] 不是对象（应为 mapping）" % index)
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise BenchmarkValidationError("cases[%d].id 缺失或非法（非空字符串）" % index)
    category = case.get("category")
    if category not in BENCHMARK_CATEGORIES:
        raise BenchmarkValidationError(
            "用例 %r 的 category=%r 未知（合法：%s）" % (case_id, category, "/".join(BENCHMARK_CATEGORIES))
        )
    for field in _CATEGORY_REQUIRED_FIELDS[category]:
        value = case.get(field)
        if value is None or value == "" or value == []:
            raise BenchmarkValidationError(
                "用例 %r（%s）缺少必填字段 %r" % (case_id, category, field)
            )
    if category == CATEGORY_AUTH_401_LAYER:
        unknown_layers = [l for l in case.get("expect_layers") or [] if l not in AUTH_LAYERS]
        if unknown_layers:
            raise BenchmarkValidationError(
                "用例 %r（auth_401_layer）的 expect_layers 含未知分层: %s（合法：%s）"
                % (case_id, unknown_layers, "/".join(AUTH_LAYERS))
            )
    weight = case.get("weight", DEFAULT_CASE_WEIGHT)
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
        raise BenchmarkValidationError("用例 %r 的 weight 必须为正数" % case_id)
    retries = case.get("retries", 1)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise BenchmarkValidationError("用例 %r 的 retries 必须为非负整数" % case_id)
    return BenchmarkCase(id=case_id, category=category, weight=float(weight), retries=retries, spec=case)


def _validate_data(data: Any) -> BenchmarkManifest:
    if not isinstance(data, dict):
        raise BenchmarkValidationError("基准清单顶层必须是对象（mapping）")
    plugin = data.get("plugin")
    if not isinstance(plugin, str) or not plugin.strip():
        raise BenchmarkValidationError("plugin 字段缺失或非法（非空字符串）")
    benchmark_version = str(data.get("benchmark_version") or "1.0")
    cases_raw = data.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise BenchmarkValidationError("cases 必须为非空数组")

    cases = [_validate_case(c, i) for i, c in enumerate(cases_raw)]

    # Spec 验收：基准须覆盖四类用例，缺一即不构成插件自带评测基准
    present = {c.category for c in cases}
    missing = [c for c in BENCHMARK_CATEGORIES if c not in present]
    if missing:
        raise BenchmarkValidationError(
            "基准缺少必备类别用例：%s（四类必备：L0 校验/方法名探测/401 分层/双引擎路由）"
            % ", ".join(missing)
        )
    return BenchmarkManifest(plugin=plugin, benchmark_version=benchmark_version, cases=cases)


def _load_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise BenchmarkValidationError("基准清单读取失败 %s: %s" % (path, exc))


def _parse_text(text: str, path: str) -> Any:
    lowered = path.lower()
    if lowered.endswith(".yaml") or lowered.endswith(".yml"):
        try:
            import yaml  # pyyaml 为服务端既有依赖
        except ImportError:
            raise BenchmarkValidationError("解析 yaml 基准清单需要 pyyaml（未安装）")
        try:
            return yaml.safe_load(text)
        except Exception as exc:
            raise BenchmarkValidationError("yaml 解析失败 %s: %s" % (path, exc))
    # 默认按 JSON（.json 及无后缀）
    try:
        return json.loads(text)
    except ValueError as exc:
        raise BenchmarkValidationError("JSON 解析失败 %s: %s" % (path, exc))


def parse_benchmark(data: Any) -> BenchmarkManifest:
    """解析并校验基准清单 dict（插件包内嵌场景）。"""
    return _validate_data(data)


def load_benchmark(source: Any) -> BenchmarkManifest:
    """加载插件自带评测基准。

    - dict：直接校验解析
    - 路径字符串：按后缀解析（.yaml/.yml → yaml；其余 → JSON）
    """
    if isinstance(source, dict):
        return _validate_data(source)
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        return _validate_data(_parse_text(_load_text(path), path))
    raise BenchmarkValidationError("不支持的基准来源类型: %r（应为 dict 或路径）" % (source,))
