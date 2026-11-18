#!/usr/bin/env bash
# 安装 Grape 的 nginx gzip drop-in（两台机器各跑一次，需 root）。
#
# 背景：nginx.conf 里 `gzip on;` 默认开着，但 `gzip_types` 被注释，只有 text/html 被压缩。
# /api/issues 返回的 JSON（约 148KB）因此全程明文过公网，实测 EIP 上 1452-3271ms，
# 压缩后 145-257ms —— 这个差距会把需求中心首屏推过前端 8s 看门狗。
#
# 用法：sudo bash deploy/install-nginx-gzip.sh
# 幂等：重复执行只会覆盖同名 drop-in 并 reload。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/nginx-gzip.conf"
DST=/etc/nginx/conf.d/grape-gzip.conf

[ -f "$SRC" ] || { echo "[gzip] 缺少 $SRC" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "[gzip] 需要 root（sudo bash $0）" >&2; exit 1; }

if [ -f "$DST" ] && cmp -s "$SRC" "$DST"; then
  echo "[gzip] $DST 已是最新，跳过"
  exit 0
fi

cp "$SRC" "$DST"
echo "[gzip] 已写入 $DST"

if ! nginx -t 2>/tmp/nginx-gzip-test.err; then
  echo "[gzip] nginx -t 失败，回滚：" >&2
  cat /tmp/nginx-gzip-test.err >&2
  rm -f "$DST"
  exit 1
fi

nginx -s reload
echo "[gzip] nginx 已 reload"
echo "[gzip] 自检：curl -s -o /dev/null -D- -H 'Accept-Encoding: gzip' \\"
echo "        http://127.0.0.1:38080/api/ping | grep -i content-encoding"
