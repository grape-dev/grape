# Spec — Issue #1884 去掉 install-worker.sh 的 hermes 自动安装分支

> 决策：B 档（用户评论 199710329 确认，Spec 见评论 199711038）。目标分支 `local-liufei`。
> 前置分支 `fix/install-engine-default-none`（8da9e3ca4）被 B 档整体删除覆盖，建议作废、勿并行合入。

## Objective

移除 `scripts/install-worker.sh` 的 `--engine` 参数与整套自动安装逻辑（`install_engine`），脚本不再安装任何 agent 引擎。

原因：`pip install 'hermes[acp]'` 装到的是 PyPI 同名无关项目（科研元数据发布工具），且仅凭 `[ -x "$VENV/bin/hermes" ]` 判定成功 → "装错 + 误报成功"。

## Scope

### In Scope

1. `scripts/install-worker.sh`
   - 删除头注释 usage 中 `[--engine auto|opencode|hermes|none]`、第 5 步 "best-effort 安装执行引擎" 描述与第 23-24 行引擎说明注释；
   - 删除 `ENGINE` 变量、`--engine)` 参数分支、`case "$ENGINE"` 取值校验、Unknown argument usage 文案中的 `[--engine ...]`；
   - 删除 5.7 段 `install_engine()` 函数定义与调用（含 `npm install -g opencode-ai` 与 `pip install 'hermes[acp]'` 两处安装）；
   - 传入 `--engine` 时落入 `Unknown argument` 分支，退出码 1（现有 `*)` 分支天然满足）。
2. `help/usage/faq.md`（Q3「安装引擎」小节）
   - 删除 `--engine auto|opencode|hermes|none` 说明行；
   - 删除 `pip install 'hermes[acp]'` 建议（PyPI 同名包与目标产品无关）；
   - 保留 opencode（`npm install -g opencode-ai`）与 codearts 自行安装指引，统一为"用户自行安装，worker 启动自动发现并注册"口径。
3. `tests/test_worker_install_ux.py`
   - 新增静态断言：脚本无 `install_engine|--engine|_want_hermes|hermes[acp]|npm install -g opencode-ai` 痕迹（Issue #1884 验收 1）；
   - 新增行为断言：`bash scripts/install-worker.sh --engine hermes` → `Unknown argument` + 退出码 1（Issue #1884 验收 2）。

### Out of Scope（明确保留，不改）

- worker 运行态引擎自动发现 + 周期重注册逻辑（`src/runtime/agent.py` 等）；
- codearts 目录信任预配置段（install-worker.sh 5.8 `provision_codearts_trust`）；
- `scripts/install-worker.ps1`（本就不安装引擎）；
- `docs/` 历史设计文档（issue-471/499 等历史记录不回写）；
- 5.9 codegraph 预装段（分析引擎，非 agent 执行引擎，Issue #666 范畴）。

## Technical Approach

### 实现路径

1. **install-worker.sh**（约 -60 行）：
   - 头注释：流程第 5 步改写为 "5. codearts 目录信任预配置（无 TTY 不再因 folder-trust 闸门致命退出）"（原 5.8 上移语义）；删除 `[--engine ...]` usage 行与第 23-24 行引擎注释；
   - 参数解析：删 `ENGINE="${ENGINE:-auto}"`、`--engine)` 分支；Unknown argument 的 usage 去掉 `[--engine ...]`；
   - 删 `case "$ENGINE" in ... esac` 校验段；
   - 删 5.7 整段（`install_engine()` + 调用行）。
2. **faq.md**：Q3 引擎小节三条改写：opencode 保留 npm 命令；hermes 改为"按 Hermes Agent 官方渠道自行安装"（不给 pip 命令）；删除 `--engine` 行；补充"安装脚本不再安装引擎"说明。
3. **测试**：`tests/test_worker_install_ux.py` 新增 `TestNoEngineInstallInScript`（文本断言，沿用现有模式）+ `TestEngineArgRejected`（subprocess 实跑 `--engine hermes` 断言退出码 1 与 stderr 含 `Unknown argument`）。

### 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 移除范围 | A 仅删 hermes 段 / B 整体删 --engine | B | 用户确认；与 Windows ps1 行为一致；彻底消除"平台帮装引擎"的错误承诺 |
| hermes 安装指引 | 保留 pip 命令 / 只说官方渠道 | 只说官方渠道 | PyPI `hermes` 同名包与产品无关，不能再引导 pip 安装 |

## 测试 seam 声明

- seam 1（文本）：`scripts/install-worker.sh` 内容不含引擎安装关键字——已有 `test_worker_install_ux.py` 的静态断言模式（`_sh()` 读取）；
- seam 2（行为）：脚本 CLI 对未知参数的公开行为（`Unknown argument` + exit 1）——subprocess 实跑，不依赖内部实现。

## 验收标准（与 Issue 评论 199710329 一致）

1. `grep -nE "install_engine|--engine|_want_hermes|hermes\[acp\]|npm install -g opencode-ai" scripts/install-worker.sh` 无输出；
2. 带 `--engine hermes` 执行 → `Unknown argument`，退出码 1；
3. 不带该参数执行时脚本不产生任何 npm/pip 引擎安装动作（无引擎为正常状态）；
4. `help/usage/faq.md` 不再出现 `--engine` 与 `hermes[acp]`；
5. `tests/test_worker_install_*` 全绿；新增断言覆盖第 1、2 条。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 外部脚本依赖 `--engine opencode` 自动安装 | 中 | faq.md 已给 `npm install -g opencode-ai` 替代命令，PR body 标注 |
| 误删运行态引擎发现逻辑 | 高 | Out of Scope 锁定；测试断言只覆盖安装段关键字 |
| 与 fix/install-engine-default-none 冲突 | 低 | 该分支作废（B 档覆盖其改动），PR body 说明勿并行合入 |
