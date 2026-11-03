# Spec: Issue #805 — 需求中心创建人（creator）一等数据化

## 目标
创建时落库 `issue.created_by`，接口透出 `creator`，前端只读展示创建人；统计口径不再随 assignee 流转漂移。

## 关键行为
1. 新建 issue（webhook 建档）后 `issue.created_by` 有值（GitCode 作者 login）。
2. `GET /issues/{owner}/{repo}`、`/issues/mine` 每条含 `creator`；详情 `GET /issues/{owner}/{repo}/{number}` 返回 `creator`。
3. 列表/看板/详情展示创建人，流转后不变；`creators=` 查询参数按创建人过滤（与 `assignees=` 并存，向后兼容）。
4. 回填脚本 `scripts/backfill_issue_creator.py` 支持 `--dry-run`/`--limit`，幂等仅空补写。
5. 幂等规则：复用已有卡片时仅在 `created_by` 为空时回填，绝不覆盖。

## 范围
- In: issues_center.py / event_pipeline.py / routes/issues.py / db_setup.py(列兜底) / backfill 脚本 / static/js/pages/issues.js（weekly-08-mys 实际前端源码，IssuesView.vue 仅存在于 vue-integration 分支，不适用本分支）。
- Out: PR 事件卡片「触发者」保持 assignee；bot 建单 creator 不做二次映射。

## 创建人提取优先级（event_pipeline 建档分支）
payload.object_attributes/user.login → payload.issue/user.login → VCS fetch（issue_opened 类事件）→ sender 兜底。
