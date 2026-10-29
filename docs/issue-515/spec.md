# Spec: Issue #515 — worker 安装包支持 x86_64 Linux 接入

> 来源：Issue Agent 已发布 Spec（Issue 评论），本文件为 Coding 落地实现的需求文档。

## Objective

- 构建：worker 安装包原生支持 x86_64 Linux 一键接入——目标机无系统 Python>=3.10 时也能完成安装、注册与心跳。
- 价值：自建主控向 x86 机器接入 worker 的通路当前被完全阻断（aarch64 解释器在 x86 上 `cannot execute binary file: Exec format error` 提前退出）。
- 用户：自建 grape 主控的运维/开发人员。
- 成功标准：见 Acceptance Criteria。

## In Scope

- `scripts/install-worker.sh` 第 3.5 步包内解释器架构校验与兜底引导（方案 A，先行）；
- `src/worker_package.py` 分架构捆绑 standalone Python（`python-x86_64/` / `python-aarch64/`，方案 B）；
- 安装日志打印目标机架构与所选解释器/wheels 架构。

## Out of Scope

- 旧版本自建主控的升级推送（需主控升级后才能下发新包，仅文档提示）；
- 非 Linux 平台（win_amd64 已由 wheels 矩阵覆盖，不扩范围）。

## Technical Approach

### 方案概述

安装端先做架构校验保住在线兜底通路（A），打包端再补双架构解释器实现完整离线安装（B），两方案组合落地。

### 实现路径

1. `install-worker.sh`：
   - 脚本前置计算 `TARGET_ARCH`（`uname -m` 归一化：`x86_64|amd64 → x86_64`、`aarch64|arm64 → aarch64`），并在首日志打印目标架构；
   - 3.5 步选用包内解释器前，先按 `python-$TARGET_ARCH/` 目录选择（旧包格式 `python/` 兜底），再试运行 `--version` 双保险校验（免解析 ELF，覆盖交叉场景）；
   - 不匹配/不可执行 → 跳过包内解释器、打印明确提示并引导安装系统 Python≥3.10，确保「依赖离线失败→在线回退」路径可达；
   - 离线 wheels 安装成功后打印所选架构（pip 按平台标签自动匹配）。
2. `worker_package.py`：
   - `_find_python()` 重构为按架构查找 `_find_pythons()`，返回 `{arch: py_root}`；缺架构时 `uv python install 3.12 --arch <arch>` 显式指定补齐；
   - 打包时按 `python-x86_64/`、`python-aarch64/` 分目录捆绑（保留旧 `python/` 单目录兼容逻辑移除——安装端旧包格式仅作读取兜底）；
   - `package_version()` 的 python 指纹改为双目录联合指纹。
3. 日志：安装全程打印 `[arch] x86_64/aarch64` 与所选解释器/wheels 架构。

### 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 实施顺序 | 仅A / 仅B / A+B | A 先行、B 跟进 | A 最小改动即刻解除阻断；B 补完整离线能力 |
| 架构校验方式 | uname -m 比对 / 试运行 | 试运行 `--version` + uname -m 双保险 | 免解析 ELF，覆盖交叉场景 |
| uv 目录名解析 | 版本段固定 / 扫描架构 token | 扫描 `-` 分段中的架构别名 | 兼容 `cpython-3.11.15-linux-aarch64-gnu`（旧）与 `cpython-3.12.8-x86_64-unknown-linux-gnu`（新）两种命名 |
| wheels 下载 py 版本 | 单一版本 / 每架构一版 | 单一版本（取各架构最高） | 双架构通常同版本（uv 同请求安装）；版本不一致属边缘场景，安装端在线兜底 |

## Affected Modules

| 模块 | 改动类型 | 说明 |
|------|---------|------|
| scripts/install-worker.sh | 修改 | TARGET_ARCH 前置计算、3.5 步架构校验、按 arch 选目录、兜底引导、日志 |
| src/worker_package.py | 修改 | _find_pythons() 分架构双目录、uv --arch 显式指定、双目录打包与指纹 |
| docs/issue-515/*.md | 新增 | 本文档 + plan.md + task.md |
| tests/test_worker_package.py | 修改 | 双架构目录打包/指纹守卫测试 |
| tests/test_worker_install_arch_check.py | 新增 | install-worker.sh 架构校验分支测试 |

## Acceptance Criteria

- [ ] x86_64 Linux（无系统 Python>=3.10）执行标准 install 命令成功接入并完成注册心跳；
- [ ] aarch64 现有 13 台 worker 升级路径回归不受影响；
- [ ] 安装日志明确打印目标机架构与所选解释器/wheels 架构；
- [ ] 所有现有测试通过；新增测试覆盖架构选择与兜底分支。

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 安装包体积翻倍（双解释器） | Med | 评估分架构出包 vs 同包双目录，必要时仅出双包 |
| 老主控下发旧包仍失败 | Med | 文档注明主控需升级；方案 A 兜底在线安装 |
| aarch64 回归风险 | Med | 回归验证 13 台现有 worker 升级路径；安装端旧 `python/` 目录兜底 + `--version` 校验保护 |

## Open Questions

- 无（VOD 已完成根因核验与需求收敛，详见Issue 评论）