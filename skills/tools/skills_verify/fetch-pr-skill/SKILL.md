---
name: fetch-pr-skill
description: 给定 GitCode PR URL，自动拉取 PR 中变更的完整 skill 到本地，并通过 Agent CLI 在独立 session 中运行该 skill
---

# Fetch PR Skill — 从 PR 自动拉取完整 Skill

给定一个 GitCode PR 链接，自动 clone 仓库、checkout PR 分支、识别变更的 skill 目录，并将**完整的 skill**（不是 diff）复制到本地。

## 何时使用

- 用户给了一个 GitCode PR 链接，想获取其中变更的完整 skill
- 用户说「拉取这个 PR 的 skill」「从这个 PR 获取 skill」
- 用户需要基于 PR 中的 skill 进行二次开发或安装

## 工作流程

> **⚠️ 启动即置状态：** 加载本 skill 后，**立即**执行 `scripts/set_processing.sh`，
> 将 `node_verify_status` 和 `node_repo_status` 置为 `processing`。
> 这是评测流程的第一步，必须在任何其他操作之前完成。

### Step 1: 解析 PR URL

从用户提供的 URL 中提取：
- 仓库 Owner（如 `developer-skill`）
- 仓库名（如 `huaweicloud-skills`）
- PR 编号（如 `277`）

支持的 URL 格式：
- `https://gitcode.com/<owner>/<repo>/pull/<number>`
- `https://gitcode.com/<owner>/<repo>/merge_requests/<number>`

### Step 2: Clone 仓库并 Fetch PR 分支

1. 检查本地是否已有仓库缓存（`/tmp/fetch-pr-skill/<owner>_<repo>`），有则 `git fetch`，无则 `git clone`
2. 使用 GitCode 的 GitLab 风格 ref 拉取 PR：
   ```bash
   git fetch origin refs/merge-requests/<PR_NUM>/head:pr-<PR_NUM>
   ```
3. Checkout 到 PR 分支

### Step 3: 识别变更的 Skill 目录

1. 通过 `git diff origin/main...HEAD --name-only` 获取 PR 变更文件列表
2. 从变更文件中找到包含 `SKILL.md` 的目录
3. 这些目录即为 PR 涉及的 skill 目录

### Step 4: 复制完整 Skill 到本地

1. 对每个识别到的 skill 目录，复制**整个目录**（包括未修改的文件）到目标路径
2. 默认目标路径为当前目录，用户可指定

### Step 5: 输出结果

报告：
- 拉取了哪些 skill
- 每个 skill 的文件数量
- 本地存放路径

### Step 6: 运行 Skill（通过 Agent CLI）

1. 调用 `run_skill.sh` 传入拉取到的 skill 路径
2. 脚本自动创建独立 session，复制 skill 快照
3. 通过适配器调用 Agent CLI 运行 skill
4. 运行结果（输出、状态、耗时）记录到 session 目录

### Step 7: 输出 Session 信息

报告：
- Session ID
- 运行状态（success/failed/timeout）
- 运行耗时
- 输出日志路径
- 查看详情命令：`bash scripts/session.sh show <session_id>`

### Step 8: 自动评测打分

1. 解析 session 日志提取原始指标（token、耗时、调用链、错误等）
2. 查询 DB 获取同 skill 的基线评测结果
3. 按 pingce.md 的 12 项评估规则打分
4. 首次运行直接给满分，后续与基线对比（10% 容忍度）
5. 安全性通过调用 huawei-cloud-skill-audit skill 评估

### Step 9: 写入数据库

1. 写入 hd_eval_skill（skill 注册）
2. 写入 hd_eval_report（评测报告，含 4 维度分数）
3. 写入 hd_eval_detail（12 项评分明细）
4. 写入 hd_eval_sub_metric（原始指标值）
5. 写入 hd_eval_chain（调用链记录）

### Step 10: 资源释放

1. 从 session 日志中提取华为云资源 ID（ECS 实例、OBS 桶、VPC 等）
2. 调用华为云 API 释放这些资源
3. 删除 session 目录和 skill 副本
4. 如果云资源释放失败，发送邮件通知到 wangjie594@h-partners.com 和 hugai@huawei.com

### Step 11: 同步流水线验证状态

将评测状态同步到 SKILLS 开发验证流水线表 `t_skills_dev_flow`（由 `eval_skill.py` 自动完成，无需手动操作）：

1. 通过 `.multica/daemon_task_context.json` 获取当前任务的 `multica_issue_id`（优先从 cwd，其次从 skill 路径向上探测）
2. 评测开始时：`UPDATE t_skills_dev_flow SET node_verify_status='processing'`
3. 评测结束时：
   - 成功 → `node_verify_status='done'`
   - 失败 → `node_verify_status='failed'`
4. 同步更新字段：
   - `node_verify_time` = 当前时间（NOW()）
   - `node_verify_url` = 被评测的 GitCode PR URL（通过 `--pr-url` 传入；未传则回退 multica issue URL）
5. 状态白名单：`processing` / `done` / `failed`
6. 更新失败不阻塞主流程（仅告警）；`--dry-run` 模式也会更新 flow 状态（只跳过 hd_eval_* 写库）

### Step 12: PR 自动合入（共建仓库节点）

评测流程结束后（无论评测成功/失败），可选自动合入 PR 并删除源分支（由 `eval_skill.py` 自动完成，受开关控制）：

1. 触发条件（同时满足）：
   - 评测流程已执行（成功或失败都触发）
   - 开关 `AUTO_MERGE_PR=on`（环境变量，默认 off）
   - 提供了 `--pr-url`（GitCode PR URL）
2. 合入动作：调用 GitCode API `PUT /api/v5/repos/{owner}/{repo}/pulls/{number}/merge`，body `{"merge_method":"merge","prune_branch":true}`，合入后自动删除源分支
3. 同步更新 `t_skills_dev_flow` 表：
   - `node_repo_status`：processing（合入中）→ done（成功）/ failed（失败）
   - `node_repo_url`：合入后 MR 页 URL
   - `node_repo_time`：完成时间
4. 状态白名单：`pending` / `processing` / `done` / `failed` / `skipped`
5. 不合入时的 `node_repo_status` 更新语义：
   - 开关 `AUTO_MERGE_PR` 非 on → 不更新 repo 状态（保持原值）
   - `--dry-run` 模式 / `GITCODE_TOKEN` 未配置 / 未提供 `--pr-url` → 不执行合入、不更新 repo 状态

   合入结果：成功 → `done`，失败 → `failed`（合入 API 失败），评测结果不影响合入。
6. 需要环境变量：`GITCODE_TOKEN`（GitCode 访问令牌）

## 快速使用

### 方式一：直接运行脚本

```bash
# 基本用法
bash fetch-pr-skill/scripts/fetch_pr_skill.sh "https://gitcode.com/developer-skill/huaweicloud-skills/pull/277"

# 指定输出目录
bash fetch-pr-skill/scripts/fetch_pr_skill.sh "https://gitcode.com/developer-skill/huaweicloud-skills/pull/277" ./my-skills
```

### 方式二：在 Agent 中调用

在对话中说：

```
/fetch-pr-skill https://gitcode.com/developer-skill/huaweicloud-skills/pull/277
```

Agent 会自动执行上述流程并交付完整 skill。

### 方式三：运行已拉取的 Skill

```bash
# 使用默认 opencode 适配器运行
bash fetch-pr-skill/scripts/run_skill.sh ./huawei-cloud-skill-audit

# 指定 Agent、超时和输入
bash fetch-pr-skill/scripts/run_skill.sh ./huawei-cloud-skill-audit \
  --agent opencode \
  --timeout 600 \
  --input "分析这个 skill 的安全合规性"

# 查看运行 session
bash fetch-pr-skill/scripts/session.sh list huawei-cloud-skill-audit
bash fetch-pr-skill/scripts/session.sh show <session_id>
```

### 方式四：单独运行评测

```bash
# 对已有 session 运行评测
python3 fetch-pr-skill/scripts/eval_skill.py <session_id> --skill-name my-skill --dry-run

# 实际写入 DB
python3 fetch-pr-skill/scripts/eval_skill.py <session_id> --skill-name my-skill --agent opencode
```

## 质量检查清单

- [ ] PR URL 格式正确且可解析
- [ ] 仓库可访问（公开仓库或已配置认证）
- [ ] PR 分支成功 fetch 和 checkout
- [ ] 至少识别到一个包含 SKILL.md 的目录
- [ ] 复制到本地的 skill 目录结构完整
- [ ] Session 目录结构完整（meta.json, output.log, skill/）
- [ ] result.json 状态和耗时记录正确
- [ ] Skill 快照与原始 skill 一致
- [ ] 评测 12 项评分计算正确
- [ ] DB 5 张表写入成功
- [ ] 首次运行给满分，后续与基线对比

## 使用示例

### 示例 1：拉取单个 skill

输入：
```
https://gitcode.com/developer-skill/huaweicloud-skills/pull/277
```

输出：
```
==> Found skill directories:
    - skills/devtools/common/huawei-cloud-skill-audit

==> Copying complete skill -> ./huawei-cloud-skill-audit
    Copied 22 files.

==> Done! Skills copied to: .
```

### 示例 2：PR 只改了 skill 中的一个文件

即使 PR 只修改了 `SKILL.md` 一个文件，checkout 后复制的仍然是整个 skill 目录（包括 scripts/、references/ 等所有文件），因为 `git checkout` 是基于整个分支快照。

### 示例 3：拉取并运行 skill

```bash
# Step 1: 拉取
bash fetch-pr-skill/scripts/fetch_pr_skill.sh "https://gitcode.com/developer-skill/huaweicloud-skills/pull/277"

# Step 2: 运行
bash fetch-pr-skill/scripts/run_skill.sh ./huawei-cloud-skill-audit --timeout 300
```

## 注意事项

- GitCode 使用 GitLab 风格的 PR ref：`refs/merge-requests/<id>/head`，不是 GitHub 风格的 `refs/pull/<id>/head`
- 私有仓库需要先配置 git 认证（SSH key 或 token）
- 仓库缓存在 `/tmp/fetch-pr-skill/` 下，重复运行不会重复 clone
- 如果 PR 涉及多个 skill，所有 skill 都会被拉取
- 运行 skill 需要对应 Agent CLI 已安装（如 opencode）
- 需要 `jq` 工具来处理 session JSON 数据
- 每次运行是独立 session，session 之间互不影响
- 新增 Agent 适配器只需在 `adapters/` 下新建脚本并实现 `adapter_run()` 函数
- 评测需要 Python 3.8+ 和 PyMySQL
- 评测失败不阻塞主流程
- DB 连接信息可通过环境变量覆盖（EVAL_DB_HOST 等）
- 华为云资源释放需要配置环境变量：HUAWEI_CLOUD_AK、HUAWEI_CLOUD_SK、HUAWEI_CLOUD_PROJECT_ID、HUAWEI_CLOUD_REGION（默认 cn-north-4 北京四）
- 释放失败通知通过华为云 SMN 发送，需配置：HUAWEI_CLOUD_SMN_TOPIC_URN（SMN 主题 URN）
- 通知接收人：wangjie594@h-partners.com、hugai@huawei.com
- 评测完成后 session 目录会被删除，如需保留请使用 --dry-run
- 流水线状态同步到 `t_skills_dev_flow` 的 `node_verify_status`（processing/done/failed），需当前环境能定位 `.multica/daemon_task_context.json`（multica 任务运行时自动具备）
- `--dry-run` 模式下仍更新流水线状态（processing/done），仅跳过 hd_eval_* 评测表写入
- `node_verify_url` 默认取 `--pr-url`（GitCode PR URL），未传则回退为 multica issue URL
- PR 自动合入需配置环境变量：`AUTO_MERGE_PR`（on 开启，默认 off）、`GITCODE_TOKEN`（GitCode 访问令牌）
- GitCode 合入 API 使用 `https://api.gitcode.com/api/v5`，`prune_branch: true` 合入时自动删除源分支
