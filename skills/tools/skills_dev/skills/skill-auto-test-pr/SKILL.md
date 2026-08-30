---
name: skill-auto-test-pr
description: |
  事件驱动的华为云 Skill 自动化测试与 PR 提交流水线。huawei-cloud-skill-creator 创建完新 skill 后通过 webhook 通知本流水线，自动执行 huawei-cloud-skill-tester 测试，测试完成后自动向 GitCode 仓库提交 Merge Request，并更新 t_skills_dev_flow 的开发节点状态。触发词：skill-auto-test-pr、自动测试PR、skill 测试提交、skill-created。
---

# Skill Auto Test & PR Pipeline

## 概述
事件驱动的华为云 Skill 自动化测试与 PR 提交流水线。`huawei-cloud-skill-creator` 创建完新 skill 后通过 webhook 通知本流水线，自动执行 `huawei-cloud-skill-tester` 测试，测试完成后自动向 GitCode 仓库提交 Merge Request，并更新 `t_skills_dev_flow` 的开发节点状态。

## 触发条件
- `huawei-cloud-skill-creator` 完成六阶段创建后，POST webhook 携带 `skill_name`
- Webhook 触发 Autopilot → agent 从 AGENTS.md 的 Trigger payload 读取 `eventPayload.skill_name`

## 工作流程
1. **接收事件**：webhook 携带 `skill_name` 触发 Autopilot
2. **执行测试**：调用 `huawei-cloud-skill-tester` 的 `run-test-pipeline.sh` 对新 skill 执行完整测试
3. **提交 PR**：clone GitCode 仓库 → 创建分支 → 复制 skill → push → 调用 API 创建 Merge Request
4. **更新状态**：PR 创建成功后更新 `t_skills_dev_flow.node_dev_status = done`，失败则 `failed`

## 使用方式

### 方式一：手动触发（指定 skill 名称）
```bash
bash ~/.agents/skills/skill-auto-test-pr/scripts/process-skill.sh <skill-name>
```

### 方式二：通过 multica autopilot webhook 触发（事件驱动，推荐）
`huawei-cloud-skill-creator` 创建完成后自动触发，无需手动干预。

手动调用 webhook 示例：
```bash
curl -X POST ${AUTOPILOT_WEBHOOK_URL} \
  -H "Content-Type: application/json" \
  -d '{"event":"skill-created","skill_name":"<skill-name>"}'
```

## Multica 集成
- **Agent**: `skill-pipeline-agent` (ID: `${AGENT_ID}`, 用 `multica agent list` 查)
- **Autopilot**: `skill-auto-test-pr-pipeline` (ID: `${AUTOPILOT_ID}`, 用 `multica agent list` 查)
- **Webhook Trigger**: skill-created-webhook（事件驱动，payload 中的 `eventPayload.skill_name` 指定目标 skill）

## 配置
- **目标仓库**: `git@gitcode.com:developer-skill/huaweicloud-skills-dev`
- **GitCode API**: `https://api.gitcode.com/api/v5`
- **PR 目标分支**: `main`
- **数据库**: 见 `.env` 的 `DB_HOST` / `DB_NAME`（更新 `t_skills_dev_flow.node_dev_status`）

### GitCode 仓库动态解析（默认按 issue 来源仓库）
`run-pipeline.sh` 解析目标仓库的优先级：
1. **DB `t_skills_dev_requirement.gitcode_issue_url`**（最权威，记录 GitCode issue 所在仓库）
2. **issue 描述 `**来源仓库：** <owner>/<repo>`**（GitCode issue webhook 投递时写入）
3. **默认仓库**（下表，直接创建的 issue 或无来源字段时回退）

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `GITCODE_REPO_SSH` | (必填, 见 .env) | SSH clone 地址 |
| `GITCODE_API` | `https://api.gitcode.com/api/v5` | API base（换 GitCode 实例才改） |
| `GITCODE_TOKEN` | （内置） | GitCode API token |
| `GITCODE_OWNER` | (必填, 见 .env) | 仓库 owner/namespace |
| `GITCODE_REPO` | `huaweicloud-skills-dev` | 仓库名 |
| `GITCODE_CHECKOUT` | `huaweicloud-skills-dev` | workspace 中 checkout 目录名（须与仓库名一致） |

设置方式（agent 环境或 autopilot instructions）：
```bash
export GITCODE_REPO_SSH="git@gitcode.com:<owner>/<repo>"
export GITCODE_OWNER="<owner>"
export GITCODE_REPO="<repo>"
export GITCODE_CHECKOUT="<repo>"
```
> 切换仓库后还需同步：新仓库的 webhook 配置（→ autopilot `awt_tAhI8o3bk-...`）、数据库中 `t_skills_dev_requirement` 的归档路径/PR 关联。

## 文件结构
```
scripts/
  process-skill.sh     # 入口：接收 skill_name 直接执行完整 pipeline（test + PR + DB更新）
  run-pipeline.sh      # 主流水线脚本（test + PR + DB更新）
```

## 评论汇报规范（Comment Delivery Rules, 2026-08-13）

**目的：** 避免评论/输出超限导致汇报丢失（SKI-49/50/51 实际事故：自动投递超 ~8KB 只发占位提示，原始输出不显示在 issue 上）。

1. **主动发评论，不依赖自动投递：** 流水线完成后，必须用 `multica issue comment add <issue-id> --content "<短摘要>"` 主动发布完成评论（含 PR 链接）。不要依赖 run 结束时的自动输出投递——它有大小限制，超限只发占位符。
2. **正文 = 结论五件套（≤1KB）：** (1) 结论（测试通过/PR 已提交/分发完成），(2) PR/链接，(3) 改动要点 3-5 条，(4) 验证结果（X/Y 通过），(5) 状态流转（DB 节点 / issue 状态）。
3. **长报告走 md 附件：** 需要完整细节（完整测试输出、逐文件变更、日志）时，写入 `<workdir>/report-<skill>.md`，挂到评论：`multica issue comment add <issue-id> --content "<短摘要>" --attachment <workdir>/report-<skill>.md`。附件无阈值限制（实测 990KB md 可上传），web 端以可下载卡片展示。
4. **单条 text 消息 ≤2000 字符，禁止过程叙述：** "Now let me... / 让我检查..." 之类的过程话术放 thinking，不要作为 text 消息输出——所有 text 消息都会拼进最终自动投递内容，叙述过多会累积超限。
5. 若仍超限：确保 PR 和 DB 状态更新先完成（步骤 1 优先），再补发短评论。

## 已知问题（Known Issues）

### 0. 已修复：tester Phase 0 删源 & 重复 PR 创建

- **tester Phase 0 卸载会删除 skill 源**（当 `~/.agents/skills/<skill>` 即安装目录时）：已在 `phase-0-install-check.sh` 增加源快照保护（`skill_dir == target_dir` 时先 `cp` 到 mktemp，卸载后从快照重装/恢复），并在 `utils.sh cleanup_after_test` 增加"安装目标==被测源目录则跳过卸载"守卫。**保留源 + 无需再手动设 `SKILL_INSTALL_DIR` 隔离**。
- **重复 PR/分支**：`run-pipeline.sh` Phase 2 已幂等化——创建前先查该分支是否已有 PR（`head.ref == branch`），有则直接复用（跳过 push + 创建）；分支已在远端但无 PR 时 fetch + 更新内容再 push（push 失败自动 `--force` 重试）。不再产生历史那种同 skill 多 PR（如 ecs-list #96/97/98/99）。

### 1. skill-pipeline-agent 的 agent identity instructions 无法更新（multica 服务端 404）

- **现象**：`multica agent update ${AGENT_ID} --instructions ...` 返回 `The requested resource was not found` / 服务端 404 "agent not found"；API GET/PATCH/PUT 同样 404。但 `agent list` 能列出该 agent，autopilot 也能正常 assign 并运行。
- **影响**：AGENTS.md 中 `Agent Identity` 区块（旧版指令，仅 `process-skill.sh <skill_name>`，不含 `multica_issue_id`）与 `Autopilot instructions`（新版，含 issue id）并存。**实际执行时 autopilot instructions 优先级更高，agent 正确遵循新逻辑**（2026-07-31 run 9da809f8 已验证：读取 issue id、更新 `node_dev_status=done`、PR 关联 Fixes #）。当前不阻塞流程。
- **根治**：需联系 multica 平台管理员修复该 agent 的 get/update 端点，或删除重建 agent 后更新 autopilot 的 assignee。**待跟进**。
- **涉及**：agent `skill-pipeline-agent` (ID `${AGENT_ID}`)

### 2. 架构演进记录（历史，已解决）

- **2026-07-31 前**：快照对比 + watcher 轮询 + skill-sync cron（已全部移除）。
- **现架构**：全事件驱动——skill-creator Phase 7 POST webhook（携带 `skill_name` + `multica_issue_id`）→ autopilot 触发 → `process-skill.sh <skill_name> <issue_id>` → tester 测试 → PR（Fixes #）→ DB 更新。
- **关键修复**：
  - `fetch_skill_from_workspace()` 改为遍历所有 `huaweicloud-skills-dev` checkout 找含目标 skill 的那个（原 `find | head -1` 会误选无该 skill 的旧 checkout）。
  - tester Phase 0 卸载会删除 `~/.agents/skills` 下的 skill 源 → PR 阶段改为从 checkout 重新获取。
  - AK/SK 从 `~/.bashrc` 加载（agent daemon 不 source bashrc）。
  - PR 关联 issue：DB 查 `gitcode_issue_number` → body 追加 `Fixes #`。
  - cisco-ai-skill-scanner 已从 skill-creator Phase 6 安全审计移除（安装太慢导致 agent 被 idle watchdog 终止）。
