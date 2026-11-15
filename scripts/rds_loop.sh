#!/usr/bin/env bash
# 跨域收口「磨」循环：每轮 = 门禁 → 余量 → 生成下一批作业单 → 等下一批改完再看。
# 它不代替改代码（剩下的都是动态 SQL/迁移逻辑，必须逐处判断），但保证每次开工都从
# 同一份数据出发：数字、排序、行号、验收口径都在 docs/weekly-11/next-batches.md 里。
#
# 用法：
#   bash scripts/rds_loop.sh            # 跑到余量清零或轮数用尽（默认 20 轮，每轮间隔 300s）
#   ROUNDS=1 SLEEP=0 bash scripts/rds_loop.sh   # 立刻跑一轮就退出（CI / 人工触发）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
PY=${PY:-venv/bin/python}
ROUNDS=${ROUNDS:-20}
SLEEP=${SLEEP:-300}
BRIEF=docs/weekly-11/next-batches.md

refs() { grep -oE "cross_domain_refs=[0-9]+" "$1" | head -1 | cut -d= -f2; }

for i in $(seq 1 "$ROUNDS"); do
  echo "===== 第 $i/$ROUNDS 轮 $(date +%H:%M:%S) ====="
  if ! bash scripts/rds_gate.sh --quick > /tmp/rds_gate_$i.log 2>&1; then
    echo "门禁红：见 /tmp/rds_gate_$i.log（先修门禁，再谈降数字）"
    grep -E "过期|FAIL|✗" /tmp/rds_gate_$i.log | head -5
    exit 1
  fi
  cur=$(refs /tmp/rds_gate_$i.log)
  echo "余量：${cur:-?} 处跨域引用"
  "$PY" scripts/rds_next_batch.py --top 3 --out "$BRIEF" >/dev/null
  echo "作业单：$BRIEF（取前 3 个文件）"
  if [ "${cur:-1}" = "0" ]; then
    echo "RESULT: 跨域引用清零，循环结束"
    exit 0
  fi
  echo "下一批目标："$(grep -m1 '^## ' "$BRIEF" | sed 's/^## //')
  [ "$i" = "$ROUNDS" ] && break
  echo "等待 ${SLEEP}s 后复查（这一轮该由改代码的人/agent 动手）"
  sleep "$SLEEP"
done
echo "RESULT: 用尽 $ROUNDS 轮，余量仍 ${cur:-?}（继续加轮次即可）"
exit 1