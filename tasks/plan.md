# Implementation Plan: Issue #242 — 跨仓库需求开发

## Overview

扩展现有场景模型，将单仓库绑定升级为「主仓库 + 关联仓库」模式。在场景数据模型、API 路由、A2A handoff、repogit worktree、PR 提交机制和前端 UI 各层支持多仓库并行操作。

## Architecture Decisions

1. **linked_repos 存储方式**：JSON 字段 `linked_repos`（TEXT），存储 `[{repo_key, branch}]` 数组。与现有 `advanced_options` JSON 字段保持一致，避免 JOIN 开销。
2. **多仓库 worktree 隔离**：每个 repo 独立 worktree（repogit 已按 repo_key 隔离，扩展即可）。
3. **PR 提交触发方**：coding agent 自动遍历 linked_repos 中有改动的仓库分别提交。
4. **分支策略**：各仓库独立分支名（用户在 linked_repos 中为每仓库指定）。

## Task List

### Phase 1: 数据模型 + API 基础
- [x] Task 1: DB schema 迁移 — devops_pipeline_scene 新增 linked_repos 列
- [x] Task 2: pipeline_config.py 查询/创建/更新 linked_repos
- [x] Task 3: scenes.py Request 模型 + CRUD 路由 + 高级选项 API

### Phase 2: A2A + Runtime 扩展
- [x] Task 4: webhook.py / event_pipeline.py — A2A dispatch 注入 linked_repos
- [x] Task 5: repogit.py — 多 repo_key bare mirror + worktree 支持
- [x] Task 6: agent.py — coding agent 多仓库 worktree 初始化

### Phase 3: 前端 + 测试
- [x] Task 7: 前端场景详情页关联仓库配置 UI
- [x] Task 8: 测试覆盖

### Checkpoint: Complete
- [x] 所有测试通过
- [x] 自审查通过
- [x] PR 创建

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 多仓库并行操作资源开销 | Med | 限制关联仓库数量上限（5 个） |
| 关联仓库 token 权限不足 | Med | A2A 前校验，不足时标注跳过 |
| DB schema 变更兼容性 | Low | linked_repos 为新增可空字段，向后兼容 |
