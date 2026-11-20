"""重生成 OpenAPI 契约（帮助 | api-docs-standardization-design.md P0 R1）。

把当前代码的对外安全路由契约落盘两份：
- help/api/openapi.json      （规范副本）
- src/api/frontend/shell/openapi.json（固定下载 URL /static/openapi.json）

用法：
    python scripts/regenerate_openapi.py [--help-path PATH] [--static-path PATH]
    python scripts/regenerate_openapi.py --release-snapshot
                                          # 发布时冻结 contracts/released/{version}.json
                                          # （弃用期观察期判定基准，release_index 单调）
    python scripts/regenerate_openapi.py --update-baseline --reason "契约变更说明"
                                          # 有意增删端点后重写契约基线（reason 必填留痕；
                                          # 写基线前先过弃用期观察期门禁，reason 不能绕过）

不启动服务、不跑 lifespan，仅复用 custom_openapi 渲染，DB 走临时 sqlite 测试库。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DEVOPS_DB_PATH",
    os.path.join(tempfile.gettempdir(), "grape_openapi_regen.db"),
)
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from src.api.openapi import custom_openapi, write_contract_integrity_baseline
from src.domains.contracts import deprecation as contract_gate
from src.kernel.help_page_gen import write_contract_baseline, write_openapi_contract
from src.api.main import app


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Regenerate OpenAPI contract artifacts")
    parser.add_argument("--help-path", default=None, help="overwrite help/api/openapi.json path")
    parser.add_argument("--static-path", default=None, help="overwrite static/openapi.json path")
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="rewrite help/api/openapi-baseline.json from the current contract "
             "(after intentional endpoint changes); requires --reason; "
             "no contract artifacts written in this mode")
    parser.add_argument(
        "--reason", default="",
        help="contract change note recorded into help/api/openapi-baseline.json "
             "(required with --update-baseline)")
    parser.add_argument(
        "--release-snapshot", action="store_true",
        help="freeze the current rendered contract as the next released snapshot "
             "contracts/released/{version}.json (release_index = max existing + 1; "
             "release-time action, snapshots are frozen and must never be "
             "rewritten; Issue #2837 deprecation observation window basis)")
    args = parser.parse_args(argv)

    if args.release_snapshot:
        # Issue #2837：发布冻结——渲染当前契约并冻结为下一发布快照
        # （release_index 单调递增；观察期删侧判定以这些不可变快照为基准）
        schema = custom_openapi(app)
        snapshot = contract_gate.write_released_snapshot(schema)
        print(f"OpenAPI: released snapshot frozen -> contracts/released/{snapshot['version']}.json")
        print(f"  release_index={snapshot['release_index']} "
              f"version={snapshot['version']} paths={len(snapshot['paths'])}")
        return 0

    if args.update_baseline:
        # 基线重生成：变更说明必填——缺省拒绝，防门禁静默重定基
        reason = " ".join((args.reason or "").split())
        if not reason:
            print("OpenAPI: --update-baseline requires --reason <contract change note>",
                  file=sys.stderr)
            print("  describe what intentionally changed (endpoints) so the "
                  "baseline stays auditable", file=sys.stderr)
            return 2
        schema = custom_openapi(app)
        # Issue #2837：写基线前先过删侧观察期门禁——与 scripts/check-openapi-
        # endpoints.py --update-baseline 走同一个 check_deprecation_gate，
        # --reason 自由文本说明不得成为绕过观察期的第二条放行口
        # （基准 = release_index 最大的已发布冻结快照）。
        try:
            released = contract_gate.latest_released_snapshot()
        except RuntimeError as e:
            print(f"OpenAPI: released snapshot unreadable — refusing to re-baseline: {e}",
                  file=sys.stderr)
            return 2
        if released is not None:
            gate_errors = contract_gate.check_deprecation_gate(
                schema.get("paths") or {}, released["paths"],
                contract_gate.current_release_index())
            if gate_errors:
                print("OpenAPI: deprecation observation window gate FAILED — "
                      "refusing to re-baseline (Issue #2837挂牌→观察→删除纪律; "
                      "--reason cannot bypass it):", file=sys.stderr)
                for e in gate_errors:
                    print(f"  - {e}", file=sys.stderr)
                return 2
        data = write_contract_baseline(schema, change_reason=reason)
        print("OpenAPI: baseline updated -> help/api/openapi-baseline.json")
        print(f"  endpoint_count={data['endpoint_count']} "
              f"change_reason={data['change_reason']}")
        # 组级完整性基线（contracts/openapi_baseline.json）同一通道重定基：
        # contract_integrity() 的判定基准，端点有意增删必须同步，否则门禁恒红（QA G-5）
        integrity = write_contract_integrity_baseline(schema, change_reason=reason)
        print("OpenAPI: integrity baseline updated -> contracts/openapi_baseline.json")
        print(f"  endpoint_count={integrity['endpoint_count']} "
              f"groups={len(integrity['groups'])} change_reason={integrity['change_reason']}")
        return 0

    schema = custom_openapi(app)
    n = write_openapi_contract(schema, help_path=args.help_path, static_path=args.static_path)
    print(f"OpenAPI: contract dumped ({n} paths)")
    from src.kernel.help_page_gen import OPENAPI_HELP_PATH, OPENAPI_STATIC_PATH
    print(f"  -> {args.help_path or OPENAPI_HELP_PATH}")
    print(f"  -> {args.static_path or OPENAPI_STATIC_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())