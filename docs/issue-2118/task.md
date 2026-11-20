# Task Checklist: Issue #2118

- [x] Task 1: 固定失败契约
  - Acceptance: 节点、场景、入口、技能测试可复现当前缺口。
  - Verify: `python3 -m pytest tests/mgmt/test_org_scene_template.py tests/contracts/test_org_skills_contract.py -q`
  - Files: `tests/mgmt/test_org_scene_template.py`, `tests/contracts/test_org_skills_contract.py`

- [x] Task 2: 补齐组织节点与场景
  - Acceptance: 三节点字段完整，`pipelines.json` 同形状，2 节点简化模板可落库。
  - Verify: `python3 -m pytest tests/mgmt/test_org_scene_template.py -q`
  - Files: `src/api/pipeline_config.py`, `configs/pipelines.json`

- [x] Task 3: 组织入口校验
  - Acceptance: 组织仓库 + 组织场景可进入；错配返回可读错误；普通仓库不受影响。
  - Verify: `python3 -m pytest tests/mgmt/test_org_scene_template.py -q`
  - Files: `src/api/org_context.py`, `src/domains/runtime/routes/issues.py`

- [x] Task 4: 组织技能
  - Acceptance: 三份技能及 references 完整，契约与 #2112 对齐。
  - Verify: `python3 -m pytest tests/contracts/test_org_skills_contract.py -q`
  - Files: `.grape/skills/org/**`

- [x] Task 5: PR #1264 审查修复
  - Acceptance: 技能命令面可执行且有 fail-closed 门禁；场景元数据查询失败显式拒绝；
    repo 级场景推进透传 `repo_key`；官方 org 场景在存量库幂等补种；
    组织运行态端点支持 worker 会话 token；org 模板不在普通场景市场暴露或应用。
  - Verify: `python3 -m pytest tests/mgmt/test_org_scene_template.py tests/contracts/test_org_skills_contract.py -q`
  - Files: `.grape/skills/org/**`, `src/api/org_context.py`, `src/api/pipeline_config.py`,
    `src/api/db_setup.py`

- [x] Task 6: PR #1264 复审修复（org 官方内置包可解析 + 运行时技能目录不入库）
  - Acceptance: `resolve_official_skill_path` 命中 `.grape/skills/org/<skill>/SKILL.md`，
    sid=0 时物化走官方包兜底（不要求 skills detail）；A2A 阶段推进按 `repo_key`
    读取场景节点；`.grape/skills` 仅白名单化仓库内置资产。
  - Verify: `python3 -m pytest tests/mgmt/test_org_skill_bundle_resolve.py tests/runtime/test_issue_2118_a2a_next_node_repo_key.py -q`
  - Files: `src/kernel/config.py`, `src/domains/runtime/routes/webhook.py`,
    `scripts/sync_skills.sh`, `.gitignore`, `tests/mgmt/test_org_skill_bundle_resolve.py`,
    `tests/runtime/test_issue_2118_a2a_next_node_repo_key.py`

- [x] Task 7: PR #1264 复审修复（组织节点 A2A 匹配 + 建单预校验 + 自动推进 + 模板隔离）
  - Acceptance: A2A/阶段推进按 id 或 tag 命中 `org_*` 节点；建单前预校验对
    sid=0 的组织技能复用官方内置包门禁；组织场景 issue_opened 从首节点启动；
    三份组织技能显式 `grape a2a handoff`；complete_task 在 stage 未推进时按
    `next/handoff` 自动交办，已交办不重复；org 官方模板不向普通仓库解析。
  - Verify: `python3 -m pytest tests/runtime/test_issue_2118_org_pipeline_flow.py tests/contracts/test_org_skills_contract.py -q`
  - Files: `src/api/pipeline_config.py`, `src/api/scene_precheck.py`,
    `src/api/pipeline_executor.py`, `src/domains/runtime/routes/webhook.py`,
    `src/domains/runtime/engine/service.py`,
    `src/domains/runtime/pipeline/event_pipeline.py`, `configs/pipelines.json`,
    `.grape/skills/org/**`, `tests/runtime/test_issue_2118_org_pipeline_flow.py`

- [x] Task 8: PR #1264 复审修复（聚合下一轮 + issue_opened 竞态 + 派发失败 + 存量迁移）
  - Acceptance: `org_aggregate` 输出 remaining 时不提前 `pending_review`，服务端按
    `next_step=a2a-qa` 延迟安排下一轮聚合；`issue_opened` 在等待场景绑定后再判定组织
    首节点；自动推进仅在 A2A 派发成功（status=ok 且 seq 匹配）后改 stage，失败保持
    原节点并留痕；存量 `custom-org-*` / `organization.org_scene_id` 场景启动迁移为
    `scene_type=org`。
  - Verify: `python3 -m pytest tests/runtime/test_issue_2118_org_pipeline_flow.py tests/mgmt/test_org_scene_template.py tests/contracts/test_org_skills_contract.py tests/atomic/test_migration_track_guard.py -q`
  - Files: `src/domains/runtime/engine/service.py`,
    `src/domains/runtime/pipeline/event_pipeline.py`, `src/api/db_setup.py`,
    `.grape/skills/org/org-aggregate-cli/**`,
    `tests/runtime/test_issue_2118_org_pipeline_flow.py`,
    `tests/mgmt/test_org_scene_template.py`

- [x] Task 9: PR #1264 复审修复（异步推进 stage 语义 + 聚合重排持久化 + 普通仓库 org 模板拒绝）
  - Acceptance: plugin/queue_remote 事件驱动路径下 stage 只在 listener 确认
    `dispatch_node` 成功后才推进（`run_a2a_dispatch` 异步时携带 stage_guard，不提前写）；
    已推进的同步路径若下游最终 failed 由 `_fail_event` 幂等回滚；
    `org_aggregate` 下一轮不再依赖进程内 `threading.Timer`——落成持久化 queued
    a2a_event（`_not_before`），巡检 `reconcile_queued_a2a` 到点补派，派发失败按退避
    重排、耗尽后转 `pending_review`；巡检 `l1_event_exists` 不再把 failed 交棒事件
    当证据；普通仓库引用 org 模板（market scope=org / file scene_type=org / 跨仓
    org 场景行）在入口与预校验双 fail-closed 拒绝，不产生 VCS/DB 死单。
  - Verify: `python3 -m pytest tests/runtime/test_issue_2118_org_recheck_persistence.py tests/runtime/test_issue_2118_org_pipeline_flow.py tests/mgmt/test_org_scene_template.py -q`
  - Files: `src/api/a2a_dispatch.py`, `src/api/org_context.py`,
    `src/api/scene_precheck.py`, `src/api/pipeline_config.py`, `src/api/patrol.py`,
    `src/domains/runtime/engine/service.py`,
    `src/domains/runtime/pipeline/event_pipeline.py`,
    `tests/runtime/test_issue_2118_org_recheck_persistence.py`,
    `tests/runtime/test_issue_2118_org_pipeline_flow.py`,
    `tests/mgmt/test_org_scene_template.py`

- [x] Task 10: PR #1264 复审修复（异步失败重排闭环 + blocked 不推进 stage）
  - Acceptance: plugin/queue_remote 路径下 `run_a2a_dispatch` 提前返回 async ok 后，
    listener 的 `dispatch_node` failed/blocked/异常统一收口——聚合重排事件
    （`payload.aggregate_recheck`）走 `_requeue_or_degrade_aggregate_recheck`，按退避
    重排、耗尽后转 `pending_review`（不再只 `_fail_event` 直接终态）；仅
    `status=dispatched` 才按 stage_guard 推进，blocked/未知状态不推进并留痕，避免
    stage 指向从未实际派发的下游；a2a 与 webhook 两处 listener 口径一致。
  - Verify: `python3 -m pytest tests/runtime/test_issue_2118_org_recheck_persistence.py tests/runtime/test_issue_476_failed_result.py tests/runtime/test_issue_2118_org_pipeline_flow.py -q`
  - Files: `src/api/a2a_dispatch.py`, `src/domains/runtime/routes/webhook.py`,
    `tests/runtime/test_issue_2118_org_recheck_persistence.py`
