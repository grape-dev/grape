#!/usr/bin/env python3
"""CI 契约端点完整性校验（Issue #897，对齐 grape-skills#187 思路）。

防两类回归：
1. 核心工作流端点丢失——当前代码渲染出的 OpenAPI 契约必须包含
   CORE_WORKFLOW_ENDPOINTS 全部端点（/api/a2a、/api/tasks/complete 等，
   Issue #897 实测 v2.2.0 曾被 A 进程子集落盘覆盖丢失）；
2. 版本间端点缩水——发布构建时校验 v2.x 契约不得删除 v2.(x-1) 基线中
   核心工作流前缀下的任何端点（子集覆盖/漂移再次发生时 CI 直接失败）。
   基线 = 仓库内 checked-in 快照 help/api/openapi-baseline.json（上次发布的
   正式契约；注意 help/api/openapi.json 是启动期生成物且被 .gitignore 忽略，
   不能作 CI 基线——否则版本间比对在 CI 永远静默跳过、形同虚设）。

用法（本地 / CI 通用，不启动服务、不落盘正式契约）：
    python3 scripts/check-openapi-endpoints.py [--baseline PATH]
                                               [--require-baseline]
                                               [--update-baseline]

--require-baseline：基线缺失/不可读时直接 FAIL（CI 必用，杜绝静默跳过版本间比对）。
--update-baseline：全部校验通过后，把当前渲染契约写回基线文件（发布构建时刷新基线用）。
--change-note：基线刷新的评审说明（写入基线文件留痕；不是观察期放行口）。
Issue #2837 删侧观察期：端点删除另须先挂牌（contracts/deprecated_endpoints.json）并经
一个发布期（基准 contracts/released/ 冻结快照 release_index），--change-note 自由文本
说明不能绕过观察期；机制生效前的历史下架走 contracts/deprecated_exemptions.json
（只减不增）。
退出码：0 通过；1 校验失败。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domains.contracts import deprecation as contract_gate  # Issue #2837 删侧观察期共用核

# 7 个核心工作流端点：全部 Agent 技能的 A2A 交办与 tasks 结构化收尾依赖
# （丢失即 `grape a2a handoff` / `grape tasks complete` 不渲染，P0 全平台断链）。
CORE_WORKFLOW_ENDPOINTS = [
    "/api/a2a",
    "/api/a2a/status",
    "/api/tasks/complete",
    "/api/tasks/current",
    "/api/tasks/notify",
    "/api/runtime/tasks/{task_id}/status",
    "/api/runtime/tasks/{task_id}/cancel",
]

# 版本间比对只看核心工作流前缀：其余业务端点的增删属正常演进，不做强约束。
CORE_WORKFLOW_PREFIXES = (
    "/api/a2a",
    "/api/tasks",
    "/api/runtime/tasks",
    # I-1（架构守护）：IM 通道端点（installations / outbound / retry / status）此前不在
    # 守护面内，四进程分流把它挂错版本时基线比对拦不住（#1363 C-3 正是这样漏掉的）。
    "/api/im",
)

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.|$)")

# 库内契约副本（`/static/openapi.json` 的实际下载源）。提成常量是为了让单测能把
# 它指到临时文件：注入「当前契约」的用例（如只给 7 个核心端点）不该被真实副本人为判定为漂移。
CONTRACT_COPY = os.path.join("src", "api", "frontend", "shell", "openapi.json")


def parse_version(version: str):
    """解析 '2.2.0' / 'v2.2' -> (major, minor)；不可解析返回 None。"""
    if not isinstance(version, str):
        return None
    m = _VERSION_RE.match(version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def check_core_endpoints(paths: dict) -> list:
    """校验核心工作流端点齐全，返回错误列表。"""
    errors = []
    for ep in CORE_WORKFLOW_ENDPOINTS:
        if ep not in paths:
            errors.append(f"core workflow endpoint missing: {ep}")
    return errors


def check_operation_id_unique(paths: dict) -> list:
    """校验 operationId 全局唯一（OpenAPI 3 要求）。

    重复 operationId 会让按 operationId 反查生成的 CLI / 契约消费方静默取到
    另一份实现（Issue: PR #1329 架构守护 CRITICAL-01 —— 同一 method+path 注册
    两次，第二份为死代码但契约里出现重复 operationId）。本校验补上这道 guard。
    """
    seen = {}
    errors = []
    for path, ops in (paths or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            oid = (op or {}).get("operationId") if isinstance(op, dict) else None
            if not oid:
                continue
            if oid in seen:
                errors.append(f"duplicate operationId '{oid}' at {seen[oid]} and {method.upper()} {path}")
            else:
                seen[oid] = f"{method.upper()} {path}"
    return errors


def check_route_registration_unique(app_routes) -> list:
    """校验同一 method+path 未被重复注册。

    渲染后的契约里 `paths` 是字典，同一 method+path 注册两次会被折叠成一份，
    因此 check_operation_id_unique 抓不到这类重复（FastAPI 只在启动时打
    UserWarning）。这里直接扫 app.routes，把「同一路由块粘两次」的回归挡在 CI。
    HEAD/OPTIONS 是框架自动补的方法，跳过以免噪声。
    """
    seen = {}
    errors = []
    for r in app_routes or []:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if not path or not methods:
            continue
        # 只看契约面：SPA 静态兜底（/{path:path}、/account 等）为既有设计；
        # include_in_schema=False 的路由（如 /api/ping 双注册）不进 OpenAPI 契约，
        # 不属本 guard 的守护对象。
        if not str(path).startswith("/api/"):
            continue
        if getattr(r, "include_in_schema", True) is False:
            continue
        for m in methods:
            m = str(m).upper()
            if m in ("HEAD", "OPTIONS"):
                continue
            key = (m, path)
            if key in seen:
                errors.append(
                    f"duplicate route registration: {m} {path} "
                    f"(registered by {type(r).__name__} after {seen[key]})")
            else:
                seen[key] = type(r).__name__
    return errors


def core_workflow_paths(paths: dict) -> set:
    """过滤出核心工作流前缀下的端点集合。"""
    return {p for p in paths if p.startswith(CORE_WORKFLOW_PREFIXES)}


def check_baseline_subset(current_paths: dict, baseline_paths: dict,
                          current_version: str, baseline_version: str) -> list:
    """版本间校验：基线核心工作流端点不得在新契约中消失。

    仅当 current 版本 >= baseline 版本时执行（降级/重放场景跳过），
    对齐 Spec 的「v2.x vs v2.(x-1) 核心工作流端点不得删除」。
    """
    cur = parse_version(current_version)
    base = parse_version(baseline_version)
    if cur is not None and base is not None and cur < base:
        return ["baseline version newer than current: "
                f"baseline={baseline_version} current={current_version}"]
    removed = sorted(core_workflow_paths(baseline_paths) - set(current_paths))
    if removed:
        return [f"core workflow endpoint(s) removed vs baseline: {', '.join(removed)}"]
    return []


def run_deprecation_gate(current_paths: dict, require_released: bool) -> list:
    """删侧观察期门禁（Issue #2837，等价 #2783）：基准 = release_index 最大的已发布快照。

    判定：快照有、当前契约无的端点，须 ① 已挂牌且经一个发布期（快照带机器可读
    x-deprecated-since 标记），或 ② 在存量豁免白名单（pre_mechanism，只减不增）；
    否则红。--update-baseline 通道在写基线前先行调用本函数（自由文本 --change-note
    不是放行口）；--require-baseline 下缺发布快照即红（观察期基准不得静默缺失）。
    """
    try:
        snapshot = contract_gate.latest_released_snapshot()
    except RuntimeError as e:
        return [f"released contract snapshots unreadable: {e}"]
    if snapshot is None:
        if require_released:
            return [
                "released contract snapshot missing: contracts/released/ — the "
                "deprecation observation window needs the frozen snapshot of the "
                "last release (release_index basis, Issue #2837); freeze one with "
                "`python scripts/regenerate_openapi.py --release-snapshot` at "
                "release time"]
        return []
    return contract_gate.check_deprecation_gate(
        current_paths, snapshot["paths"],
        contract_gate.current_release_index())


def load_baseline(path: str):
    """读取基线契约文件，返回 (paths, version) 或 (None, None)。

    基线缺失返回 (None, None) 交由调用方决定是否报错（--require-baseline）；
    文件存在但不可读/非 JSON 属异常，直接 FAIL 退出。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError) as e:
        print(f"FAIL: cannot read baseline contract {path}: {e}", file=sys.stderr)
        sys.exit(1)
    return data.get("paths", {}), data.get("info", {}).get("version", "")


def build_current_contract() -> dict:
    """当前代码渲染 OpenAPI 契约（不启动服务，临时 sqlite，不落盘）。"""
    import tempfile

    os.environ.setdefault(
        "DEVOPS_DB_PATH",
        os.path.join(tempfile.gettempdir(), "grape_openapi_check.db"),
    )
    os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

    from src.api.openapi import custom_openapi
    from src.api.main import app

    return custom_openapi(app)


def check_checked_in_copy(current_paths: dict, copy_path: str) -> list:
    """库内契约副本（/static/openapi.json 的实际下载源）必须与当前渲染一致。

    G6 回归背景（PR #1363 第 3 轮 QA）：新增 17 个端点后忘了重跑生成器，
    库内副本仍是旧契约 → 用户下载到的 openapi.json 缺 takeover/IM/pin/copy/readd。
    比对口径取路径集合（版本/描述差异由生成器负责刷新）。
    """
    if not os.path.isfile(copy_path):
        return [f"checked-in contract copy missing: {copy_path}"]
    try:
        with open(copy_path, encoding="utf-8") as f:
            copy_paths = json.load(f).get("paths", {})
    except Exception as e:  # noqa: BLE001 - 契约文件损坏要 fail-loud
        return [f"cannot read checked-in contract copy {copy_path}: {e}"]

    added = sorted(set(current_paths) - set(copy_paths))
    removed = sorted(set(copy_paths) - set(current_paths))
    if not added and not removed:
        return []
    detail = []
    if added:
        detail.append(f"{len(added)} missing (e.g. {', '.join(added[:5])})")
    if removed:
        detail.append(f"{len(removed)} stale (e.g. {', '.join(removed[:5])})")
    return [
        f"checked-in contract copy is stale: {copy_path} "
        f"({len(copy_paths)} paths) vs rendered ({len(current_paths)} paths): "
        + "; ".join(detail)
        + " — run `python scripts/regenerate_openapi.py` and commit the copy"
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="CI OpenAPI contract endpoint completeness check (Issue #897)")
    parser.add_argument(
        "--baseline", default=os.path.join("help", "api", "openapi-baseline.json"),
        help="baseline contract file (checked-in snapshot of last released contract)")
    parser.add_argument(
        "--require-baseline", action="store_true",
        help="fail when baseline file is missing (CI must set this so the "
             "version-diff check can never be silently skipped)")
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="write the current rendered contract back to the baseline file "
             "after all checks pass (refresh at release build time)")
    parser.add_argument(
        "--change-note", default="",
        help="contract change note recorded into the baseline file (audit trail; "
             "it does not bypass the deprecation observation window)")
    args = parser.parse_args(argv)

    schema = build_current_contract()
    current_paths = schema.get("paths", {})
    current_version = schema.get("info", {}).get("version", "")

    errors = check_core_endpoints(current_paths)
    errors.extend(check_operation_id_unique(current_paths))
    from src.api.main import app as _app
    errors.extend(check_route_registration_unique(_app.routes))
    # G6 守卫：库内契约副本（/static/openapi.json 下载源）不得与代码漂移
    errors.extend(check_checked_in_copy(current_paths, CONTRACT_COPY))

    baseline_paths, baseline_version = load_baseline(args.baseline)
    if baseline_paths:
        errors.extend(check_baseline_subset(
            current_paths, baseline_paths, current_version, baseline_version))
    elif args.require_baseline:
        errors.append(
            f"baseline contract missing: {args.baseline} "
            "(version-diff check cannot run; commit the released contract "
            "snapshot or pass the correct --baseline path)")

    # Issue #2837：删侧观察期门禁——normal 模式与 --update-baseline 模式都先行判定；
    # 写基线发生在 errors 检查之后，自由文本 --change-note 无法吸收未挂牌/未满期删除。
    errors.extend(run_deprecation_gate(current_paths, args.require_baseline))

    print(f"OpenAPI contract check: {len(current_paths)} paths, version={current_version}")
    if baseline_paths:
        print(f"  baseline: {args.baseline} ({len(baseline_paths)} paths, "
              f"version={baseline_version})")
    else:
        print("  baseline: none (version-diff check skipped)")
    try:
        snapshot = contract_gate.latest_released_snapshot()
    except RuntimeError:
        snapshot = None
    if snapshot is not None:
        print(f"  released snapshot: {snapshot['version']} "
              f"(release_index={snapshot['release_index']})")
    for ep in CORE_WORKFLOW_ENDPOINTS:
        mark = "OK" if ep in current_paths else "MISSING"
        print(f"  [{mark}] {ep}")

    if errors:
        print("FAIL: OpenAPI contract endpoint completeness check failed:",
              file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    if args.update_baseline:
        from src.kernel.help_page_gen import write_contract_baseline
        write_contract_baseline(schema, args.baseline, change_reason=args.change_note)
        print(f"  baseline updated: {args.baseline} "
              f"({len(current_paths)} paths, version={current_version})")
    print("OK: OpenAPI contract endpoint completeness check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
