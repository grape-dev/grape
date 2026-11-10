#!/bin/bash
echo "=== Running all contract checks ==="
./scripts/check-shared-versions.sh
python3 scripts/check-i18n-keys.py
python3 scripts/check-css-vars.py
python3 scripts/check-health-contract.py
python3 scripts/check-secrets.py
# Issue #536：危险 DDL 卡点——db_setup.py 新增 DROP/RENAME/MODIFY COLUMN 必须收口
python3 scripts/check-destructive-ddl.py
# Issue #897：契约端点完整性卡点——核心工作流端点（/api/a2a、/api/tasks/* 等）
# 不得从 OpenAPI 契约丢失（v2.2.0 曾被子集落盘覆盖，CLI 命令组不渲染）。
# --require-baseline：基线快照（help/api/openapi-baseline.json）缺失时 FAIL，
# 版本间比对不再静默跳过（QA 审查修复项）。
python3 scripts/check-openapi-endpoints.py --require-baseline
# check-doc-redaction.py 是针对开源清洗分支（weekly-07-clean）的 docs 门禁：
# 内部周分支的 docs 合法保留内部信息，直接跑会整片报错，故全仓扫描只作开源合入前手工门禁：
#   python3 scripts/check-doc-redaction.py
# PR 门禁（2026-11-09 PR #1077 评审）：两条入口，让规则有真实执行点且不阻断历史存量：
#   1) GRAPE_PR_DIFF=<base-sha>：只扫 base..HEAD 的新增行（推荐，口径 = 只审本次引入）
#   2) GRAPE_PR_FILES="a.md b.js"：整文件扫描指定清单（文档专项排查用）
if [ -n "${GRAPE_PR_DIFF:-}" ]; then
  echo "--- doc-redaction (diff vs $GRAPE_PR_DIFF) ---"
  python3 scripts/check-doc-redaction.py --diff "$GRAPE_PR_DIFF" || exit 1
fi
if [ -n "${GRAPE_PR_FILES:-}" ]; then
  echo "--- doc-redaction (changed files) ---"
  # shellcheck disable=SC2086
  python3 scripts/check-doc-redaction.py --files $GRAPE_PR_FILES || exit 1
fi
# smoke-admin.sh needs a running server (localhost:3000); run manually after deploy
# ./scripts/smoke-admin.sh
echo "=== Contract checks complete ==="
