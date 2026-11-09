# Spec: Issue #640 — issues update `milestone_id` 假成功（200 但里程碑未设置）

> 来源：Issue #640 评论 #197476735 已发布的 Spec Document（Issue Agent，评级 ready，跨流水线复核通过）。本文件为仓库内索引副本。
> 目标分支：`$GRAPE_WORK_BRANCH` = weekly-08-mys（场景注入基线；Issue Spec 原文标注 weekly-09，以注入基线为 PR base）。

## 根因
- GitCode 上游 `PATCH /api/v5/repos/{owner}/issues/{number}` 里程碑参数名为 `milestone`（int，"里程碑序号"），不存在 `milestone_id`；且 body 必填 `repo`+`title`。
- `_RULES[("issues","update")]` 同名直传 `milestone_id` → 上游不识别 → 静默忽略 → 200 假成功；仅传 milestone_id 时缺必填 repo/title → 400。

## 验收（对应 Issue Spec 6 条 AC）
1. 传有效 milestone_id 后，`issues get` 回显已设置的 milestone（非 null）；
2. 传无效/上游不支持时，`issues update` 返回明确错误（502 结构化信息，不再 200 假成功）；
3. 仅传 milestone_id（不带其他字段）不再被上游 400 拒绝（服务端自动补 repo+title）；
4. `milestones list` 响应含可引用 number 字段（存量已满足，help 同步说明）；
5. help/api/10-git-repos.md 契约说明同步（milestone_id → 上游 milestone）；
6. 新增测试覆盖映射、补齐、回读校验、错误路径；现有测试无回归。

Out of Scope：milestones create/update 端点（#577/#632）、GitHub provider 行为、grape-skills 仓 runbook（PR 中注明跨仓跟进）。
