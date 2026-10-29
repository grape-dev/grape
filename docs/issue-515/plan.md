# Implementation Plan: Issue #515 — worker 安装包支持 x86_64 Linux 接入

## Overview

worker 安装包当前按构建机（aarch64）单架构捆绑 standalone Python，x86_64 目标机选中 aarch64 解释器后 `Exec format error` 提前退出，无法到达在线兜底。本计划分两步：安装端架构校验（方案 A）保住在线通路，打包端双架构解释器（方案 B）补完整离线安装。

## Architecture Decisions

- **安装端双保险校验**：`uname -m` 归一化选 `python-$TARGET_ARCH/` 目录 + 试运行 `--version` 确认可执行。免解析 ELF，覆盖「目录名与内容不一致」「文件损坏」等交叉场景。
- **打包端按架构分目录**：`python-x86_64/`、`python-aarch64/`。`_find_python()` 重构为 `_find_pythons()` 返回 `{arch: py_root}`，缺架构时 `uv python install 3.12 --arch <arch>` 显式补齐。
- **旧包格式读取兜底**：install-worker.sh 仍保留 `python/` 单目录检测（老主控下发的旧包），但必须通过 `--version` 校验，防 aarch64 旧包在 x86 上再次踩 Exec format error。
- **版本指纹联动**：`package_version()` 的 `-p` 指纹改为各架构目录联合指纹，任一架构解释器变化即触发包重建。
- **测试 seam**：
  - `src.worker_package._scan_pythons/_find_pythons`（monkeypatch `UV_PYTHON_DIR` 为假目录即可单测，不触碰真实 uv）；
  - `build_package()` 产物 tar 结构（断言 `python-x86_64/`、`python-aarch64/` 目录存在、无 `python/`）；
  - `install-worker.sh` 的 `_pick_bundled_python` 函数体（仿 `test_install_rollback_regression.py` 提取函数 + mock 环境运行）。

## Task List

### Phase 1: 打包端双架构解释器（方案 B）

- [ ] Task 1: 重构 `_find_python()` → `_find_pythons()`（按架构扫描 + uv --arch 补齐），新增红测试（假 UV_PYTHON_DIR 下按架构取目录、版本最高者优先）
  - Files: `src/worker_package.py`, `tests/test_worker_package.py`
  - Verify: `python3 -m pytest tests/test_worker_package.py -q`
- [ ] Task 2: `build_package()` 按 `python-<arch>/` 分目录捆绑 + `package_version()` 联合指纹，红测试（tar 含双架构目录）+ 绿实现
  - Files: `src/worker_package.py`, `tests/test_worker_package.py`
  - Verify: 同上 + `test_build_package_contents` 兼容更新

### Phase 2: 安装端架构校验（方案 A）

- [ ] Task 3: `install-worker.sh` 前置 TARGET_ARCH 计算 + 3.5 步 `_pick_bundled_python` 双保险校验 + 兜底引导 + 日志打印，新增红测试（提取函数 harness：匹配/不匹配/旧包兜底三分支）+ 绿实现
  - Files: `scripts/install-worker.sh`, `tests/test_worker_install_arch_check.py`
  - Verify: `python3 -m pytest tests/test_worker_install_arch_check.py tests/test_worker_install_ux.py -q`

### Phase 3: 回归 + 收尾

- [ ] Task 4: 全量相关测试回归（worker_package / install ux / rollback / windows install）
  - Verify: `python3 -m pytest tests/test_worker_package.py tests/test_worker_install_ux.py tests/test_install_rollback_regression.py tests/test_issue_452_windows_install.py tests/test_windows_worker.py -q`

## Checkpoints

- [ ] Phase 1 完成：worker_package 相关测试全绿
- [ ] Phase 2 完成：install-worker.sh 静态守卫（全英文输出）与 harness 测试全绿
- [ ] 全部完成：自审查 5 轴 + 敏感信息增量扫描通过 → 建 PR

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| uv 不支持交叉架构安装请求 | ~~Low~~ 已实测排除 | ~~失败仅告警~~ QA S1 修正：`uv python install` 无 `--arch` 参数（uv 0.12.3 实测 `unexpected argument` rc=2）；改用官方完整请求串 `cpython-3.12-linux-<arch>-gnu`，任意 uv 版本可用，实测成功安装 cpython-3.12.13-linux-x86_64-gnu。失败路径仍有 returncode 检查 + log.warning（附 stderr 尾部） |
| uv 未安装（构建机无 uv） | Low | 现有机制：只打包已存在架构，`--version` 校验保护另一架构机器 |
| 双架构版本不一致 → wheels py 版本单一 | Low | 安装端在线兜底；文档注明建议同版本 |
| 旧主控下发旧包仍失败 | Med | 方案 A 引导提示升级主控/安装系统 Python |
| tar 包体积增大 | Med | 双解释器约 +4MB（子集裁剪），可接受；后续可评估分架构出包 |
| 重建路径版本戳与包内容不一致（QA R1） | ~~Med~~ 已修复 | `build_package()` 重建路径改用 `package_version(force=True)` 绕过 60s TTL 缓存，补装架构后强制重扫指纹 |

## Open Questions

- 无