# Spec: Issue #3621 — 建单端点补 `trigger_pipeline` 可选参数（只建单不触发）

> 来源：Issue #3621 Spec 评论（issue agent 生成，Developability: ready）。路径勘误已按 Issue 评论核对：
> 本分支实际落点为 `src/domains/runtime/routes/issues.py`（建单入口）与 `src/domains/runtime/issues_center.py`
> （`src/api/issues_center.py` 为其 sys.modules 兼容别名，同一模块对象）；`contracts/` 下仅
> `health.schema.json` 等契约，无 `openapi_baseline.json`，建单 body 契约按仓库实际情况以
> `docs/openapi/11-issues.md` 登记为准。

## Objective
- 统一建单端点 `POST /api/issues/{owner}/{repo}` 增加可选参数 `trigger_pipeline`（boolean，默认 `true`），
  支持「只建单、不触发流水线」的挂单模式。
- 默认行为完全不变（零破坏）；`false` 时单可见、无任何流水线事件；后续触发与直接建单行为一致。

## Scope
- In：建单入口 body/model 新增字段（缺省 true）；`false` 时照常建平台单 + 同步 GitCode Issue + 建 issue
  卡片，跳过流水线启动；不触发时写可辨识状态 `stage=created`（`status` 保持 `active`）；后续首次触发复用
  `issue_opened` 事件路径（幂等、不重复入链）；补默认与 `false` 路径回归测试。
- Out：不改造流水线节点编排与 Agent 执行逻辑；不新增独立「批量攒单」业务模块；不改变 GitCode Issue
  的创建与同步语义。

## Acceptance Criteria
- [x] 不传 `trigger_pipeline` 或传 `true`：建单行为与现状完全一致（GitCode 同步、`stage=vod`、
  产生 `issue_opened` 同步触发、进入流水线）。
- [x] 传 `trigger_pipeline=false`：平台单创建成功且列表可见，GitCode Issue 已同步，`stage=created`，
  无任何流水线事件。
- [x] 对 `false` 建出的单执行触发（`POST /api/issues/{issue_id}/pipeline/trigger` → `trigger_pending_issue_pipeline`）：
  正常进入流水线，事件形态与直接建单一致，且不重复入链（重复/并发触发 → 409）；触发失败或无事件落地时
  回滚为 `stage=created` 可重试，不卡死。
- [x] 默认调用回归测试与新增路径测试全部通过。
