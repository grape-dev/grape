#!/usr/bin/env bash
# 一道门：三库独立化收口的标准验收（P1 静态面 + 契约面 + 门禁测试 + 归位候选排序）。
# 用法：bash scripts/rds_gate.sh            # 全跑
#       bash scripts/rds_gate.sh --quick    # 跳过 pytest（只跑静态/契约/排序）
# 退出码：0 = 全绿；1 = 有红项（输出里逐条列出）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
PY=${PY:-venv/bin/python}
OWNER=docs/weekly-11/table-owner.tsv
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
  step "门禁测试（arch + contracts + scripts）"
  "$PY" -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider 2>&1 | tail -2 || rc=1
  # 测试会改写 openapi 快照，属产物污染，必须还原
  git checkout -- src/api/frontend/shell/openapi.json 2>/dev/null || true
fi

step "归位候选（按收益排序，下一批从这里取）"
"$PY" scripts/rds_burndown.py --rank 2>&1 | head -10 || rc=1

echo
if [ "$rc" = "0" ]; then echo "GATE: 全绿"; else echo "GATE: 有红项（见上面逐条）"; fi
exit "$rc"