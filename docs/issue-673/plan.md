# Plan: Issue #673

## 实现路径
1. `src/api/feedback_status.py`（新增）：`derive_feedback_status(stage, task_status)` 映射 + `sync_feedback_statuses()` 联动同步（rank 只升不降：open < in_progress < resolved < closed）。
2. `feedback_issue_converter.py`：
   - `_fetch_pending` SQL：`(source='agent' OR (source='user' AND COALESCE(repo_key,'')!='')) AND issue_url='' AND is_deleted=0`，补选 `source`/`username`；
   - `_resolve_target`：source='user' 一律按 repo_key 路由（复用 `_repo_target`/`_repo_bot_token`）；
   - `_build_issue_body`：user 来源分支（提交人/仓库/事件 + description 脱敏）；
   - `feedback_reconcile_loop`：convert 后追加 `sync_feedback_statuses()`。
3. 前端 `my_feedback.js`：状态徽标后追加 `#issue_number` 外链（stopPropagation）。
4. 前端 `my_feedback_detail.js`：信息网格新增「关联 Issue」行（链接 + issue_progress 文案）。
5. i18n zh/en：`field_issue` / `issue_none` / `issue_progress`。
6. 测试：converter user-source 用例（带上下文转换 / 无上下文跳过 / 幂等）、feedback_status 映射与同步用例、mine 字段断言。

## 风险与对策
- 意见类误转换 → 仅 repo_key 非空的 user 反馈入选
- bot token 缺失 → 沿用本轮 skipped 下轮重试
- 状态回退覆盖人工终态 → 同步按 rank 只升不降
