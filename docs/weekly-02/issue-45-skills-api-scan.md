# Issue #45 参考：技能实际 API 使用面（vod/coding/issue/qa）

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：参考基线（非实现文档）
> 日期：2026-09-07
> 数据来源：服务器 `skills/` 下 `vod/` `coding/` `issue/` `qa/` 全部 SKILL.md + references/（共 144 文件）接口引用扫描。
> 用途：CLI 子命令覆盖优先级、OpenAPI 对外接口集合、Git 仓库接口白名单的权威依据。

## 一、各技能引用的 Grape 内部 API

| 技能目录 | SKILL.md | references 补充 |
|----------|----------|----------------|
| vod/vod | `/api/a2a` | `/api/a2a`（requirement-elicit-guide） |
| vod/blog-vod | — | `/api/a2a`（a2a-handoff） |
| vod/iac-vod | `/api/a2a` | `/api/a2a` |
| vod/event-dialogue | `/api/events/{seq}/flow/reset`、`/api/events/{seq}/knowledge` | — |
| vod/repo-onboarding | `/api/tasks/notify` | — |
| coding/implementation | `/api/a2a` | `/api/a2a`（new-dev-flow） |
| coding/skill-creator | `/api/a2a` | — |
| coding/rest-api-skill-creator | `/api/a2a`、`/api/search` | — |
| coding/ai-blog-generation | `/api/a2a` | — |
| issue/issue-advance | `/api/a2a`、`/api/tasks/close`、`/api/tasks/notify` | `/api/tasks/notify`、`/api/a2a` |
| issue/issue-multitask | `/api/a2a`、`/api/tasks/close`、`/api/tasks/notify` | 同上 |
| issue/issue | `/api/a2a`、`/api/tasks/close`、`/api/tasks/notify` | 同上 |
| issue/blog-issue | `/api/a2a`、`/api/tasks/close`、`/api/tasks/notify` | — |
| issue/skill-issue | `/api/a2a`、`/api/tasks/close`、`/api/tasks/notify` | 同上 |
| qa/qa | `/api/a2a` | — |
| qa/pr | `/api/a2a` | — |
| qa/impact-analysis | `/api/a2a` | — |
| qa/skill-tester / rest-api-skill-tester | `/api/a2a` | — |

## 二、Grape 内部 API 引用统计（去重后）

| 端点 | 方法 | 引用数 | 涉及技能 |
|------|------|:---:|----------|
| `/api/a2a` | POST | 45 | vod/coding/issue/qa 几乎全部 |
| `/api/tasks/notify` | POST | 26 | issue 系列 + repo-onboarding |
| `/api/tasks/close` | POST | 5 | issue 系列 |
| `/api/events/{seq}/knowledge` | POST | 1 | event-dialogue |
| `/api/events/{seq}/flow/reset` | POST | 1 | event-dialogue |
| `/api/search` | GET | 1 | rest-api-skill-creator |

> **CLI/OpenAPI 优先级建议**：`/api/a2a`、`/api/tasks/notify`、`/api/tasks/close` 技能最依赖 → CLI 最先覆盖；`/api/events/*/knowledge`、`/api/events/*/flow/reset` 为 event-dialogue 专用（worker 结果获取）。

## 三、各技能引用的 GitCode API（去重归一，~14 类）

| 端点（归一） | 用途 |
|--------------|------|
| `GET /api/v5/repos/{o}/{r}` | 仓库信息 |
| `GET /api/v5/repos/{o}/{r}/branches` | 分支列表 |
| `GET /api/v5/repos/{o}/{r}/contents/codeowners` | CODEOWNERS |
| `GET/POST /api/v5/repos/{o}/{r}/issues` | 列/建 Issue |
| `GET/PATCH /api/v5/repos/{o}/{r}/issues/{n}` | Issue 详情/更新 |
| `GET/POST /api/v5/repos/{o}/{r}/issues/{n}/comments` | Issue 评论 |
| `GET/POST /api/v5/repos/{o}/{r}/pulls` | 列/建 PR |
| `GET/PATCH /api/v5/repos/{o}/{r}/pulls/{n}` | PR 详情/更新 |
| `GET/POST /api/v5/repos/{o}/{r}/pulls/{n}/comments` | PR 行内评论 |
| `PUT /api/v5/repos/{o}/{r}/pulls/{n}/comments/{id}` | 标记 resolved |
| `GET /api/v5/repos/{o}/{r}/pulls/{n}/files` | 变更文件 diff |
| `GET /api/v5/repos/{o}/{r}/commits` | 提交历史 |
| `GET /api/v5/repos/{o}/{r}/labels` | 标签 |
| `GET /api/v5/repos/{o}/{r}/milestones` | 里程碑 |

> 这些 GitCode 接口原由技能以 `gitcode-api-reference.md` 公共参考承载（外部 API）；**按 2026-09-07 决策包装为 grape Git 仓库统一接口（`/api/git-repo/...`）**，技能统一走 grape API + 单一 token，`X-Git-Provider` 区分平台（见 `issue-45-git-repos-api-design.md`）。