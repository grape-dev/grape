# 事件类型映射参考（GitCode/GitHub → Grape 内部事件）

> 本文档说明 GitCode、GitHub Webhook 事件如何映射到 Grape 内部统一事件类型（canonical event types），
> 以及 Grape 内部事件（A2A、skipped 等）的含义和触发条件。
> 源码定义：`src/vcs/event_types.py`（统一类型）、`src/vcs/gitcode.py`（GitCode 映射）、`src/vcs/github.py`（GitHub 映射）、
> `src/api/routing.py`（路由映射）。

---

## 一、统一事件类型（Canonical Event Types）

所有平台的 Webhook 事件在进入流水线前，先归一化为以下统一类型：

### Issue 事件

| 统一类型 | 说明 | 触发条件 |
|----------|------|----------|
| `issue_opened` | Issue 创建 | Issue 首次创建（open/create） |
| `issue_updated` | Issue 内容更新 | title/description 变更（非 assignee/label） |
| `issue_closed` | Issue 关闭 | Issue 状态变为 closed |
| `issue_reopened` | Issue 重新打开 | Issue 从 closed 重新 open |
| `issue_assigned` | Issue 分配负责人 | assignees 字段变更 |
| `issue_labeled` | Issue 添加/移除标签 | labels 字段变更 |
| `issue_comment` | Issue 评论 | 在 Issue 上发表评论 |

### PR 事件

| 统一类型 | 说明 | 触发条件 |
|----------|------|----------|
| `pull_request_opened` | PR 创建 | PR 首次创建（open/reopen） |
| `pull_request_updated` | PR 更新 | PR 内容编辑或新增 commit（synchronize） |
| `pull_request_closed` | PR 关闭（未合并） | PR 被关闭但未合并 |
| `pull_request_merged` | PR 合并 | PR 合并到目标分支 |
| `pull_request_review` | PR 审查 | 审查提交（approve/request_changes） |
| `note_hook_pr` | PR 评论 | 在 PR 上发表评论 |

### 其他事件

| 统一类型 | 说明 | 触发条件 |
|----------|------|----------|
| `push` | 代码推送 | push 到任意分支 |
| `tag_created` | Tag 创建 | 新建 Git Tag |
| `release_created` | Release 发布 | 新建 Release |
| `build_start` | 构建开始 | CI/CD 流水线启动 |
| `build_failure` | 构建失败 | CI/CD 构建失败 |
| `deploy_start` | 部署开始 | 部署流程启动 |
| `deploy_finish` | 部署完成 | 部署流程结束 |

---

## 二、GitCode Webhook → 统一类型映射

GitCode Webhook 通过 `X-GitCode-Event` 请求头标识事件类型，payload 结构类似 GitLab。

| GitCode 事件头 | payload action | 统一类型 | 备注 |
|----------------|----------------|----------|------|
| `Issue Hook` | `open`/`create` | `issue_opened` | |
| `Issue Hook` | `close` | `issue_closed` | 不路由（SKIP） |
| `Issue Hook` | `reopen` | `issue_reopened` | 不路由（SKIP） |
| `Issue Hook` | `update` + assignees 变更 | `issue_assigned` | 不路由（SKIP） |
| `Issue Hook` | `update` + labels 变更 | `issue_labeled` | |
| `Issue Hook` | `update`（其他） | `issue_updated` | |
| `Note Hook` | noteable_type=`MergeRequest` | `note_hook_pr` | PR 评论 |
| `Note Hook` | noteable_type≠`MergeRequest` | `issue_comment` | Issue 评论 |
| `Merge Request Hook` | `open` | `pull_request_opened` | |
| `Merge Request Hook` | `update` | `pull_request_updated` | |
| `Merge Request Hook` | `merge` | `pull_request_merged` | |
| `Merge Request Hook` | `close` | `pull_request_closed` | 不路由（SKIP） |
| `Merge Request Hook` | `reopen` | `pull_request_opened` | |
| `Merge Request Hook` | `review`/`approve` | `pull_request_review` | |
| `push` | — | `push` | 不路由（SKIP） |
| `tag_push` | — | `tag_created` | 路由到 SRE |
| `release` | — | `release_created` | 路由到 SRE |
| `pipeline` | — | `build_start` | 路由到 SRE |

> GitCode 也兼容 `issues`、`pull_request` 等 GitHub 风格事件头（幂等映射）。

---

## 三、GitHub Webhook → 统一类型映射

GitHub Webhook 通过 `X-GitHub-Event` 请求头标识事件类型，payload 含 `action` 字段。

| GitHub 事件头 | action | 统一类型 | 备注 |
|---------------|--------|----------|------|
| `issues` | `opened` | `issue_opened` | |
| `issues` | `closed` | `issue_closed` | 不路由（SKIP） |
| `issues` | `reopened` | `issue_reopened` | 不路由（SKIP） |
| `issues` | `assigned`/`unassigned` | `issue_assigned` | 不路由（SKIP） |
| `issues` | `labeled`/`unlabeled` | `issue_labeled` | |
| `issues` | `edited` | `issue_updated` | |
| `issue_comment` | — | `issue_comment` | |
| `pull_request` | `opened`/`reopened` | `pull_request_opened` | |
| `pull_request` | `edited` | `pull_request_updated` | |
| `pull_request` | `synchronize` | `pull_request_updated` | 新增 commit |
| `pull_request` | `closed` + merged=true | `pull_request_merged` | |
| `pull_request` | `closed` + merged=false | `pull_request_closed` | 不路由（SKIP） |
| `pull_request` | `labeled` | `pull_request_opened` | |
| `pull_request_review` | — | `pull_request_review` | |
| `pull_request_review_comment` | — | `note_hook_pr` | PR 行内评论 |
| `push` | — | `push` | 不路由（SKIP） |
| `create` | — | `tag_created` | 路由到 SRE |
| `release` | — | `release_created` | 路由到 SRE |
| `workflow_run` | — | `build_start` | GitHub Actions |
| `deployment` | — | `deploy_start` | |
| `deployment_status` | — | `deploy_finish` | |

---

## 四、Grape 内部事件

除 VCS Webhook 事件外，Grape 流水线内部也会产生以下事件类型：

| 内部事件类型 | 说明 | 触发条件 |
|--------------|------|----------|
| `a2a_event` | A2A 交办事件 | Agent 间通过 `/api/a2a` 端点交办任务（如 VOD→Issue、Coding→QA、QA→SRE） |
| `vod_signal` | VOD 信号 | 引导式 Onboarding 触发的 VOD 信号 |
| `onboarding_exp` | Onboarding 探索 | 新用户 Onboarding 探索阶段 |
| `onboarding_issue` | Onboarding Issue | 新用户 Onboarding Issue 阶段 |
| `onboarding_explore` | Onboarding 探索（别名） | 同 onboarding_exp |

### A2A 事件特点

- **豁免去重**：A2A 事件不参与 Issue 级别的事件去重（同一 Issue 可多次 A2A 交办不同 Agent）
- **信任域**：A2A 鉴权支持关联仓库信任域（同场景关联仓库或相同 bot token 的仓库间可互调）
- **场景节点校验**：目标 Agent 必须在当前 Issue 场景的 pipeline nodes 内，否则跳过（`not_in_scene`）
- **触发方式**：Agent 执行完成后通过 `GRAPE_SERVER_URL/api/a2a` 端点提交

---

## 五、事件路由（event_type → Agent）

Grape 根据统一事件类型路由到对应 Agent 处理：

### 可路由事件 → Agent 映射

| 事件类型 | 目标 Agent | 路由方式 |
|----------|-----------|----------|
| `issue_opened` | VOD | 阶段矩阵（首次创建 → VOD 审视） |
| `issue_updated` | Issue | 阶段矩阵（已有 Issue 状态 → Issue Agent） |
| `issue_comment` | Issue | 阶段矩阵（评论触发 Issue Agent 判断） |
| `issue_labeled` | Issue | 阶段矩阵 |
| `pull_request_opened` | QA | PR 订阅匹配 / 场景路由 |
| `pull_request_updated` | QA | PR 订阅匹配（需有新 commit） |
| `pull_request_merged` | SRE | 阶段矩阵（合并 → 部署） |
| `pull_request_review` | — | PR 订阅匹配 |
| `note_hook_pr` | — | PR 订阅匹配 |
| `tag_created` | SRE | 静态映射 |
| `release_created` | SRE | 静态映射 |
| `build_start` | SRE | 静态映射 |
| `build_failure` | SRE | 静态映射 |
| `deploy_start` | SRE | 静态映射 |
| `deploy_finish` | SRE | 静态映射 |
| `a2a_event` | 动态 | A2A payload 中 `agent` 字段指定 |

### 不路由事件（SKIP_EVENTS）

以下事件被流水线直接过滤，不创建事件、不路由 Agent：

| 事件类型 | 原因 |
|----------|------|
| `issue_closed` | Issue 关闭不需要 Agent 处理 |
| `issue_reopened` | Issue 重开不需要 Agent 处理 |
| `issue_assigned` | 分配负责人不需要 Agent 处理 |
| `pull_request_closed` | PR 关闭（未合并）不需要 Agent 处理 |
| `push` | 代码推送不触发流水线（防止频繁触发） |

### 副作用事件（SIDE_EFFECT_EVENTS）

| 事件类型 | 副作用 |
|----------|--------|
| `issue_closed` | → 关闭关联的 agent_tasks |

---

## 六、事件跳过原因（skipped reasons）

事件被跳过时会在 `event_skipped` 表记录原因：

| 跳过原因 | 说明 |
|----------|------|
| `unrecognized_event` | 无法识别的事件类型（非已知统一类型） |
| `no_route` | 非Issue/PR事件且无静态路由映射 |
| `issue_done` | Issue 已标记完成，后续事件不再处理 |
| `self_loop` | 自循环检测（bot 创建的事件不再触发 bot 自身） |
| `label_only_change` | Issue 仅标签变更，无实质内容变化 |
| `no_agent_session` | 仓库未配置 Agent（无 devops_agent_session 绑定） |
| `not_in_scene` | 目标 Agent 不在当前场景的 pipeline nodes 内 |
| `no_commit_on_pr_update` | PR 更新但无新 commit（不触发 QA 审查） |
| `agent_disabled` | 目标 Agent 被禁用 |
| `no_skill_config` | 仓库未配置 Skill |
| `skill_config_error` | Skill 配置错误 |
| `prompt_build_failed` | Prompt 构建失败 |
| `subscription_no_skill` | PR 订阅匹配但无对应 Skill |
| `subscription_agent_disabled` | PR 订阅匹配但 Agent 被禁用 |
| `system_auto_comment` | 系统自动评论（不触发流水线） |
| `user_cancel` | 用户取消任务 |
| `user_confirm_done` | 用户确认 Issue 完成 |
| `user_resume` | 用户恢复任务 |
| `action_disallowed` | 事件动作被 event_rule 规则禁止 |

---

## 七、Webhook 端点

| 平台 | 端点 | 事件头 | Token 验证 |
|------|------|--------|------------|
| GitCode | `POST /webhook/gitcode` | `X-GitCode-Event` | `X-GitCode-Token`（与 managed_repos.webhook_token 比对） |
| GitHub | `POST /webhook/github` | `X-GitHub-Event` | `X-Hub-Signature-256`（HMAC SHA256 签名验证） |

> GitCode Webhook 端点也兼容误入的 GitHub 事件（自动检测 `X-GitHub-Event` 头并转发处理）。
> 未托管仓库（不在 managed_repos 表中）的 Webhook 会被拒绝（404）。
