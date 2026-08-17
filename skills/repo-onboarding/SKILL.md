---
name: repo-onboarding
version: 1.1.0
description: |
  项目首次托管引导（通用）：分析项目功能特性、使用方式、技术栈。
  根据 $ONBOARDING_TARGET 指引创建对应类型的单个引导 Issue——
  experience=「快速部署体验」需求；explore=「探索方向引导」诉求。
  仅在模板一键创建并托管后异步触发（experience / explore 各执行一次）。
tools:
  - codegraph
---

# 项目首次托管引导（repo-onboarding）

## 目标
根据 `$ONBOARDING_TARGET` 创建对应类型的引导 Issue，帮助开发者快速上手模板项目。

## 目标类型判定（必须最先）
- 读环境变量 `$ONBOARDING_TARGET`：
  - `experience` → 执行「快速部署体验」分支（步骤 3A）
  - `explore` → 执行「探索方向引导」分支（步骤 3B）
  - 其他/缺失 → 默认按 `experience` 处理并在日志/结果中说明

## 执行步骤（必须按序）

1. **建立项目知识基线**（⚠️ 必须先执行，3A/3B 都依赖它）
   - 读取 `references/codegraph-project-analysis.md`，按其流程用 codegraph 分析项目结构
   - 产出写到共享位置 `data/workspaces/{owner}/{repo}/project-info/`：`project-graph.md` + `project-capabilities.md`（供 VOD / issue-advance 复用）
   - 提炼：技术栈、核心模块、功能特性、启动/部署方式
   - codegraph 不可用时按该参考的降级方案（README + GitCode API），产物标注 `[degraded]`，不阻塞引导

2. **判定目标类型**（读 `$ONBOARDING_TARGET`，见上）

3A. **快速部署体验（experience）**
   - 创建 **1 个** Issue，标题形如「【体验】快速部署并体验本项目的核心功能」
   - 内容：部署步骤摘要（启动命令/环境要求）、体验路径（如何跑通核心流程）、预期效果
   - assignee = 仓库 owner

3B. **探索方向引导（explore）**
   - 基于知识基线，提炼 2-4 个「有意思、易理解、有真实价值」的探索方向
   - 每个方向：一句话价值 + 期望能力 + 影响范围
   - 创建 **1 个** 汇总 Issue，标题形如「【探索方向】值得探索的 N 个方向」
   - assignee = 仓库 owner

4. **生成任务**
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

5. **写入引导结果**（必须，系统据此建立 Issue 阶段）
   - 创建的 Issue 成功后，在 `$ONBOARDING_RESULT` 指向的文件写入：
     ```json
     {"number": 1, "title": "【体验】快速部署并体验本项目的核心功能", "assignee": "<owner>", "target": "experience"}
     ```
   - `number` 填 GitCode 实际返回的 Issue 号（新模板仓库编号从 1 递增：体验 #01、探索 #02）。
   - `$ONBOARDING_RESULT` 由系统注入（=`data/workspaces/{owner}/{repo}/onboarding-agent/onboarding-result-{target}.json`），直接写该文件，不新建路径。
   - 若创建失败：不写文件（主事件仍 completed，由系统以"无结果"处理）。

6. **输出引导摘要**
   - 在仓库 README 顶部追加一段「🤖 本仓库已托管，可体验以下内容...」或在该 Issue 中写明

## 硬约束（必须遵守）
- 只用 `$BOT_GITCODE_TOKEN`；不读 Grape 内部数据（`data/workspaces` 项目知识文档除外）
- 允许使用 codegraph 工具分析项目结构，允许在 `data/workspaces/{owner}/{repo}/project-info/` 下创建/更新知识基线文档
- 不修改代码、不建 PR；仅建 1 个 Issue + 分配 + 通知
- assignee 必须是"人"（owner），不是 bot 账号
