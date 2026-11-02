# Spec: Issue #736 — git-repo contents --raw 返回 base64 与命名语义不符

> 承接 Issue #736 已发布 Spec（2026-11-02 20:07，评级 ready，标签 bug/P2/spec-ready）。
> 本文档为跨流水线移交（马岩松的专属开发流水线 vod→issue→coding→qa）后的开发基线记录。
> Target Branch: weekly-08-mys（场景注入的开发基线；既有 Spec 原文 weekly-09 为原流水线时点值，以注入值为准）。

## Objective
- `grape git-repo contents --path <p> --raw` 直接输出解码后的明文文件内容，与 `--raw` 命名语义一致。

## 现状
- 服务端 `/api/git-repo/{owner}/{repo}/contents/{path}` 返回 base64 `content` 字段。
- 旧版 CLI `--raw` 原样透传 JSON（含 base64），用户需 `jq -r .content | base64 -d` 两步解码；文档未标注语义。

## 方案（沿用已发布 Spec 首选路径）
- **CLI 输出层解码（首选）**：`grape-cli` `src/registry.py` 对 `gitrepo_contents` 的 `--raw` 分支 base64 解码后直出明文；非 UTF-8（二进制）内容按原始字节输出（不崩溃）；非法 base64 回退 JSON 输出（行为有定义）。
- **兼容**：新增 `--base64` 显式参数原样输出 base64 `content` 字段，保留旧能力。
- **文档同步（兜底）**：`--help` 帮助文本、README（EN/ZH）、grape-skills `issue-cli` 的 `grape-cli-reference.md` / `issue-runbook.md` / `issue-developability-analysis.md` 同步标注 `--raw` 明文语义。

## 涉及仓库
| 仓库 | 改动 |
|------|------|
| grape-dev/grape-cli | `src/registry.py` --raw 解码/二进制字节输出/非法回退 + `--base64` 参数；`--help` 文案；README EN/ZH；`test/test_registry.py` 新增 3 个测试 |
| grape-dev/grape-skills | issue-cli 技能 3 个 reference 文档标注 `--raw` 明文语义 |
| grape-dev/grape | 本目录 spec/plan/task 文档（流程产物） |

## Acceptance Criteria
- [x] `contents --path README.md --raw` 直接输出可读明文。
- [x] `--help` 与 reference 文档的 `--raw` 描述与实际行为一致。
- [x] 二进制/非 UTF-8 文件 `--raw` 不崩溃，按原始字节输出（有测试）。
- [x] runbook 降级链路示例无隐含两步解码（已标注 --raw 直出明文）。
- [x] 现有测试通过；新增测试覆盖 `--raw` 解码输出、二进制、非法 base64、`--base64`。

## 风险与缓解
- 既有脚本依赖 base64 输出 → 提供 `--base64` 显式参数（旧行为保留）。
- 大文件解码性能 → 与现状一致，无新增放大。
