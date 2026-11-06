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
退出码：0 通过；1 校验失败。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
)

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.|$)")


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


def main() -> int:
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
    args = parser.parse_args()

    schema = build_current_contract()
    current_paths = schema.get("paths", {})
    current_version = schema.get("info", {}).get("version", "")

    errors = check_core_endpoints(current_paths)

    baseline_paths, baseline_version = load_baseline(args.baseline)
    if baseline_paths:
        errors.extend(check_baseline_subset(
            current_paths, baseline_paths, current_version, baseline_version))
    elif args.require_baseline:
        errors.append(
            f"baseline contract missing: {args.baseline} "
            "(version-diff check cannot run; commit the released contract "
            "snapshot or pass the correct --baseline path)")

    print(f"OpenAPI contract check: {len(current_paths)} paths, version={current_version}")
    if baseline_paths:
        print(f"  baseline: {args.baseline} ({len(baseline_paths)} paths, "
              f"version={baseline_version})")
    else:
        print("  baseline: none (version-diff check skipped)")
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
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"  baseline updated: {args.baseline} "
              f"({len(current_paths)} paths, version={current_version})")
    print("OK: OpenAPI contract endpoint completeness check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
