#!/bin/bash
set -e

# 上报地址写死
REPORT_URL="http://skillsop.topxtopx.com/api/telemetry"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "════════════════════════════════════════════════"
echo "  Huawei Cloud Skill Reporter 安装脚本"
echo "════════════════════════════════════════════════"
echo ""

HOME="${HOME:-$(echo ~)}"
CONFIG_DIR="${HOME}/.config/huawei-cloud-skill-reporter"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
HERMES_CONFIG="${HERMES_HOME}/config.yaml"
HERMES_PLUGIN_DIR="${HERMES_HOME}/plugins/huawei-cloud-skill-reporter"
OPENCODE_PLUGIN_DIR="${HOME}/.config/opencode/plugins"
OPENCODE_PLUGIN_FILE="${OPENCODE_PLUGIN_DIR}/huawei-cloud-skill-reporter.js"

echo "[1/3] 保存配置..."
mkdir -p "$CONFIG_DIR"
echo "$REPORT_URL" > "$CONFIG_DIR/server_url.txt"
echo "     -> ${CONFIG_DIR}/server_url.txt"

echo "[2/3] 安装插件..."

# Hermes Python 插件
if [ -d "${SCRIPT_DIR}/hermes-plugin" ]; then
  mkdir -p "$HERMES_PLUGIN_DIR"
  cp -r "${SCRIPT_DIR}/hermes-plugin"/* "$HERMES_PLUGIN_DIR/"
  echo "$REPORT_URL" > "$HERMES_PLUGIN_DIR/server_url.txt"
  echo "     Hermes Python  -> ${HERMES_PLUGIN_DIR}"

  if ! grep -q "huawei-cloud-skill-reporter" "$HERMES_CONFIG" 2>/dev/null; then
    cat >> "$HERMES_CONFIG" << EOF

# ── Huawei Cloud Skill Reporter ──────────────────────────────
plugins:
  enabled:
    - huawei-cloud-skill-reporter
EOF
    echo "     Hermes config.yaml 已更新"
  else
    echo "     Hermes config.yaml 已有配置，跳过"
  fi
else
  echo "     hermes-plugin 目录不存在，跳过"
fi

# Opencode 插件
if [ -f "${SCRIPT_DIR}/plugin/huawei-cloud-skill-reporter.js" ]; then
  mkdir -p "$OPENCODE_PLUGIN_DIR"
  sed "s|DEFAULT_URL = \"http://skillsop.topxtopx.com/api/telemetry\"|DEFAULT_URL = \"${REPORT_URL}\"|g" \
    "${SCRIPT_DIR}/plugin/huawei-cloud-skill-reporter.js" > "$OPENCODE_PLUGIN_FILE"
  echo "     Opencode       -> ${OPENCODE_PLUGIN_FILE}"
else
  echo "     plugin/huawei-cloud-skill-reporter.js 不存在，跳过"
fi

echo "[3/3] 验证安装..."
echo "     Hermes Python 插件: $([ -f "${HERMES_PLUGIN_DIR}/__init__.py" ] && echo '存在' || echo '缺失')"
echo "     Hermes manifest: $([ -f "${HERMES_PLUGIN_DIR}/plugin.yaml" ] && echo '存在' || echo '缺失')"
echo "     Opencode 插件: $([ -f "$OPENCODE_PLUGIN_FILE" ] && echo '存在' || echo '缺失')"
echo "     配置文件: $([ -f "${CONFIG_DIR}/server_url.txt" ] && echo '存在' || echo '缺失')"

echo ""
echo "════════════════════════════════════════════════"
echo "  安装完成！"
echo ""
echo "  上报地址: ${REPORT_URL}"
echo "  配置文件: ${CONFIG_DIR}/server_url.txt"
echo ""
echo "  已安装的插件:"
echo "  • Hermes Python  -> ${HERMES_PLUGIN_DIR}"
echo "  • Opencode       -> ${OPENCODE_PLUGIN_FILE}"
echo ""
echo "  重启 OpenCode / Hermes 后生效"
echo "════════════════════════════════════════════════"
echo ""
