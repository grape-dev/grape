# Spec: Issue #2227 — grape CLI 能力漂移守卫与部署线核验口径

> 权威 Spec 见 Issue #2227「📋 Spec Document」评论；本文为 coding 落地摘要。
> 目标分支：`arch-weekly-11-zj` | 评级：ready | 2026-11-12

## 目标

消除 worker 部署线 grape CLI 的**构建/能力漂移**：确保 `grape git-repo commits --sha <ref>`
可用，或在不可用时能被守卫显式检出，并让文档给出可执行的降级核验命令。

## 范围

### In Scope
- `scripts/install-worker.sh` 第 4.5 节安装后能力探测与告警（记录实际版本）。
- 新增 `scripts/check_grape_cli_drift.py`：`commits --sha` 能力漂移守卫。
- `scripts/check-all.sh` 接入守卫作为门禁（无 grape 环境显式 SKIP）。
- `help/api/10-git-repos.md` 口径校准：版本要求 + 降级核验命令。

### Out of Scope
- grape-cli 本体功能开发（上游已具备 `--sha`）。
- 现网 worker 逐台手工升级（由 sre/发布环节执行）。
- grape-skills 仓库内 grape-cli-reference/runbook 改写（拆伴随项）。

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 探测方式 | 解析 `git-repo commits --help` 是否含 `--sha` | 同版本不同构建也会漂移，版本号不可信 |
| 判定策略 | 无 grape = SKIP；有 grape 无 `--sha` = FAIL | 避免无 CLI 的 CI 误报，又能检出真实漂移 |
| 被测二进制 | `GRAPE_CLI_BIN`（默认 PATH 中的 `grape`） | 可对 worker venv 内二进制做离线核验 |
| install 失败处理 | 仅告警，不中断安装 | 守卫负责报警，worker 仍须上线 |

## 验收标准
- [x] 守卫缺 `--sha` 时 exit 1，输出实际版本与缺失项；具备时 PASS；无 grape 时 SKIP。
- [x] `scripts/check-all.sh` 已接入守卫。
- [x] `scripts/install-worker.sh` 安装后可探测并告警 `--sha` 缺失（含实际版本）。
- [x] CLI 文档注明 grape-cli >= 0.3.3 与降级核验命令（`pulls get` / `contents --ref` + `git log`）。