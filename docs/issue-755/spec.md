# Spec: Issue #755 — 跨仓 A2A 转交后 issue_number 在目标仓 VCS 404

> 本文档为 Coding 侧实施规格（正式 Spec 由 Issue Agent 发布于 Issue 评论，此处为开发基线摘要）。

## 目标

跨仓 A2A 转交后，目标任务携带的 issue_number 能在目标仓 VCS 解析到真实落地 Issue，消除下游 Agent 全流程 404。

## 现状缺陷

- 源仓（如 grape-skills#1099）A2A 转交到目标仓（grape-dev/grape）时，payload.issue_number 沿用源仓单号；
- 目标仓 VCS 对该编号 404，`event_pipeline.resolve_issue_id` 提取后注入任务 `ISSUE_NUMBER=1099`；
- issue-cli runbook Step 1 读上下文必 404，下游 Agent 需自行发现真实落地单（VOD 自动建为 #753/#754/#755）。

## 方案

方案 A（主）+ 方案 B（辅）：

- **方案 A — 单号回填**：A2A 入口与 `_cross_pipeline_handoff` 在派发前将源仓单号解析/回填为目标仓真实落地单号（carried_context.target_issue_number / DB 反查 carried_context.source_issue_number），下游注入链路自然生效。
- **方案 B — 入口校验兜底**：`a2a_endpoint` 对 issue_number 做目标仓存在性校验（DB 快查优先 + VCS API 兜底），明确 404 → 400 拒绝并提示转交建单状态。

## 修改位置

| 模块 | 改动 |
|------|------|
| `src/api/routes/webhook.py` — 新增 `_issue_exists_in_db` / `_resolve_cross_repo_issue_number` / `_a2a_issue_vcs_check` 三个 helper | 回填/校验核心逻辑 |
| `src/api/routes/webhook.py` — `a2a_endpoint` | 入口回填（repo_key 解析后）+ 鉴权后 VCS 存在性校验（400） |
| `src/api/routes/webhook.py` — `_cross_pipeline_handoff` | 场景校验后做单号回填；carried_context 持久化 target_issue_number |
| `src/api/event_pipeline.py` — `resolve_issue_id` | payload 单号与 carried_context.source_issue_number 一致且 target_issue_number 存在时取真实单号（防御未回填 payload） |

## 验收标准

- [ ] 跨仓 A2A 转交后，目标仓 Agent 用 ISSUE_NUMBER 调 `git-repo issues get` 命中真实落地 Issue（非 404）
- [ ] carried_context 包含 source_issue_number / target_issue_number 配对字段
- [ ] A2A 入口对 issue_number 不存在于目标仓 VCS 时返回 400 并提示转交建单状态
- [ ] 编号映射兜底：DB 查无 → VCS API 校验 → 仍无则 400 拒绝
- [ ] 现有测试通过 + 新增测试覆盖上述场景
