#!/usr/bin/env bash
# 轻量入口：转发到 skills_verify 通用部署脚本
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/../deploy_skill.sh" "$@"
