# Spec: Issue #673 — 反馈闭环状态可见性（「我的反馈」分析状态 + 关联 Issue 链接）

> 来源：issue Agent 已发布 Spec（见 Issue #673 评论），本文件为 coding 阶段落档副本。
> 目标分支：weekly-mys（马岩松的专属开发流水线注入基线）。与 #22 互相引用不合并、共用状态枚举口径。

## Objective
在用户工作台「我的反馈」页面，为每条反馈展示分析状态（open / in_progress / resolved / closed）与关联 GitCode Issue 链接，形成反馈闭环的「状态可见性」。

## Scope
- `src/api/feedback_issue_converter.py`：`_fetch_pending` 从仅 `source='agent'` 扩展为「agent + 带 repo 上下文（repo_key 非空）的 user」；user 反馈按 repo_key 路由建 Issue 并回写 issue_url/issue_number/converted_at；纯意见类（无 repo_key）跳过且不报错。
  - 幂等组键（QA 复审 S2 修复）：agent 反馈按 `(task_id, skill_name, title)` 共用一个 Issue；user 反馈按行 id 独立分组（task_id/skill_name 常为空，不同用户同标题不得并组，各自建 Issue、各自回写）。
- 新增 `src/api/feedback_status.py`：统一状态映射（user_feedback.status ↔ issue.stage/status），#22 通知机制共用同一映射；reconcile 周期内联动同步（只升不降，不覆盖 admin 手工终态）。
  - 状态联动双通道（QA 复审 S1 修复）：agent 反馈带内部 issue_id 映射 → `sync_feedback_statuses` 走内部 issue 表 JOIN 推导；user 反馈转换生成外部 GitCode Issue（内部无 issue 行）→ `sync_external_feedback_statuses` 按 issue_url/issue_number 拉取外部 Issue state 联动（closed → closed，只升不降，open 不回退），每轮 limit=20 控制外部 API 调用量。
- `src/api/static/js/pages/my_feedback.js`：列表项状态徽标后渲染关联 Issue 链接（issue_url 有值时 `#issue_number` 外链，target=_blank）。
- `src/api/static/js/pages/my_feedback_detail.js`：基本信息网格新增「关联 Issue」字段（链接 + 处理进度文案）。
- `src/api/static/locales/{zh,en}/app.json`：新增 field_issue / issue_none / issue_progress 文案。
- 无 DDL 变更（user_feedback 表 issue_url/issue_number/converted_at 列已存在）。

## Out of Scope
- 主动推送通知（#22 范围）；管理端反馈列表改造；反馈编辑/追加评论。

## Acceptance Criteria
- 列表/详情页可见状态与关联 Issue 链接，点击新窗口跳转
- 带上下文的 user 反馈在 reconcile 周期内被转换并回写；无上下文不转换不报错
- 幂等：`WHERE issue_url=''` 保护，重复 reconcile 不重复建 Issue
- 状态随 issue 推进联动更新（映射公共模块，与 #22 共用）
- zh/en i18n 齐全；现有测试 + 新增测试通过
