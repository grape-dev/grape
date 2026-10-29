# Spec: Issue #499 — worker 安装体验优化

> 来源：Issue #499（issue agent Spec 评论）| 目标分支：weekly-09

## 目标
1. 删除安装脚本中引导用户安装 opencode/hermes/codearts/codex 执行引擎的提示段（worker 启动自动发现引擎，引导无必要）。
2. 安装输出文案统一英文（脚本用户可见输出无中文）。
3. 必要依赖 wheels 多平台预置进 grape-worker 包：linux x86_64 + linux aarch64 + win_amd64，首装默认包内离线安装，仅平台不匹配在线兜底。

## 范围
- `scripts/install-worker.sh`：删除「===== 7. 执行引擎提示 =====」段（`_engine_hint`）；全部 log/fail/echo/printf 输出英文化；保留 `install_engine`（#471 C2 --engine 显式安装功能）与引擎自动发现。
- `scripts/install-worker.ps1`：删除「===== 7. 执行引擎提示 =====」段（`$engineHints`）；删除 wheels 离线安装的 ARM64-only 门槛（多平台 wheels 后 Windows 也可离线）；输出英文化（`未知版本` 等）。
- `src/worker_package.py`：`_ensure_wheels()` 改为按 `WHEEL_PLATFORMS` 矩阵（manylinux2014_x86_64 / manylinux2014_aarch64 / win_amd64）各下载一份 wheels 平铺混装于同一目录；安装端 pip 按平台标签自动精确匹配。
- 不改：requirements-worker.txt 依赖清单、引擎自动发现/注册逻辑、macOS 平台（在线兜底）。

## 验收标准
- Linux/Windows 全新安装输出无「安装 opencode/hermes/codex/codearts 引擎」引导语。
- 安装输出（log/fail/echo/printf / Log/Fail/Write-Host）无中文字符。
- linux x86_64 / linux aarch64 / win_amd64 首装依赖走包内 wheels 离线完成；不匹配平台在线兜底且英文提示清晰。
- 包体大小前后对比有记录（docs/issue-499/package-size.md）。
- 现有安装/升级/回滚回归测试通过；新增 `_ensure_wheels` 平台选择测试与脚本 UX 静态守卫测试。
