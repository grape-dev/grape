---
name: repo-onboarding
version: 1.2.0
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

## 创建 GitCode Issue（确定性 API，3A/3B 共用）

**唯一允许的端点**：

```bash
curl -s -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues" \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "<标题>",
    "body": "<内容>",
    "assignee": "<仓库 owner>",
    "labels": "体验"        # ⚠️ 必须是逗号分隔字符串（"体验" / "体验,探索方向"）
  }'
```

- ⚠️ **`labels` 必须是字符串**，禁止 JSON 数组（`["体验"]`）——数组会触发
  `400 Request body parsing error`，这是 GitCode v5 创建接口的已知行为。
- ⚠️ **禁止**尝试 `/api/v1/*`、`/api/v4/projects/*`、`gitcode.com/-/issues`、
  `/issues/new` 等旧版/网页接口——要么不存在（404）要么被 WAF 拦截（418）。
  不要在一堆错误端点上反复试错，认准上面这一个。
- 成功判定：响应 HTTP 200/201 且含 issue `id`/`number`；失败（400/401/403/404/418/5xx）
  视为**致命错误**：立即以非 0 退出任务并报错，禁止降级、禁止假装完成。

## 执行步骤（必须按序）

1. **建立项目知识基线**（⚠️ 必须先执行，3A/3B 都依赖它）
   - 读取 `references/codegraph-project-analysis.md`，按其流程用 codegraph 分析项目结构
   - 产出直接写到共享位置 `data/workspaces/{owner}/{repo}/project-info/`：`project-graph.md` + `project-capabilities.md`（供 VOD / issue-advance 复用）
   - 提炼：技术栈、核心模块、功能特性、启动/部署方式
   - codegraph 不可用时按该参考的降级方案（README + GitCode API），产物标注 `[degraded]`，不阻塞引导。**降级只影响知识条线，不影响创建 GitCode Issue（步骤 3 的 API 命令无任何前置依赖）**

3A. **快速部署体验（experience）**
   - 创建 **1 个** Issue，标题「【体验】快速部署并体验本项目的核心功能」
   - 内容：部署步骤摘要（启动命令/环境要求）、体验路径（如何跑通核心流程）、预期效果
   - assignee = 仓库 owner，labels = "体验"（字符串）

3B. **探索方向引导（explore）**
   - 基于知识基线，提炼 2-4 个「有意思、易理解、有真实价值」的探索方向
   - 每个方向：一句话价值 + 期望能力 + 影响范围
   - 创建 **1 个** 汇总 Issue，标题形如「【探索方向】值得探索的 N 个方向」
   - assignee = 仓库 owner，labels = "探索方向"（字符串）

4. **生成任务**
   - 调用 `POST /api/tasks/notify`，`assignee=owner`，`task_description` 简要描述
   ```bash
   curl -X POST {{SERVER_URL}}/api/tasks/notify \
  -H "Authorization: Bearer $BOT_G...OKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "assignee": "<owner 的 GitCode 用户名>",
       "repo_key": "'"$REPO_FULL_NAME"'",
       "issue_number": "<Issue 号>",
       "task_description": "<该 Issue 的简要任务描述>"
     }'
   ```
   - 若返回 `{ok:false}`，在 Issue 评论说明（不改变整体成功判定）

5. **写入引导结果**（必须，系统据此建立 Issue 阶段）
   - 创建的 Issue 成功后，在 `$ONBOARDING_RESULT` 指向的文件写入：
     ```json
     {"number": 1, "title": "【体验】快速部署并体验本项目的核心功能", "assignee": "<owner>", "target": "experience"}
     ```
   - `number` 填 GitCode 实际返回的 Issue 号（新模板仓库编号从 1 递增：体验 #01、探索 #02）。
   - `$ONBOARDING_RESULT` 由系统注入（=`data/workspaces/{owner}/{repo}/onboarding-agent/onboarding-result-{target}.json`），直接写该文件，不新建路径。
   - **⚠️ 失败语义（必须遵守）**：创建 Issue 失败 = 整个任务失败。禁止写结果文件、禁止输出「已完成/成功」、禁止用知识基线/README 等替代。必须立即以非 0 退出，让系统记录为 failed
6. **输出引导摘要**
   - 在仓库 README 顶部追一段「🤖 本仓库已托管，可体验以下内容...」或写在该 Issue 中

## 硬约束（必须遵守）
- 只用 `$BOT_GITCODE_TOKEN`（或 prompt 注入的 `$USER_GITCODE_TOKEN`）；不读 Grape 内部数据（`data/workspaces` 项目知识文档除外）
- 创建 Issue 只走步骤 3 给的那一个 v5 端点；在哪里失败就在哪里以非 0 退出，禁止换端点死磕、禁止静默降级
- 允许使用 codegraph 工具与 `data/workspaces/{owner}/{repo}/project-info/` 知识文档
- 不修改代码、不建 PR；仅建 1 个 Issue + 分配 + 通知
- assignee 必须是"人"（owner），不是 bot 账号
