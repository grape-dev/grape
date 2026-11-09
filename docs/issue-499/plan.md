# 方案设计：Issue #499

## 核心决策

### 1. wheels 多平台预置 —— 平铺混装 + pip 平台标签自动匹配
- 打包端 `_ensure_wheels()` 按 `WHEEL_PLATFORMS = [("manylinux2014_x86_64","linux x86_64"), ("manylinux2014_aarch64","linux aarch64"), ("win_amd64","windows amd64")]` 矩阵逐平台 `pip download --platform <tag>`，全部落同一 `WHEELS_DIR` 平铺混装（纯 Python wheel 天然去重，多平台二进制 wheel 文件名不同不冲突）。
- 安装端不新增平台探测代码：`pip install --no-index --find-links <wheels>` 本身按 wheel 平台标签精确匹配本机环境，不兼容 wheel 被跳过；全需求无可兼容 wheel 时报错 → 安装脚本回退在线（既有兜底路径）。
- 保留 linux aarch64（构建机原生平台，现有 arm worker 离线安装能力不回退），新增 linux x86_64 与 win_amd64（Spec 指定平台，Windows 收 Linux wheel 的痛点）。
- 缓存 marker 升级为 `py_version:平台矩阵键:requirements哈希`，矩阵/依赖变化自动重下。

### 2. 安装脚本去引导 + 英文化
- sh 删除「===== 7. 执行引擎提示 =====」整段（`_engine_hint` 变量）；ps1 删除 `===== 7 =====` 段（`$engineHints`）。`install_engine`（--engine 显式安装，#471 C2）与引擎自动发现注册保留。
- sh 全部 `log/fail/echo/printf` 输出、ps1 全部 `Log/Fail/Write-*` 输出与用户可见字符串（如回滚版本兜底值 `未知版本`）英文化；代码注释不强制（不属输出）。
- ps1 删除 wheels 离线安装的 `$env:PROCESSOR_ARCHITECTURE -match "ARM64"` 门槛：多平台 wheels 后 Windows 离线安装可成功，失败仍回退在线（英文提示 `Installing deps online (fallback, platform wheel not bundled)`）。

### 3. 测试 seam（公开接口）
- `src.worker_package._ensure_wheels(py_version)` / 新增 `_pip_download_wheels(dest, platform_args, py_version)`：mock `_pip_download_wheels` 断言按矩阵逐平台调用、单平台失败不阻断、缓存 marker 短路。
- 安装脚本静态守卫：`tests/test_worker_install_ux.py` 断言无引擎引导段残留、输出字符串无 CJK、ps1 无 ARM64-only 门槛。

## 风险
- 包体增大（3 平台二进制 wheels）→ 只预置 requirements-worker.txt 必要项，实测体积见 package-size.md。
- pip 对不匹配 wheel 的跳过行为依赖平台标签机制（pip 标准语义）→ 失败回退在线兜底保留。
