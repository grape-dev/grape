#!/usr/bin/env bash
# 离线安装 codegraph（代码图谱 CLI，Knowledge 看板 project-info 分析用）。
#
# codegraph 1.5.0 = 主包(安装器) + 平台二进制包 @colbymchenry/codegraph-linux-<arch>。
# 平台包自包含（内置 node 运行时 + Rust 内核 + wasm 语法），bin/codegraph 可直接执行，
# 无需系统 node/npm —— 本脚本即「解压平台包 + 软链 bin」，完全离线。
#
# 用法:
#   install-codegraph.sh [--src <tgz 目录>] [--prefix /usr/local] [--arch auto]
#   --src     codegraph tgz 所在目录（默认: 脚本所在目录）
#   --prefix  安装前缀（默认 /usr/local；bin/ 与 lib/node_modules/ 落在其下）
#   --arch    auto(按 uname -m) | linux-arm64 | linux-x64
#
# 验证: 安装后输出 codegraph --version（如 1.5.0）。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="/usr/local"
ARCH="auto"

while [ $# -gt 0 ]; do
  case "$1" in
    --src)    SRC="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --arch)   ARCH="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

if [ "$ARCH" = "auto" ]; then
  case "$(uname -m)" in
    x86_64|amd64)  PLAT="linux-x64" ;;
    aarch64|arm64) PLAT="linux-arm64" ;;
    *) echo "不支持的架构: $(uname -m)（可用 --arch linux-x64/linux-arm64 手动指定）"; exit 1 ;;
  esac
else
  PLAT="$ARCH"
fi

TGZ="$(ls "$SRC"/colbymchenry-codegraph-${PLAT}-*.tgz 2>/dev/null | head -1 || true)"
if [ -z "$TGZ" ] || [ ! -f "$TGZ" ]; then
  echo "未找到 ${PLAT} 平台包: ${SRC}/colbymchenry-codegraph-${PLAT}-*.tgz（跳过 codegraph 安装）"
  exit 0
fi

DEST="$PREFIX/lib/node_modules/@colbymchenry/codegraph-linux-${PLAT}"
BIN="$PREFIX/bin/codegraph"

# 已装同版本则跳过（幂等）
CUR_VER=""
if [ -x "$BIN" ]; then
  CUR_VER="$("$BIN" --version 2>/dev/null || true)"
fi
PKG_VER="$(basename "$TGZ" | sed -E 's/^colbymchenry-codegraph-linux-[a-z0-9]+-([0-9.]+)\.tgz$/\1/')"
if [ -n "$CUR_VER" ] && [ "$CUR_VER" = "$PKG_VER" ]; then
  echo "codegraph ${CUR_VER} 已安装（$BIN），跳过"
  exit 0
fi

echo "安装 codegraph ${PKG_VER} (${PLAT}) → $PREFIX ..."
mkdir -p "$DEST" "$PREFIX/bin"
# 平台包 tgz 内含 package/ 前缀，--strip-components=1 去掉
tar xzf "$TGZ" -C "$DEST" --strip-components=1
ln -sf "$DEST/bin/codegraph" "$BIN"

VER="$("$BIN" --version 2>/dev/null || echo "验证失败")"
echo "codegraph 安装完成: $VER"
[ "$VER" != "验证失败" ]
