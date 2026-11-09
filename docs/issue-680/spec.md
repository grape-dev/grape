# Spec — Issue #680 PR 合并事件关联 Issue 解析取错候选（resolve_pr_issue_id）

> 来源：Issue #680 Spec（issue agent 已发布，评级 ready）｜目标分支：weekly-08-mys

## 背景与根因

PR #116 合并事件（事件 38851）解析关联 issue 时，`resolve_pr_issue_id` 从 GitCode
`payload.issues` 列表"取第一个"，取到正文背景引用的旧 issue #274 而非闭环目标
#282；#274 场景无 sre 节点 → 场景校验 `not_in_scene` 丢弃整个事件 → SRE 断链
71 分钟，需人工评论恢复。

## 需求

1. `resolve_pr_issue_id` 优先级重排：分支名 `issue-N`（强信号）→ PR 标题 `#N`
   → issues 列表**取编号最大** → PR 详情 API 兜底（同规则：head.ref → title → issues 最大）。
2. `resolve_issue_id` 的 issues 兜底段同改"取编号最大"。
3. 新增统一入口 `_resolve_event_issue_id`：一次解析存 `evt["_resolved_issue_id"]`，
   原 7 处调用点（阶段路由 / 订阅子事件 ×3 / `_pre_issue_id` / sre 特例 / 主解析）
   全部消费统一值，消除 token 口径分裂。
4. 顺手修 `##274` 双井号拼接缺陷（`_norm_issue_id` 入口/出口规范化）。
5. 新增单测 `tests/test_event_pipeline_pr_issue.py`。

## 验收标准

- PR #116 原始形态（title 含 issue #282、issues=[#282,#274]、head=feat/issue-282）→ #282
- 多 issue 列表乱序 [#274,#282] → #282；分支名 feat/issue-282 + 正文仅提 #274 → #282
- title 自引用跳过；issues 含 PR 自身编号跳过；GitHub 形态（无 issues 列表）行为不变
- 双 # 规范化：##274 → #274
- 同一 evt 多调用点解析一致（_resolved_issue_id 只计算一次）
- 现有测试通过 + 新增测试覆盖

## 范围外

场景节点校验逻辑（`_STAGE_EVENT_AGENT_MAP`、not_in_scene skip 记录格式）、user_id
归属覆盖缺陷、历史数据修复。
