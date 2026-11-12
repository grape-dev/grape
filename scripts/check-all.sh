#!/bin/bash
# 解释器选择（QA #1272 I2）：守卫脚本 import 运行期依赖（fastapi 等），
# 系统 python3 缺依赖时会 ModuleNotFoundError 崩溃；旧版脚本无 `|| exit 1`，
# check-all 仍然 EXIT=0 → 门禁空转。这里按「能否 import fastapi」挑选，
# 全都不可用就直接 fail-loud，而不是让守卫崩在中间。
if [ -z "${PY:-}" ]; then
  _repo="$(cd "$(dirname "$0")/.." && pwd)"
  for _cand in "${GRAPE_PYTHON:-}" "$_repo/.venv/bin/python" "$HOME/grape/.venv/bin/python" "python3"; do
    [ -n "$_cand" ] || continue
    if "$_cand" -c "import fastapi" >/dev/null 2>&1; then PY="$_cand"; break; fi
  done
fi
if [ -z "${PY:-}" ]; then
  echo "[FATAL] 找不到可 import fastapi 的 Python；请设 GRAPE_PYTHON=/path/to/python" >&2
  exit 1
fi
echo "=== Running all contract checks ==="
./scripts/check-shared-versions.sh || exit 1
"$PY" scripts/check-i18n-keys.py || exit 1
"$PY" scripts/check-css-vars.py || exit 1
"$PY" scripts/check-health-contract.py || exit 1
"$PY" scripts/check-secrets.py || exit 1
# Issue #536：危险 DDL 卡点——db_setup.py 新增 DROP/RENAME/MODIFY COLUMN 必须收口
"$PY" scripts/check-destructive-ddl.py || exit 1
# Issue #897：契约端点完整性卡点——核心工作流端点（/api/a2a、/api/tasks/* 等）
# 不得从 OpenAPI 契约丢失（v2.2.0 曾被子集落盘覆盖，CLI 命令组不渲染）。
# --require-baseline：基线快照（help/api/openapi-baseline.json）缺失时 FAIL，
# 版本间比对不再静默跳过（QA 审查修复项）。
"$PY" scripts/check-openapi-endpoints.py --require-baseline || exit 1
# 四进程路由漂移：nginx 分流 vs 各进程实际路由（无 nginx/无进程时自动跳过）
if [ -f "${GRAPE_NGINX_CONF:-/etc/nginx/conf.d/grape.conf}" ]; then
  "$PY" scripts/probe/route_contract_probe.py || exit 1
else
  echo "[skip] route_contract_probe: nginx conf 不存在"
fi
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
