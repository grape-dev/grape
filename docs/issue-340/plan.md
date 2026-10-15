# Plan: Issue #340 — 级联清理实现方案

## 架构
新增 `src/repo_cleanup.py` 作为级联清理唯一入口：

```
cascade_cleanup_repo_data(repo_key, owner, repo, trigger, operator_id, dry_run)
  ├─ 1. repo_key 归一化（gitcode: 前缀双写兼容）
  ├─ 2. dry_run → 仅统计返回
  ├─ 3. DB 单事务：先查存在表集合（sqlite_master）→ 依赖序删除
  │      task_messages←agent_tasks；scene_knowledge_ref←agent_shared_knowledge；
  │      vod_conversations←devops_agent_session；chat_messages←chat_conversations；
  │      其余按 repo_key 直删 → commit；异常 rollback 并抛 RepoCleanupError（可重试）
  ├─ 4. 文件清理（DB 提交后）：skills/repo 目录 + workspace 目录
  │      补偿：rmtree 前写 meta `cleanup_pending:{k}`，成功后删标记；
  │      retry_pending_file_cleanups() 供重试
  └─ 5. 审计：auth_audit.log_audit("repo_cascade_cleanup", detail={repo_key, trigger, stats})
```

## 触发路径集成
| 路径 | 位置 | trigger |
|------|------|---------|
| 平台解除托管 | repos.py `remove_managed_repo`（POST /repos/remove） | platform（operator_id=当前用户） |
| webhook 删除事件 | webhook.py `_process_gitcode_webhook` / `_process_github_webhook` 识别删除事件 → 级联清理 + remove_managed_repo | webhook |
| 探活轮询兜底 | repos.py `_probe_stale_repos` | polling |

## 模块改动
1. `src/repo_cleanup.py`（新增）：核心函数 + `RepoCleanupError` + `retry_pending_file_cleanups`；
2. `src/auth/audit.py`：EVENT_TYPES 增加 `repo_cascade_cleanup`；
3. `src/api/routes/repos.py`：`_cleanup_repo_data` 改为兼容包装（老测试不破）；`remove_managed_repo`、`_probe_stale_repos` 接入级联清理；
4. `src/api/routes/webhook.py`：新增 `_is_repo_delete_event(header, payload)`；两条 webhook 路径 token 校验通过后命中删除事件 → 清理 + 返回 `repo_deleted`；
5. `src/vcs/api_client.py`：`VcsApiClient.repo_exists()`（404→False，供探活复用）；
6. `locales/zh|en/settings.json`：`confirm_remove` 文案明确"将清空全部相关数据"（含需求中心卡片/流水线/知识基线/场景关联）；
7. `tests/test_repo_cascade_cleanup.py`（新增）。

## 事务与补偿细节
- DB：单一 `with db_lock` 事务，任何 DELETE 失败 → `db.rollback()` + 抛 `RepoCleanupError`，调用方可安全重试（无半清理）；
- 文件：无法纳入 DB 事务 → DB 提交成功后再删文件；删除前写 pending 标记，失败保留标记供重试（最终一致）；
- 审计：无论成败都记审计（失败记 error detail）。

## 风险
- 误删活跃仓库：清理前置条件 = 调用点已在解除托管/删除确认之后；webhook/探活路径先验证仓库已不可达；
- 老库缺表：先探测表存在性再删，不因缺表中断事务。
