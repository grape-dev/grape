#!/usr/bin/env python3
"""CI 文档面门禁（Issue #2837 挂牌态文档标注 + 基线存在面守卫，附 #2420 同步面观测）。

与 pytest 门禁共用 `src.kernel.help_docs_sync.py` 单一事实源
（api-docs-standardization-design.md R3/R4：脚本与测试口径一致）：

- Issue #2837（等价 #2783）挂牌态文档标注联动（本脚本主判定，FAIL 即红）：
  已挂牌（contracts/deprecated_endpoints.json）且仍在契约的端点，help/api 与
  docs/openapi 两处须同步标注 Deprecated + 替代端点
  （`src.domains.contracts.deprecation.deprecated_doc_annotation_errors`，
  与 check-openapi-endpoints.py 构成删侧/文档面双通道防绕过；空登记表空转不误红）。
- `--require-baseline`：基线存在面守卫（FAIL 即红）——基线快照
  （help/api/openapi-baseline.json，与 check-openapi-endpoints.py 同一
  checked-in 文件）缺失/不可读时 FAIL，杜绝静默跳过。文档面门禁不做版本间比对
  （版本间比对在契约面 check-openapi-endpoints.py）。
- #2420 双向同步面（missing/extra）当前为**观测不阻断**（WARN）：arch 线
  #2420 文档面未收敛（基线分支既有 18 failed，与本 PR 无关），收敛前不在 CI
  门禁里重复红；由 tests/api/test_api_docs_sync.py pytest 门禁管辖。收敛后可将
  WARN 升回 FAIL（与 weekly 线 #2420 版本对齐）。

用法（本地 / CI 通用，渲染当前代码契约，临时 sqlite，不启动服务、不落盘）：
    python3 scripts/check-api-docs.py [--require-baseline] [--baseline PATH]

与 scripts/check-openapi-endpoints.py 并列互补：彼管契约面登记清单完整性
（contracts/required_operations.json）与删侧观察期门禁，本管文档面挂牌标注
与基线携带。

退出码：0 通过；1 挂牌标注/基线守卫失败。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.kernel.help_docs_sync as help_docs_sync

# 与 check-openapi-endpoints.py 同一 checked-in 契约基线快照（文档面共享同一基线文件）
DEFAULT_BASELINE = os.path.join("help", "api", "openapi-baseline.json")


def _baseline_errors(path: str, require: bool) -> list:
    """基线面守卫：--require-baseline 下缺失/不可读即红（本地无该旗标时宽容跳过）。"""
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                f.read(1)
        except OSError as e:
            return [f"docs face baseline unreadable: {path}: {e}"]
        return []
    if require:
        return [
            f"docs face baseline missing: {path} — help/api must keep the "
            "checked-in contract snapshot (same file as check-openapi-endpoints.py;"
            " pass the correct --baseline path or commit the snapshot)"]
    return []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="CI docs face gate: deprecation doc annotation + baseline "
                    "guard (Issue #2837; shares src.kernel.help_docs_sync.py "
                    "with the pytest gate)")
    parser.add_argument(
        "--baseline", default=DEFAULT_BASELINE,
        help="checked-in contract baseline snapshot the docs face must ship with")
    parser.add_argument(
        "--require-baseline", action="store_true",
        help="fail when the baseline file is missing/unreadable (CI must set this "
             "so the shared checked-in baseline can never be silently dropped; "
             "version-diff itself runs in check-openapi-endpoints.py)")
    args = parser.parse_args(argv)

    errors = _baseline_errors(args.baseline, args.require_baseline)

    # Issue #2837：挂牌态文档标注联动（空登记表 → 空转不误红）。
    from src.domains.contracts.deprecation import deprecated_doc_annotation_errors
    errors += deprecated_doc_annotation_errors()

    # #2420 双向同步面：观测不阻断（arch 线文档面未收敛，见模块 docstring）。
    missing, extra = help_docs_sync.compare()
    drift = ([f"契约有、文档缺: {m.upper()} {p} ({op_id})"
              for m, p, op_id in missing]
             + [f"文档有、契约无（疑似已下线）: {m.upper()} {p}" for m, p in extra])

    if errors:
        print("FAIL: docs face deprecation/baseline check failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(
            "  fix: 挂牌端点须在 help/api 与 docs/openapi 两处同步标注 "
            "Deprecated + 替代端点（contracts/deprecated_endpoints.json 的 "
            "replacement 路径须出现在块内）；基线快照须随仓携带。", file=sys.stderr)
        return 1

    if drift:
        print(f"WARN: #2420 docs<->contract sync drift (not gating on arch line "
              f"until #2420 converges; owned by tests/api/test_api_docs_sync.py): "
              f"missing={len(missing)}, extra={len(extra)}", file=sys.stderr)
    print("OK: docs face deprecation/baseline check passed "
          f"(baseline={args.baseline}, deprecated=0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
