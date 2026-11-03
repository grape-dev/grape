# Plan: Issue #671

## 子任务 ① A2A 归属校验：注册表优先 + GitCode 可达性回退（webhook.py）

- 锚点块改为「未命中 → 标记 `_repo_unregistered`」，不再立即 400。
- 鉴权之后（`_authorize_a2a` / 用户级校验通过）执行回退：
  - `_a2a_repo_fallback_check(repo_key, user_hint)`：
    token 解析与 git-repo 透传端点同源（`TokenResolver` 策略链 + 会话用户 token 回退），
    `VcsApiClient.get_repo_info()` 探测；verdict: ok / not_found / no_permission / unreachable / no_token。
  - 放行门：仅认证调用方（会话 token 有效或 JWT/API Key 用户）触发回退外呼，
    未认证调用方维持原 400 not hosted（不新增外呼暴露面）。
  - `ok` → `_a2a_register_repo(repo_key)` 最小行 `INSERT OR IGNORE` 幂等补录；
    `not_found`/`no_permission` → 400 细分错误信息；
    `no_token`/`unreachable`/其他 → 维持原「未托管」400（安全默认）。
- 错误信息区分「仓库不存在/无权限」与「未托管」。

## 子任务 ② 注册链路缺口修复（repos.py）

- 缺口定位：仓库创建（`POST /repos/create`）后 `host_after_create` 失败或 webhook_url
  未配置时，仅返回「托管失败」消息，不落任何 `managed_repos` 行；而
  `repair-host` 的 `check_repo_access` 依赖 `get_managed_repos`（JOIN user_repos），
  未注册仓直接 403 —— 自愈入口对缺口仓不可达，形成「未注册 → 无法自愈 → 永久未注册」死锁。
  eduSwarm 即该形态（GitCode 可达、会话可用、managed_repos 无行）。
- 修复：
  1. `create_new_repo`：托管失败/webhook_url 缺失分支兜底注册最小行
     （`add_managed_repo` 最小参数 + `mark_user_repo_join` + `set_host_status`），
     保证仓库进入注册链路，repair-host 可达。
  2. `repair_host`：`check_repo_access` 403 时，若 `managed_repos` 无该仓行，
     用调用者用户 token 做 VCS 可达性校验（`VcsApiClient.get_repo_info`），
     通过即放行修复（等效托管权限自证）。

## 子任务 ③ A2A runbook 降级路径（help/api/12-a2a.md）

- 返回表补 400 `repo not hosted`（含 Issue #671 回退语义与细分错误）。
- 约束节补降级路径：400 not hosted → 校正 repo 拼写/确认托管 → 重试 ≤3 →
  仍失败在 Issue 评论说明并人工交办（对齐后台反馈 319 口径）。

## 测试（tests/test_issue_671_a2a_repo_fallback.py）

- 已注册仓：不走回退，行为不变（回归）。
- 未注册 + 回退 ok：放行且 managed_repos 幂等补录（重复调用单行）。
- 未注册 + not_found / no_permission：400 细分错误信息。
- 未注册 + 未认证：维持 400 not hosted（不触发回退外呼）。
- `_a2a_repo_fallback_check` verdict 判定（404/403/网络错误/无 token）。
- create_new_repo 失败分支兜底注册 / repair_host 未注册仓 VCS 自证放行（单测 helper）。
