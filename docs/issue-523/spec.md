# Spec: Issue #523 — 修正建单 403 错误归因（Space 仓库禁止建单被误报为处理人无效）

> 承接 Issue Agent 已发布 Spec（VOD 跨流水线复核 ready）。目标分支：weekly-08-mys（原 Spec 所写 weekly-09 为前一流水线基线，本流水线以 weekly-08-mys 为开发基准，已核对该分支 L1464-L1468 仍为单一 assignee 断言，无重复开发风险）。

## Objective
- 修正 `src/api/issues_center.py` `create_task_via_issue` 建单 403 异常分支的错误归因：保留上游 GitCode 403 响应正文（截断 ≤200 字符 + 脱敏），按语义分流提示，不再仅凭 HTTP 403 断言「assignee 无效」。
- 根因：GitCode 对 Space/模型仓/数据集仓返回 403 正文 "The current operation is prohibited for model repository and dataset repository."，属仓库能力限制；当前提示「处理人无效或无权限」把排障引向账号改名/成员权限/OAuth 错误方向。

## Scope
- `create_task_via_issue` 403 分支（原 L1464-L1468）改造：从 `_err_msg` 提取 `(403): ` 之后的上游正文，截断+脱敏。
- 语义分流（按上游正文关键词，大小写不敏感）：
  - `prohibited for model repository and dataset repository` → 「当前仓库类型不支持创建 Issue，请改用普通代码仓」+ 上游脱敏正文；
  - 用户/权限类正文（user not found / permission / forbidden / member 等）→ 保留「处理人无效或无权限（assignee=...）」+ 上游脱敏正文；
  - 无法识别的 403 → 通用「创建 Issue 失败（上游 403）」+ 脱敏正文，不猜测归因。
- 401/422 分支语义保持不变。

### Out of Scope
- 绕过 GitCode 对 Space/模型仓/数据集仓的建单限制；#497（账号改名 assignee 404）；查重链路。

## Acceptance Criteria
- [ ] 403 detail 保留脱敏截断的上游正文，不再仅凭状态码断言 assignee 无效
- [ ] prohibited-for-model-repository 正文 → 仓库类型不支持提示 + 改用普通代码仓建议，不含「处理人无效」措辞
- [ ] 权限类 403 → 保留 assignee 提示；未识别 403 → 通用提示附脱敏正文
- [ ] 401/422 现有分支不回归；正常代码仓建单成功路径不受影响
- [ ] 新增四类回归用例全部通过；现有测试通过
