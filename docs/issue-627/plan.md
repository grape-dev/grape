# Plan: Issue #627 — 生产续接（发布）子单（方案 C：复用现有子任务机制）

## 方案概述
**替换 PR #799 的旁路实现**：移除独立 `production_release` 表与旁路派发/回填链路，改为「发布子单」——
用户批准入口直接创建一张**发布子单**（真实平台 Issue 子任务，`is_subtask_mode=1` + `parent_issue_id=源需求`，
沿用现有 `## 父任务` 子任务识别机制），绑定要素（repo/候选SHA/目标环境/验收引用/批准来源）落在子单
`issue` 行自带字段上；子单以标准 a2a 派发直通 sre 执行部署，子单的**终态字段 `release_result` 即发布结果**
（三分支：deployed_success / deploy_failed / not_executed）。零状态机改动、零平行表、全程在流水线时间线内可回溯。

## 与原旁路实现的差异
| 维度 | PR #799 旁路 | 方案 C 发布子单 |
|------|-------------|-----------------|
| 存储 | 独立表 `production_release` | `issue` 行自身（复用子任务机制：parent_issue_id + is_subtask_mode + release_* 字段） |
| 生命周期 | 旁路状态机 pending→dispatched→executing→finished | 子单标准 stage 流转：sre → 终态；`release_result` 三分支 |
| 派发 | 服务内直调 a2a_endpoint 携带 `payload.production_release` | 同标准 a2a 派发，payload 不再需要绑定锚点（回填按子单 issue_id 反查） |
| 回填 | complete_task/fail_task 按 payload.release_task_id 反查旁路表 | 同钩子位置，按 task.issue_id 判定发布子单并回填 `release_result` |
| 时间线 | 事件挂在源需求 issue 上，发布任务另查 | 子单自己的 issue 时间线（issue_opened→sre a2a→sre 完成回帖），父需求收子任务完成通知 |

## 架构决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 子单形态 | 真实平台 Issue（标题 `发布子单：需求 #N → env @ sha12`，body 含 `## 父任务` 链接 + `## 发布绑定` 块） | 复用现有子任务机制（`_parse_parent_issue_from_payload` 识别父任务）；平台时间线公开可回溯 |
| 子单建档 | `ensure_issue_task(subtask_mode=1)` + `set_parent_issue` + release 字段落 `issue` 行 | 幂等建档 + 现有子任务字段 |
| 入口鉴权 | `require_no_session_token`（JWT/grape_ 用户级） | 与 confirm-done/cancel/resume 一致；tk_ 会话 token 不得创建发布子单 |
| 唯一性 | 同（父需求+候选SHA+目标环境）存在发布子单 → 409 | 防重复发布；SHA 落库前统一小写 |
| 结果三分支 | `issue.release_result` 独立字段 | 子单终态即发布结果；阶段 completed 不等价于部署成功 |
| 证据不可变 | 拒绝路径零写入（不建平台 Issue、不写 issue 行、不增删 events） | 拒绝时保留原验收证据 |
| 派发 | 创建子单后服务内直调 a2a_endpoint（agent=sre, issue_number=子单号） | 标准 A2A 链路（场景节点校验/去重/事件落库/引擎选择全保留） |
| 回填 | complete_task/fail_task 中 sre 任务按 task.issue_id 查 issue 行 → 回填 release_result | 无需 payload 锚点，普通 sre 任务（非发布子单）零影响 |
| 流水线防护 | issue_opened 路由：发布子单跳过 vod 智能推荐/派发（已直通 sre） | 防止平台 webhook 回声把子单再丢进 vod 正常开发流 |

## 状态表达（发布子单）
- 创建：`issue.status=processing`、`stage=sre`（直通 sre，不经 vod/issue/coding/qa）
- 执行中：sre a2a_event running（子单 issue 时间线可见）
- 终态（complete_task/fail_task 回填，幂等）：
  - `release_result='deployed_success'`：exit 0 且结果携带 deploy_url 证据
  - `release_result='deploy_failed'`：exit != 0（release_failure_reason 记录摘要）
  - `release_result='not_executed'`：exit 0 但无部署证据（阶段 completed 不视为发布成功）
  - 同时 `status='completed'`、`completed_reason='release_<branch>'`、`stage='done'`——子单终态

## 拒绝校验链（create 时按序，任一失败抛 ReleaseSubissueError，**拒绝路径零写入**）
1. **鉴权**：非用户级 token（tk_）→ 403；需求不存在 → 404。
2. **终态前置**：源需求 `issue.status='completed'`（非终态走正常流水线，拒绝 400 `not_terminal`）。
3. **SRE 验收前置**：源需求存在已完成 sre 事件（无 → 400 `no_sre_acceptance`）。
4. **候选一致性**：candidate_sha 为完整 40 位 SHA 且与最近 sre 验收事件 payload 的 candidate_sha 一致（有记录时）。
5. **目标环境**：非空且 ≤64 字符。
6. **批准时效**：approved_at 距今 ≤ TTL（默认 24h，`PRODUCTION_RELEASE_APPROVAL_TTL` 可覆盖）。
7. **唯一性**：同（父需求+候选SHA+目标环境）已存在发布子单 → 409 `duplicate`。

## 回填链路设计
- 创建发布子单后，`dispatch=True` 时服务内直调 `webhook.a2a_endpoint`（agent='sre'，issue_number=子单号，
  payload 携带 context 说明生产发布 + `parent_issue`/`is_subtask_mode` 透传）——与旧旁路派发同模式。
- sre 任务终态钩子 `_backfill_release_subissue_on_complete`（complete_task + fail_task 双路径）：
  按 task.repo_key + task.issue_id 查 `issue` 行，`release_target_env != ''` 且 `release_result` 为空时
  按 exit_code + deploy_url 证据回填三分支；终态幂等（已回填不再覆盖）。
- PR #799 R2 语义保留：fail_task 失败上报同样回填 deploy_failed（此前 release 永停 dispatched 的缺陷不再存在）。

## 受影响模块
- `src/api/db_setup.py`：删除 `production_release` 表 DDL + 索引；`issue` 表补 release_* 列。
- `src/api/session_store.py`：删除 production_release 存储函数；新增 `list_release_subissues`。
- `src/api/issues_center.py`：删除 `create_production_release`/`_dispatch_production_release_sre`/
  `_fill_production_release_result`；新增 `create_release_subissue`/`_dispatch_release_sre`/
  `_backfill_release_subissue_result`/`is_release_subissue`。
- `src/api/routes/issues.py`：`/production-release` POST/GET 改调发布子单实现（路由路径保持不变，前端契约稳定）。
- `src/runtime/service.py`：`_fill_production_release_on_complete` → `_backfill_release_subissue_on_complete`。
- `src/api/routes/webhook.py`：移除 a2a payload `production_release` 透传（不再需要）。
- `src/api/event_pipeline.py`：issue_opened 路由对发布子单跳过 vod（防止 webhook 回声二次派发）。

## 测试 seam
公开 seam = HTTP API（`POST/GET /api/issues/{issue_id}/production-release`）——用户可见行为。
测试用 TestClient + 真实测试库（先例：test_tasks_lifecycle.py / test_issues_api.py），平台建单
`_post_gitcode_issue` 沿用 test_issues_api 的 mock 模式；另以纯函数级用例直测回填三分支。

## 提交策略（TDD 红/绿分提交）
1. `docs(#627)`：plan/task 更新为方案 C。
2. `test(#627)`：重写失败测试（拒绝链 + 创建子单 + 唯一性 + 查询 + 回填三分支 + 状态机不变）。
3. `feat(#627)`：实现（删旁路 + issue 列迁移 + 存储层 + 领域函数 + 路由 + 回填钩子 + 流水线防护）。
4. 回归：现有测试套件。