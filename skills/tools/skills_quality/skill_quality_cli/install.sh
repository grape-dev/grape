#!/usr/bin/env bash
# 安装 skill-quality CLI 到 /usr/local/bin
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="/usr/local/bin/skill-quality"

echo "安装 skill-quality CLI..."
cp "${SCRIPT_DIR}/skill-quality" "${TARGET}"
chmod +x "${TARGET}"
echo "已安装到 ${TARGET}"
echo "验证: skill-quality --version"
skill-quality --version
