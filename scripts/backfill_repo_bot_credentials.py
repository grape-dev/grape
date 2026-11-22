#!/usr/bin/env python3
"""Issue #3740：bot 凭据回填 CLI（managed_repos 历史列 → grape_t_repo_credentials）。

为什么需要它：rc（``cred_type='bot'``）在生产的 gitcode/github 侧从未回填——RepoBotToken
的 rc 查询恒 miss、100% 走 legacy 列回落（实测 8,872 条 WARNING/h，占全部告警 46%）。
本脚本把「回填 → 验证 → 盖章」做成一次可审计的操作，由 sre 在回填窗口执行；
代码侧不再注册自动迁移（回填写生产数据属高风险动作，必须受控）。

用法（顺序即推荐流程，**默认 dry-run，不写库**）::

    # 1) 回填前基线（缺口有多少、覆盖多少）
    python scripts/backfill_repo_bot_credentials.py --verify

    # 2) 计划（零写入，只有 --apply 才写）
    python scripts/backfill_repo_bot_credentials.py --dry-run

    # 3) 执行（先备份数据库！）
    python scripts/backfill_repo_bot_credentials.py --apply

    # 4) 回填后验证 + 盖章（验证缺口非空则拒绝盖章）
    python scripts/backfill_repo_bot_credentials.py --verify --stamp

退出码：0=成功；1=验证发现缺口（未达下线前置）；2=环境/参数错误。

数据库由 ``DEVOPS_DB_PATH``（sqlite）或 MySQL 环境变量决定，与主程序同源——
不要在脚本里另配 DSN，避免回填打错库。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Issue #3740 bot credential backfill "
                    "(managed_repos legacy columns -> grape_t_repo_credentials)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=False,
                      help="只算计划，零写入（默认行为）")
    mode.add_argument("--apply", action="store_true", default=False,
                      help="真正执行回填（幂等：只填空，不覆盖已有 rc 行）")
    ap.add_argument("--verify", action="store_true", default=False,
                    help="输出回填验证报告（缺口 / 覆盖率）")
    ap.add_argument("--stamp", action="store_true", default=False,
                    help="回填完成后在 grape_t_schema_migrations 盖章（下线 checklist ①）")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="以 JSON 输出（默认 JSON；保留开关以便将来切换人读格式）")
    return ap


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    # 导入放在参数解析之后：--help/参数错误不应触发 DB 初始化
    from src.api.db_setup import (
        REPO_BOT_BACKFILL_NAME,
        REPO_BOT_BACKFILL_VERSION,
        backfill_repo_bot_token_columns_to_rc,
        stamp_repo_bot_credential_backfill,
        verify_repo_bot_credential_backfill,
    )

    out = {"mode": "verify" if args.verify and not args.apply else
           ("apply" if args.apply else "dry-run")}

    if args.verify:
        out["verify"] = verify_repo_bot_credential_backfill()

    if args.apply:
        out["backfill"] = backfill_repo_bot_token_columns_to_rc(dry_run=False)
        if not args.verify:
            out["verify"] = verify_repo_bot_credential_backfill()
    elif not args.verify:
        out["backfill"] = backfill_repo_bot_token_columns_to_rc(dry_run=True)

    if args.stamp:
        verify = out.get("verify") or verify_repo_bot_credential_backfill()
        if verify.get("probe_ok") is False:
            out["stamp"] = {"skipped": True,
                            "reason": "managed_repos 探列失败，拒绝盖章（无法证明回填完成）",
                            "error": verify.get("error") or ""}
            print(json.dumps(out, ensure_ascii=False, indent=2))
            print("[error] managed_repos 探列失败，验证/盖章不可信，拒绝执行", file=sys.stderr)
            return 2
        if verify.get("missing"):
            out["stamp"] = {"skipped": True,
                            "reason": "回填缺口非空，拒绝盖章（先 --apply）",
                            "missing_count": len(verify["missing"])}
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 1
        out["stamp"] = stamp_repo_bot_credential_backfill()
        out["stamp"]["version"] = REPO_BOT_BACKFILL_VERSION
        out["stamp"]["name"] = REPO_BOT_BACKFILL_NAME

    print(json.dumps(out, ensure_ascii=False, indent=2))

    backfill = out.get("backfill") or {}
    verify = out.get("verify") or {}
    # 探列失败 = 环境/驱动异常：回填与验证结果都不可信，必须失败退出而不是
    # 把「零写入 + 空缺口」当成成功（Issue #3740 复审 Required）。
    for label, payload in (("backfill", backfill), ("verify", verify)):
        if payload.get("probe_ok") is False:
            print(f"[error] {label}: managed_repos 探列失败，拒绝把空结果当成功: "
                  f"{payload.get('error') or ''}", file=sys.stderr)
            return 2
    if verify.get("missing"):
        print(f"[warn] 仍有 {len(verify['missing'])} 个仓库缺 rc bot 行", file=sys.stderr)
        return 1
    if args.apply and backfill.get("filled", 0) == 0 and backfill.get("would_fill", 0):
        print("[warn] planned rows > 0 but nothing written (并发/唯一约束冲突？)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())