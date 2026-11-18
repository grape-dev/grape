#!/usr/bin/env bash
# 把当前 checkout 增量同步到兄弟机并部署（四进程 + 三库）。
#
# 为什么不再打 tar：整包 49.5MB，打包 4.8s + 上传 1.0s + 远端解包 2.5s + 复制到租户 2.0s ≈ 10.3s，
# 而源码没变的日子这些开销全是浪费。rsync 增量在同样的目录上实测 0.8s，且远端不存在"解包"这一步。
#
# 用法：
#   SIBLING_PWD='...' bash scripts/sync_to_sibling.sh              # 同步 + 远端部署
#   SIBLING_PWD='...' bash scripts/sync_to_sibling.sh --sync-only   # 只同步，不部署
#   SIBLING=192.168.0.116 SIBLING_PWD='...' ...                     # 覆盖目标机
#
# 需要 sshpass（已装）。口令只从环境变量取，不写入文件、不出现在进程标题里以外的地方。
set -euo pipefail

SIBLING="${SIBLING:-192.168.0.116}"
SIBLING_PWD="${SIBLING_PWD:?需要 SIBLING_PWD（兄弟机 root 口令）}"
DST="${DST:-/home/grape/grape-new-version2}"
TENANT_DST="${TENANT_DST:-/home/orgtest/grape-new-version2}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"

_ssh() { sshpass -p "$SIBLING_PWD" ssh $SSH_OPTS "root@$SIBLING" "$@"; }

echo "[sync] $SRC -> $SIBLING:$DST（rsync 增量）"
_T0=$(date +%s)
sshpass -p "$SIBLING_PWD" rsync -az --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='data/' \
  --exclude='.grape/skills.bak' --exclude='static-dist.bak' \
  -e "ssh $SSH_OPTS" \
  "$SRC/src" "$SRC/scripts" "$SRC/configs" "$SRC/deploy" "$SRC/docs" \
  "root@$SIBLING:$DST/"
echo "[sync] 传输完成 $(( $(date +%s) - _T0 ))s"

# 属性与租户镜像：rsync 以 root 写入会把属主变成 root，必须还原；
# 租户 checkout 用 rsync 增量（原来 cp -a 全量要 2s）。
_ssh "set -e
  cd $DST
  chown -R grape:grape src scripts configs deploy docs
  if [ -d $TENANT_DST/src ]; then
    rsync -a --delete src/ $TENANT_DST/src/
    chown -R orgtest:orgtest $TENANT_DST/src
  fi
  echo '[sync] 属主与租户镜像完成'"

if [ "${1:-}" = "--sync-only" ]; then
  echo "[sync] --sync-only：跳过部署"
  exit 0
fi

echo "[sync] 远端部署（deploy3db.sh）"
_ssh "cd $DST && bash deploy/deploy3db.sh"
echo "[sync] 总耗时 $(( $(date +%s) - _T0 ))s"