# Issue #672 Spec — pending-review/issues detail 双路径解析修复

> 来源：Issue #672（Agent feedback 后台 id 320）；沿用 issue Agent 已发布 Spec，本文件为开发落档副本。

## 目标
- pending-review 系列端点支持「内部需求库 issue_id」与「GitCode number + 会话仓库作用域」两条解析路径，至少一条可用。
- issues detail（`GET /api/issues/{owner}/{repo}/{number}`）在内部表无该编号行时，按 number+owner/repo 走 git-repo 通道兜底，消除同编号 404/正常分裂（与 git-repo issues get 一致）。

## 现状三类失败（eduSwarm 仓实测）
1. 注入 `GRAPE_ISSUE_ID=6298` → `issues pending-review --issue_id 6298` 404（内部需求表无该主键行）。
2. `--issue_id 1089` → 403 仓库作用域不匹配（1089 是 GitCode number，撞上其它仓库的内部 issue.id）。
3. `issues detail --owner agent-best-practices --repo eduSwarm --number 1089` 404，但 `git-repo issues get` 同编号正常（id=4491872）。

## 根因
- `issue` 表行仅在 webhook/A2A 建档时写入；eduSwarm 仓该 issue 从未在本平台建档 → 内部 id 路径与本地 detail 聚合双双 miss。
- `_session_devops_issue` 仅按内部主键解析且严格 scope 校验，无 number 兜底。
- `get_issue_detail` 仅查本地 stage/status/events 聚合，无 VCS 兜底。

## 范围（In / Out）
- In：`_session_devops_issue` 双路径解析（内部 id → number@session-repo 兜底）；detail VCS 兜底；必要时的最小建档（同步修复，复用 `_sync_issue_fields(ensure_row=True)`）；回归测试；runbook 降级指引。
- Out：#667 独立修复（仅关联引用）；git-repo 通道改动；前端 UI。

## 验收
- eduSwarm 三组失败场景复现通过：pending-review 用 number 可解析并置审核点；detail 与 git-repo issues get 同编号均 200 且一致。
- 原有行为回归：非 devops agent 403、纯 scope 侵入 403、完全不存在 404 均保持。
