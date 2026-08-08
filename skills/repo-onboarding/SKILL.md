---
name: repo-onboarding
version: 1.0.0
description: |
  项目首次托管引导（通用）：分析项目功能特性、使用方式、技术栈，
  创建「快速部署体验」与「探索方向引导」两个 Issue，帮助开发者快速上手模板项目。
  仅在模板一键创建并托管后异步触发一次。
triggers: []
tools:
  - codegraph
---

# 项目首次托管引导（repo-onboarding）

## 目标
帮助仓库 owner 快速了解本模板项目，并推动其上手体验与二次开发。

## 执行步骤（必须按序）

1. **建立项目知识基线**（参考 issue-advance）
   - 用 codegraph 分析项目结构，生成 `project-graph.md` + `project-capabilities.md`
   - 提炼：技术栈、核心模块、功能特性、启动/部署方式

2. **Issue 1 · 快速部署体验**
   - 创建 Issue，标题形如「【体验】快速部署并体验本项目的核心功能」
   - 内容：部署步骤摘要（启动命令/环境要求）、体验路径（如何跑通核心流程）、预期效果
   - assignee = 仓库 owner

3. **Issue 2 · 探索方向引导**
   - 基于知识基线，提炼 2-4 个「有意思、易理解、有真实价值」的探索方向
   - 每个方向：一句话价值 + 期望能力 + 影响范围
   - 汇总成一个 Issue，标题形如「【探索方向】值得探索的 N 个方向」
   - assignee = 仓库 owner

4. **生成任务**（对每个 Issue）
   - 调用 `POST /api/tasks/notify`，`assignee=owner`，`task_description` 简要描述
   ```bash
   curl -X POST {{SERVER_URL}}/api/tasks/notify \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "assignee": "<owner 的 GitCode 用户名>",
       "repo_key": "'"$REPO_FULL_NAME"'",
       "issue_number": "<Issue 号>",
       "task_description": "<该 Issue 的简要任务描述>"
     }'
   ```
   - 若返回 `{ok:false}`，在 Issue 评论说明

5. **输出引导摘要**
   - 在仓库 README 顶部追加一段「🤖 本仓库已托管，可体验以下内容...」或第一个 Issue 中写明

## 硬约束（必须遵守）
- 只用 `$BOT_GITCODE_TOKEN`；不读 Grape 内部数据（`data/workspaces` 项目知识文档除外）
- 不修改代码、不建 PR；仅建 Issue + 分配 + 通知
- assignee 必须是"人"（owner），不是 bot 账号
