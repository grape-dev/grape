"""plugin-tester 回归执行出分（Issue #1093；L0 统一入口 Issue #1796）。

执行插件自带评测基准（benchmark.BenchmarkManifest），通过传输 seam
PluginClient 与被测插件交互（不触平台内部），产出量化分数（0-100）：

score = round(100 * Σ(通过用例权重) / Σ(用例权重), 1)

四类执行语义（与 QA/plugin-tester 节点已验证的免凭据自检技巧一致）：
- l0_check：经统一入口 ``validate_plugin_l0(manifest, file_names)``（Issue
  #1796：与市场上传 ``POST /api/plugins/upload`` 同源，src/api/plugin_l0.py
  适配层）离线校验——输入口径同上传（manifest.json + 包内 file_names，取自
  plugin_context 的 manifest/file_names 字段）；本工程不自建第二套 L0 规则，
  判定/错误项/validator/degraded 与上传逐条一致；builtin 降级时结论只标注
  「仅内置最小校验」，不输出「L0 全过」
- method_probe：list_methods() 后断言预期方法名全部暴露（多余方法不扣分）
- auth_401_layer：免凭据调用受保护方法，断言返回可分类的鉴权错误分层
  （401/403 + missing/invalid/expired 签名），未分类（5xx/超时/乱码）判失败；
  retries 容错外部抖动（重试次数内出现任一命中分层即通过）
- dual_engine_route：同一调用在声明引擎上各自完成（2xx），任一失败判失败
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from src.domains.mgmt.plugin_tester.benchmark import (
    CATEGORY_AUTH_401_LAYER,
    CATEGORY_DUAL_ENGINE_ROUTE,
    CATEGORY_L0_CHECK,
    CATEGORY_METHOD_PROBE,
    BenchmarkCase,
    BenchmarkManifest,
)

# ===== 401 分层签名（免凭据自检 demo 已验证形态，子串匹配、大小写不敏感）=====

_AUTH_LAYER_SIGNATURES = {
    "missing": ("missing", "absent", "not provided", "api key required", "key required"),
    "invalid": ("invalid", "unauthorized", "forbidden", "bad key", "wrong key"),
    "expired": ("expired", "out of date", "stale key"),
}

# L0 检查项（check 字段合法取值；未知检查项判失败，不可假通过）。
# Issue #1796：manifest_integrity 的实现 = 统一入口 validate_plugin_l0
# （src/api/plugin_l0.py，与市场上传同源）；本模块不自建 L0 规则。
L0_CHECK_MANIFEST_INTEGRITY = "manifest_integrity"
_L0_SUPPORTED_CHECKS = (L0_CHECK_MANIFEST_INTEGRITY,)


class RunError(Exception):
    """基准执行期传输/环境异常（单用例失败不中断整体回归）。"""


class AuthLayer:
    MISSING = "missing"
    INVALID = "invalid"
    EXPIRED = "expired"


def classify_auth_error(status: Any, body: Any) -> Optional[str]:
    """将免凭据调用的响应分类为鉴权错误分层；不可分类返回 None。

    分层口径（401 分层用例；401/403 均为鉴权拒绝语义——部分插件以
    403 Forbidden 表示凭据无效，与 invalid 签名表对应）：
    - 401/403 + 缺失签名 → missing（凭据缺失）
    - 401/403 + 失效/拒绝签名 → invalid（含 403 Forbidden）
    - 401/403 + 过期签名 → expired
    - 其他状态（5xx/超时/乱码 body）→ None（鉴权分层缺失，判失败）
    """
    if status not in (401, 403):
        return None
    text = ""
    if isinstance(body, str):
        text = body
    elif isinstance(body, dict):
        text = " ".join(str(v) for v in body.values() if isinstance(v, (str, int, float)))
    if not text:
        return None
    lowered = text.lower()
    for layer in (AuthLayer.EXPIRED, AuthLayer.MISSING, AuthLayer.INVALID):
        if any(sig in lowered for sig in _AUTH_LAYER_SIGNATURES[layer]):
            return layer
    return None


class PluginClient(Protocol):
    """被测插件传输 seam（QA 节点按插件运行时适配，本模块只依赖此协议）。"""

    def list_methods(self) -> List[str]: ...

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None, engine: Optional[str] = None) -> Any:
        """调用插件方法；engine=None 为主路由，双引擎用例按声明引擎逐个调用。

        返回 (status, body) 二元组；异常由实现方抛出（含网络/超时）。
        """
        ...


class CaseResult:
    """单条用例执行结果。"""

    __slots__ = ("case_id", "category", "passed", "detail", "weight", "attempts")

    def __init__(self, case_id: str, category: str, passed: bool, detail: str, weight: float, attempts: int = 1):
        self.case_id = case_id
        self.category = category
        self.passed = passed
        self.detail = detail
        self.weight = weight
        self.attempts = attempts

    def as_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "detail": self.detail,
            "weight": self.weight,
            "attempts": self.attempts,
        }


class BenchmarkReport:
    """一次回归的报告：分数 + per-case 结果 + L0 统一入口结果（#1796）。"""

    __slots__ = ("plugin", "plugin_version", "benchmark_version", "trigger", "score", "results",
                 "l0_ok", "l0_errors", "l0_validator", "l0_degraded")

    def __init__(self, plugin: str, benchmark_version: str, trigger: str, score: float, results: List[CaseResult],
                 plugin_version: str = "", l0_ok: Optional[bool] = None, l0_errors: Optional[List[str]] = None,
                 l0_validator: Optional[str] = None, l0_degraded: Optional[bool] = None):
        self.plugin = plugin
        self.plugin_version = plugin_version
        self.benchmark_version = benchmark_version
        self.trigger = trigger
        self.score = score
        self.results = results
        self.l0_ok = l0_ok
        self.l0_errors = list(l0_errors or [])
        self.l0_validator = l0_validator
        self.l0_degraded = l0_degraded

    def category_stats(self) -> Dict[str, tuple]:
        """{category: (passed, total)} 统计。"""
        stats: Dict[str, List[int]] = {}
        for r in self.results:
            passed, total = stats.get(r.category, (0, 0))
            stats[r.category] = [passed + (1 if r.passed else 0), total + 1]
        return {cat: (v[0], v[1]) for cat, v in stats.items()}

    def failed_cases(self) -> List[CaseResult]:
        return [r for r in self.results if not r.passed]

    def as_dict(self) -> Dict[str, Any]:
        """报告序列化：含 L0 统一入口结果（validator/degraded，#1796）。

        builtin 降级（l0_degraded=True）时附 ``l0_degraded_hint``（与上传响应
        同源文案），明确「仅内置最小校验，完整 L0 未生效」。
        """
        report = {
            "plugin": self.plugin,
            "plugin_version": self.plugin_version,
            "benchmark_version": self.benchmark_version,
            "trigger": self.trigger,
            "score": self.score,
            "results": [r.as_dict() for r in self.results],
            "l0_ok": self.l0_ok,
            "l0_errors": list(self.l0_errors),
            "l0_validator": self.l0_validator,
            "l0_degraded": self.l0_degraded,
        }
        if self.l0_degraded:
            from src.api.plugin_l0 import l0_degraded_hint
            report["l0_degraded_hint"] = l0_degraded_hint()
        return report


# ===== 执行器 =====


def _execute_l0_check(case: BenchmarkCase, plugin_context: Dict[str, Any]) -> tuple:
    """L0 用例执行：统一入口 ``validate_plugin_l0``（与市场上传同源，#1796）。

    - 输入口径同上传：plugin_context.manifest（manifest.json 内容）+
      plugin_context.file_names（包内文件名清单）；缺失时 fail-closed 判失败
      （不假通过），不旁路统一入口。
    - 返回 ``(CaseResult, l0_result)``：l0_result 为统一入口原始返回
      （ok/errors/validator/degraded），供报告透出与上传侧逐条对账。
    """
    check = case.spec.get("check")
    if check not in _L0_SUPPORTED_CHECKS:  # 校验层只保证 check 非空，未知取值在此判失败（不可假通过）
        return CaseResult(
            case.id, case.category, False,
            "未支持的 L0 检查项: %r（当前支持：%s）" % (check, ", ".join(_L0_SUPPORTED_CHECKS)),
            case.weight,
        ), None
    manifest = plugin_context.get("manifest")
    file_names = plugin_context.get("file_names")
    if not isinstance(manifest, dict) or not isinstance(file_names, list):
        detail = ("插件上下文缺少统一 L0 入口输入（manifest/file_names，"
                  "口径同上传：manifest.json + 包内 file_names）")
        return CaseResult(case.id, case.category, False, detail, case.weight), {
            "ok": False, "errors": [detail], "validator": None, "degraded": None,
        }
    from src.api import plugin_l0  # 统一入口（与 POST /api/plugins/upload 同源）
    l0 = plugin_l0.validate_plugin_l0(manifest, list(file_names))
    if l0["ok"]:
        if l0["degraded"]:
            # builtin 降级：判定与上传一致（通过），但只标注最小校验，不得输出「L0 全过」
            detail = "仅内置最小校验通过（validator=builtin，degraded：完整 L0 未生效）"
        else:
            detail = "L0 校验通过（validator=%s）" % l0["validator"]
    else:
        detail = "L0 校验失败（validator=%s）：%s" % (l0["validator"], "; ".join(l0["errors"]))
    return CaseResult(case.id, case.category, bool(l0["ok"]), detail, case.weight), l0


def _run_method_probe(case: BenchmarkCase, client: PluginClient) -> CaseResult:
    try:
        exposed = set(client.list_methods())
    except Exception as exc:
        return CaseResult(case.id, case.category, False, "list_methods 异常: %s" % exc, case.weight)
    expect = [m for m in case.spec.get("expect_methods") or []]
    absent = [m for m in expect if m not in exposed]
    if absent:
        return CaseResult(
            case.id, case.category, False,
            "入口未暴露预期方法: %s（实际: %s）" % (", ".join(absent), sorted(exposed)),
            case.weight,
        )
    return CaseResult(case.id, case.category, True, "方法名探测通过（%d/%d）" % (len(expect), len(expect)), case.weight)


def _run_auth_401_layer(case: BenchmarkCase, client: PluginClient) -> CaseResult:
    method = case.spec["method"]
    payload = case.spec.get("payload")
    expect_layers = set(case.spec["expect_layers"])  # 必填字段（校验层保证），不做静默默认
    max_attempts = case.retries + 1
    seen: List[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            status, body = client.call(method, payload)
        except Exception as exc:
            seen.append("transport:%s" % exc)
            continue
        layer = classify_auth_error(status, body)
        if layer is None:
            seen.append("%s:unclassified" % status)
            continue
        if layer in expect_layers:
            return CaseResult(case.id, case.category, True, str(layer), case.weight, attempts=attempt)
        seen.append("%s:%s" % (status, layer))
    detail = "鉴权分层未命中（期望 %s）；观察: %s" % (sorted(expect_layers), "; ".join(seen) or "无响应")
    return CaseResult(case.id, case.category, False, detail, case.weight)


def _run_dual_engine_route(case: BenchmarkCase, client: PluginClient) -> CaseResult:
    method = case.spec["method"]
    payload = case.spec.get("payload")
    engines = list(case.spec.get("engines") or [])
    for engine in engines:
        last: Any = None
        for _attempt in range(case.retries + 1):
            try:
                status, body = client.call(method, payload, engine=engine)
            except Exception as exc:
                last = "transport:%s" % exc
                continue
            if isinstance(status, int) and 200 <= status < 300:  # 2xx 即路由完成（README 契约）
                last = None
                break
            last = "%s:%s" % (status, body)
        if last is not None:
            return CaseResult(
                case.id, case.category, False,
                "引擎 %r 路由未完成: %s" % (engine, last), case.weight,
            )
    return CaseResult(case.id, case.category, True, "双引擎路由完成（%s）" % ", ".join(engines), case.weight)


_CASE_EXECUTORS = {
    CATEGORY_METHOD_PROBE: _run_method_probe,
    CATEGORY_AUTH_401_LAYER: _run_auth_401_layer,
    CATEGORY_DUAL_ENGINE_ROUTE: _run_dual_engine_route,
}


def run_benchmark(
    manifest: BenchmarkManifest,
    client: PluginClient,
    trigger: str = "",
    plugin_context: Optional[Dict[str, Any]] = None,
) -> BenchmarkReport:
    """执行基准 → 量化分数报告（0-100，按权重）。单用例失败不中断整体回归。

    trigger 必填（回归触发点 core_update/release，见 gate.REGRESSION_TRIGGERS）：
    缺省/空串直接 ValueError，不静默缺省——报告自带触发点，与门禁判定一致可审计。

    plugin_context 为 QA 侧被测插件上下文：L0 用例消费 manifest（manifest.json
    内容）与 file_names（包内文件名清单，口径同上传 POST /api/plugins/upload），
    version 用于报告 plugin_version 字段。
    """
    if not isinstance(trigger, str) or not trigger.strip():
        raise ValueError(
            "回归触发点 trigger 必填（core_update/release），不可静默缺省（触发点需可审计）"
        )
    context = plugin_context or {}
    results: List[CaseResult] = []
    l0_result = None
    for case in manifest.cases:
        if case.category == CATEGORY_L0_CHECK:
            # L0 用例走统一入口执行器（返回用例结果 + 统一入口原始返回）
            case_result, raw_l0 = _execute_l0_check(case, context)
            if l0_result is None:
                l0_result = raw_l0
            results.append(case_result)
            continue
        executor = _CASE_EXECUTORS.get(case.category)
        if executor is None:  # pragma: no cover - 校验层已拦截未知类别
            results.append(CaseResult(case.id, case.category, False, "无执行器", case.weight))
            continue
        results.append(executor(case, client))

    total_weight = sum(r.weight for r in results) or 1.0
    passed_weight = sum(r.weight for r in results if r.passed)
    score = round(100.0 * passed_weight / total_weight, 1)
    l0 = l0_result or {}
    return BenchmarkReport(
        plugin=manifest.plugin,
        benchmark_version=manifest.benchmark_version,
        trigger=trigger,
        score=score,
        results=results,
        plugin_version=str(context.get("version") or ""),
        l0_ok=l0.get("ok"),
        l0_errors=l0.get("errors") or [],
        l0_validator=l0.get("validator"),
        l0_degraded=l0.get("degraded"),
    )
