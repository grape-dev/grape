# Spec: Issue #671 — A2A 400 repo not hosted（注册表优先 + GitCode 可达性回退）

> 基准：Issue #671 评论 id 197551357（issue agent 发布、id 197610005 确认）。
> 本文档为 coding 侧落地版，与确认版 Spec 保持同一范围/验收。

## 目标
统一 `grape a2a handoff` 与 git-repo 命令的仓库归属判定口径：
- 注册表（`managed_repos`）优先；
- 未命中时回退 GitCode 可达性/权限校验（与 git-repo 透传端点同源通道）；
- 回退放行后幂等补录 `managed_repos`；
- 修复新托管仓未进入 `managed_repos` 的注册链路缺口；
- A2A runbook 补充 400「repo not hosted」降级路径（重试 ≤3 → 评论说明 + 人工交办，对齐反馈 319 口径）。

## 改造锚点
- `src/api/routes/webhook.py` A2A 端点「仓库真实性校验」块
  （`SELECT 1 FROM managed_repos WHERE repo_key=?` 未命中即 400 not hosted）。
- `src/api/routes/repos.py` 托管/创建链路（`create_new_repo` / `repair_host`）。
- `help/api/12-a2a.md`（A2A runbook）。

## Out of Scope
- A2A 协议/payload 变更；`managed_repos` 表结构重构；
  反馈 319 的 labels/assignee 403 口径问题（另案处理）。

## 验收标准（同确认版 Spec 6 项）
1. 对 `gitcode:agent-best-practices/eduSwarm` 类仓（注册表未命中但 GitCode 可达）A2A 放行（2xx/skipped）；
2. 同一 repo_key 两链路（git-repo 命令 vs A2A）判定一致；
3. 回退放行后 `managed_repos` 幂等补录，重复调用不产生重复行；
4. 注册链路缺口定位并有修复落地（create/repair 入口兜底注册）；
5. A2A runbook 含 400 repo not hosted 降级路径；
6. 现有测试通过 + 新增测试覆盖回退放行/补录幂等/错误语义。
