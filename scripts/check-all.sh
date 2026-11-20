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
# Issue #2227：grape CLI 能力漂移卡点——部署线必须支持 `git-repo commits --sha`
# （按分支/SHA 核验目标分支是否已实现）。无 grape 环境显式 SKIP 不误报；存在但缺
# `--sha` 时 FAIL 并输出实际版本与缺失项。可用 GRAPE_CLI_BIN 指定被测二进制。
"$PY" scripts/check_grape_cli_drift.py || exit 1
# Issue #2462 A3：跨域读查询名闭包——代码引用的 lib 函数与命名查询必须都在
# 目标域 public 模块的注册面内（lib 开关关闭时这类漂移在测试里不可见，
# 只会在三库三机切换时以 AttributeError/KeyError 暴露，因此做成静态门禁）。
"$PY" scripts/check_lib_query_names.py src || exit 1
# Issue #2479：禁止矩阵静态守卫——runtime/insight 域代码不得引用 mgmt remote
# 通道（MgmtServiceRemote / GsbClient("mgmt") / ServiceHttpClient("mgmt") /
# _PROVIDERS 裸访问）；运行时守卫见 src/kernel/gsb/provider.py::matrix_forces_lib。
"$PY" scripts/check_gsb_route_matrix.py || exit 1
# Issue #2504：跨域读清单棘轮门禁——src/ 跨域读点清单必须与基线快照一致
# （scripts/p4/read_inventory_baseline.json）。新增裸读/读点漂移即 FAIL；更新基线
# 须 `--update-baseline` 并在 PR 中说明理由（维护流程见
# docs/lib-3db/read-inventory-diff.md）。
"$PY" scripts/p4/scan_cross_domain_reads.py --check || exit 1
# Issue #897：契约端点完整性卡点——核心工作流端点（/api/a2a、/api/tasks/* 等）
# 不得从 OpenAPI 契约丢失（v2.2.0 曾被子集落盘覆盖，CLI 命令组不渲染）。
# --require-baseline：基线快照（help/api/openapi-baseline.json）缺失时 FAIL，
# 版本间比对不再静默跳过（QA 审查修复项）。
"$PY" scripts/check-openapi-endpoints.py --require-baseline || exit 1
# Issue #2837（等价 #2783）：弃用期机制文档面门禁——与上一条构成双通道防绕过。
# 已挂牌端点必须在 help/api 与 docs/openapi 两处同步标注 Deprecated + 替代端点；
# --require-baseline 保证共享契约基线快照随仓携带（缺失即 FAIL，不静默空转）。
"$PY" scripts/check-api-docs.py --require-baseline || exit 1
# 跨库视图新鲜度（缺列 1054 / 视图多余列 / 孤儿视图 1146 / 列类型漂移；
# 缺视图默认只报告（owner-only 新表不卡门禁），需要跨域的表登记在 create-missing 清单；
# 未配置 DB_HOST 时跳过，配置了但连不上则失败）
"$PY" scripts/domain_view_freshness.py --check --skip-if-unavailable \
    --strict-owner --known-duplicates scripts/view_owner_known_duplicates.txt \
    --create-missing-tables scripts/view_create_missing_allowlist.txt \
    --narrowed-tables scripts/view_narrowed_allowlist.txt \
    --ignore-missing-tables scripts/view_ignore_missing_tables.txt || exit 1
# 四进程路由漂移：nginx 分流 vs 各进程实际路由（无 nginx/无进程时自动跳过）
if [ -f "${GRAPE_NGINX_CONF:-/etc/nginx/conf.d/grape.conf}" ]; then
  # --warmup：先预热 worker 包/安装包等重接口，避免部署刚重启时的冷启动超时被误判为 unreachable
  "$PY" scripts/probe/route_contract_probe.py --warmup || exit 1
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
# PR #1377 复审 I-5：check-all 全绿 ≠ 守卫绿。两条前端缓存棘轮（页面模块 ?v= 冻结表、
# 语言包内容指纹/cacheVer 单调）与 node 前端用例此前不在本门禁内，导致「check-all exit 0」
# 掩盖了 head 上真实失败的守卫。此处纳入统一门禁。
"$PY" -m pytest -q tests/api/test_frontend_cache_version_ratchet.py \
                      tests/api/test_i18n_locale_cache_ratchet.py \
                      tests/api/test_frontend_bundle_source_ratchet.py || exit 1
# G-01（QA 第 6 轮守卫轴）：异步阻塞棘轮——db_lock 长扫描会让心跳/claim/complete
# 一起排队（实测 4 次复发为 nginx 504 + 引擎失联）。此前不在本门禁内，PR #1507
# 曾以 +28 新增形态绕过所有「守卫全绿」自述。请求路径新增读一律走
# src/kernel/bulk_read.reader()（免进程锁读通道）。
"$PY" scripts/check_async_blocking.py || exit 1
if command -v node >/dev/null 2>&1; then
  _rc=0
  for _f in tests/js/test_*.js; do
    node "$_f" >/dev/null || { echo "[FAIL] $_f" >&2; _rc=1; }
  done
  [ "$_rc" = 0 ] || exit 1
  echo "[ok] tests/js (node)"
else
  echo "[skip] tests/js: node 不可用"
fi
# smoke-admin.sh needs a running server (localhost:3000); run manually after deploy
# ./scripts/smoke-admin.sh
echo "=== Contract checks complete ==="
