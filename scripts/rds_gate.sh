#!/usr/bin/env bash
# 一道门：三库独立化收口的标准验收（P1 静态面 + 契约面 + 门禁测试 + 归位候选排序）。
# 用法：bash scripts/rds_gate.sh            # 全跑
#       bash scripts/rds_gate.sh --quick    # 跳过 pytest（只跑静态/契约/排序）
# 退出码：0 = 全绿；1 = 有红项（输出里逐条列出）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
PY=${PY:-venv/bin/python}
OWNER=configs/table_ownership.json
BASE=docs/weekly-11/foreign-refs-allowlist.csv
rc=0
step() { echo; echo "=== $* ==="; }

step "R1-R7 架构约束"
python3 scripts/check-arch.py | tail -2 || rc=1

step "外域表引用棘轮（G4）"
python3 scripts/foreign_table_refs.py --owner "$OWNER" --baseline "$BASE" --check | tail -2 || rc=1

step "内部契约快照（P2）"
python3 scripts/gen_internal_contract.py --check | tail -2 || rc=1

step "依赖看板（P3）"
python3 scripts/gen_domain_dependency_board.py --check | tail -2 || rc=1

if [ "${1:-}" != "--quick" ]; then
  step "门禁测试（arch + contracts + scripts + 架构白名单精度）"
  # QA G5：白名单精度守卫（tests/runtime/test_check_arch_whitelist_precision.py）此前不在门禁里，
  # 白名单漂移只能靠人工复审发现。这里显式接线，与 check-arch 的静态校验互为补充。
  "$PY" -m pytest tests/arch tests/contracts tests/scripts \
      tests/runtime/test_check_arch_whitelist_precision.py -q -p no:cacheprovider 2>&1 | tail -2 || rc=1
  # 测试会改写 openapi 快照，属产物污染，必须还原
  git checkout -- src/api/frontend/shell/openapi.json 2>/dev/null || true
fi

step "未定义名棘轮（F821）"
# QA G6：门禁此前对 undefined name 零可见性（44 处静默失败即由此漏过复审）。
"$PY" scripts/check_undefined_names.py || rc=1

step "进程库连接棘轮（PG1）"
# 分库后「域文件用进程库连接查本域表」在别的进程里会打到错的库（静默旧值或 1146）。
# 与 foreign_table_refs（跨域表引用）互补：这里比较的是「文件域 vs 连接来源」。
python3 scripts/scan_pgconn.py --check | tail -2 || rc=1

step "归位候选（按收益排序，下一批从这里取）"
# 归位候选只是报告，有剩余才正常；不能把它当成红项
"$PY" scripts/rds_burndown.py --rank 2>&1 | head -10 || true

echo
if [ "$rc" = "0" ]; then echo "GATE: 全绿"; else echo "GATE: 有红项（见上面逐条）"; fi
exit "$rc"