# 路径 B Webhook 过滤修复——自循环检测

## 需求项

### R20.1 路径 B agent 评论自循环修复

**问题**：路径 B 下 agent 用用户 token 发评论，actor 是用户而非 bot，自循环检测不命中，导致重复执行。

**核心矛盾**：路径 B 下 agent 和用户用同一个 token（用户 token），actor 完全相同（host_user VCS username）。任何仅基于 actor 的检测都无法区分"agent 回声"和"用户主动操作"。简单判断 `actor == host_vcs_username → 跳过` 会导致用户自己发评论也被拦截，无法触发流水线。

**设计约束**：
- 不能将 host_user 加入 bot_usernames（会拦截用户的正常操作，见场景 17.2）
- 不能依赖评论标记（agent 直接调 API 发的评论无标记，见场景 12.1）
- 不能仅基于 actor 判断（actor 相同无法区分，见场景 18）

- R20.1.1：路径 B 自循环检测四条件——全部满足才跳过：
  1. 仓库是路径 B（host_user_id > 0）
  2. actor == host_vcs_username（host 用户的 VCS 平台用户名）
  3. event_type in (issue_comment, note_hook_pr, issue_updated)（agent 可能发的操作类型）
  4. 该 issue/PR 有正在执行的 task（agent_tasks.status in (running, dispatched)）或该 PR 有正在执行的 task
  条件 4 不满足 → 说明没有 agent 在执行 → actor 的操作是用户主动操作 → 放行
- R20.1.2：开发者指令豁免——评论内容含 /done /cancel /resume /完成任务 /取消任务 /恢复任务 时，即使四条件满足也不拦截（用户指令优先，先于自循环检测处理）
- R20.1.3：host_vcs_username 获取方式——从 external_identities 查 host_user_id 对应的 provider username（VCS 平台 login 字段，不是 grape account username）
- R20.1.4：host_vcs_username 缓存——避免每次 webhook 都查 external_identities，可在 managed_repos 新增 host_vcs_username 列（R16.5.5），托管时从 external_identities 同步写入，或从 external_identities 带 TTL 缓存查询
- R20.1.5：host_vcs_username 同步——每次验证 token 有效性（provider.verify_token）时，如果返回的 login 与 DB 中的 host_vcs_username 不一致 → 更新 DB（用户改了 VCS 平台用户名的场景）。**R24.3 补充**：OAuth refresh 成功后也同步（refresh 不调 verify_token，需额外调 get_user_login 获取当前 login）
- R20.1.6：向前兼容——路径 A 仓库（host_user_id == 0）不受影响，原有 bot_usernames 检测不变
- R20.1.7：查询失败降级——如果 host_vcs_username 查询失败（external_identities 已删除等）→ 降级为不检测（不阻断 pipeline），记录 warning
- R20.1.8：task 执行状态查询——查 agent_tasks 表 WHERE repo_key=? AND issue_id=? AND status IN ('running','dispatched') LIMIT 1，有结果说明 agent 正在执行

### R20.2 路径 B fail_task 回帖降级处理

**问题**：路径 B 下 fail_task 无法发 Issue 评论（无全局 bot token + 用户 token 已过期）。

- R20.2.1：fail_task 路径 B 回帖失败时不静默——记录 warning 日志 + task errors 额外 append COMMENT_POST_FAILED
- R20.2.2：fail_task 路径 B 回帖失败时 dashboard 显示增强提示——"Token 已过期且无法发 Issue 评论，请重新绑定 Token 后在 dashboard 查看任务结果"
- R20.2.3：fail_task 路径 B 回帖失败时不重试（避免用过期 token 反复 401）
- R20.2.4：如果路径 B 仓库有全局 bot token（尚未 auto_upgrade 但已配置）→ fail_task 优先用全局 bot token 发评论（best-effort，可能 403）

**设计**：fail_task 评论 token 选择通过 handler.get_comment_token() 调用（R18.6.9），UserTokenHandler.get_comment_token 内部逻辑：
1. 用户 token 有效 → 用用户 token
2. 用户 token 过期 → 尝试全局 bot token（best-effort，可能 403）
3. 无可用 token → 返回空 → fail_task 记录 COMMENT_POST_FAILED

### R20.3 路径 B agent 执行期间 token 过期保护

**问题**：agent 执行耗时长，执行期间用户 token 可能过期，导致结果回帖失败或 git push 失败。

- R20.3.1：agent 执行前预检 token 有效性——如果 token 剩余有效期 < 预估执行时间，记录 warning 但不阻断（token 可能续期或任务提前完成）。PAT 无 expires_at，预检仅对 OAuth token 有效（检查 token_expires_at）
- R20.3.2：_post_result_to_issue 失败时（401）→ task errors append RESULT_POST_FAILED + dashboard 显示"结果已执行但无法回帖到 Issue，请在 dashboard 查看任务详情"
- R20.3.3：_post_result_to_issue 失败时不影响 task 状态——task 仍标记 completed（agent 确实完成了工作，只是回帖失败）
- R20.3.4：agent git push 失败时（401）→ fail_task 正常处理（已有 R10/R16.1 逻辑）

**设计**：_post_result_to_issue 改为通过 handler.post_issue_comment() 调用（R18.6.10），UserTokenHandler.post_issue_comment 内部 try-except 保护，失败时返回 False，调用方据此 append RESULT_POST_FAILED。

### R20.4 路径 B auto_upgrade webhook 重建保护

**问题**：auto_upgrade 删旧 webhook 可能因 bot 无 admin 权限或 host token 过期而失败。

- R20.4.1：auto_upgrade 遍历路径 B 仓库时，host 用户 token 过期 → 跳过并记录日志，仓库保持路径 B（已有行为，明确为设计意图而非 bug）
- R20.4.2：auto_upgrade 删旧 webhook 失败时（bot 无 admin 权限）→ 不写 DB（不写 bot_token + 不清 host_user_id），仓库保持路径 B
- R20.4.3：auto_upgrade webhook 重建失败时 → 仓库保持路径 B，记录日志，管理员可手动 repair_host
- R20.4.4：auto_upgrade 成功标准：邀 bot + accept invite + webhook 重建 + DB 写入 全部成功才算升级，任一步失败则回滚到路径 B

**设计**：auto_upgrade_path_b_repos 内部对每一步的结果做检查，任一步失败则 continue 跳过该仓库，不写 DB。具体：
1. 获取 host 用户 token → 空 → skipped, continue（R20.4.1）
2. 邀 bot → 失败 → 记日志 continue（邀请幂等，可能已是协作者，继续尝试 webhook）
3. accept invite → 失败 → 记日志 continue
4. _rebuild_webhook_with_bot_token → 返回空（删旧/建新失败）→ 不写 DB, continue（R20.4.2/R20.4.3）
5. _update_repo_bot_token → 写 DB（只有前面全成功才执行）

### R20.5 路径 B webhook 创建权限提示

**问题**：非 admin 用户托管仓库时创建 webhook 403。

- R20.5.1：托管时如果 setup_webhook 返回 403 → 返回明确错误"需要仓库 admin 权限创建 webhook，请联系管理员托管或获取 admin 权限"
- R20.5.2：前端展示托管失败原因 + 指引文案

**设计**：托管逻辑（onboarding/repo register）中 setup_webhook 返回 403 时，返回 HTTP 403 + error_code=WEBHOOK_PERMISSION_DENIED + 指引文案。前端 i18n 映射该 error_code。

### R20.6 路径 B 自循环检测与路径 A 的一致性保证

**问题**：路径 A 和路径 B 的自循环检测逻辑不一致，路径 B 有缺口。且路径 B 下 agent 和用户 actor 相同，简单判断会误拦截用户操作。

- R20.6.1：pipeline 步骤 2.0 自循环检测改为通过 TokenActionHandler.is_self_loop_actor() 调用（R18.6.8），消除现有 if actor in bot_usernames + if _is_user_token_repo 散落分支
- R20.6.2：开发者指令检测（/done /cancel /resume）移到自循环检测之前——确保用户指令不被自循环检测误拦截（调整 pipeline 步骤顺序：2.0b 开发者指令 → 2.0 自循环检测）
- R20.6.3：路径 B 自循环检测不影响用户正常操作——用户在 issue 上评论时，如果没有正在执行的 task（条件 4 不满足）→ 放行 → 用户可以正常触发流水线
- R20.6.4：路径 B 自循环检测的时序竞态——agent 完成执行到 task 状态更新为 completed 之间有窗口，期间 agent 发的评论可能被放行（task 已不是 running 但 agent 还在收尾）。可接受——最多多一次 pipeline 触发，由下次自循环检测（task 已完成 → 条件 4 不满足 → 放行 → 但新 task 执行后自循环检测会命中）兜底
- R20.6.5：如果 host_vcs_username 查询失败（external_identities 已删除等）→ 降级为不检测（不阻断 pipeline），记录 warning

### R20.15 event dialogue 自循环检测覆盖（P0 补充）

**问题**：event dialogue（事件对话，events.py）用 Repo(repo_key).bot_token 获取 token 注入 agent。event dialogue 不走 agent_tasks 表 → 自循环检测条件 4 查不到 → agent 评论触发重复执行。

- R20.15.1：event dialogue 执行时在 agent_tasks 表创建 task 行（status=running）→ 自循环检测条件 4 可查到
- R20.15.2：event dialogue 完成后更新 task status=completed → 条件 4 不满足 → 用户后续评论可正常触发
- R20.15.3：event dialogue token 获取改为 Repo.bot_token（内部已委托 TokenResolver，R20.11）
- R20.15.4（R23.2 补充）：event dialogue 启动时更新 `devops_agent_session.last_active_at` + `status='active'` → 让 check_running_task session 兜底查询生效。原 R20.15.1 要求创建 agent_tasks 行，但 event dialogue 是内存执行架构（_conv_tasks 字典 + run_in_executor），不创建 DB task 行。改为通过 session heartbeat 让兜底生效，效果等价。
- R20.15.5（R23.2 补充）：event dialogue 完成时更新 `devops_agent_session.status='idle'` → 兜底不再命中 → 用户后续评论可正常触发流水线（R21.7.1）

### R21.5 PR 维度自循环检测（B2 遗漏）

**问题**：R20.1.8 check_running_task 只定义 WHERE repo_key=? AND issue_id=?，PR 评论自循环检测缺 pr_id 字段定义。

- R21.5.1：agent_tasks 表利用现有 issue_id 字段存储 PR 关联标识——PR 类事件的 issue_id 已存为 PR 号或 PR 关联 Issue 号（resolve_pr_issue_id 解析结果）
- R21.5.2：check_running_task 对 note_hook_pr 事件：先尝试 issue_id 查询（PR 关联的 Issue），如果查不到 → 按 repo_key + event_type='note_hook_pr' 查询（PR 维度）
- R21.5.3：补充查询 SQL：`SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('running','dispatched') LIMIT 1`——issue_id 字段对 PR 事件存储 PR 号或关联 Issue 号，查询逻辑不变
- R21.5.4：如果 PR 无关联 Issue（issue_id=空或 PR 号格式如 #pr_123）→ 条件 4 降级到 R20.10.5 session 兜底查询

### R21.6 非 webhook 场景 task 行 issue_id 语义（B3 遗漏）

**问题**：飞书 VOD / Chat direct / autopilot / event dialogue 创建 task 行时无 issue 上下文，issue_id 填空则条件 4 永远匹配不到。

- R21.6.1：飞书 VOD agent 执行时 issue_id 填飞书消息关联的 Issue 号（VOD handler 从飞书消息解析 repo_key + issue_id）。如果无关联 Issue → issue_id 填空 → 条件 4 降级到 session 兜底（R20.10.5）
- R21.6.2：Chat direct 模式 issue_id 填会话关联的 Issue 号（conv.repo_key + conv.issue_number）。如果无关联 Issue → issue_id 填空 → 条件 4 降级到 session 兜底
- R21.6.3：autopilot issue_id 填巡检关联的 Issue 号（如有）。无关联 → issue_id 填空 → session 兜底
- R21.6.4：event dialogue issue_id 填事件关联的 Issue 号（evt.issue_id）。无关联 → issue_id 填空 → session 兜底
- R21.6.5：issue_id 为空时，条件 4 的 agent_tasks 查询不会命中（WHERE issue_id=? 用空串匹配不到有值行）→ 降级到 R20.10.5 session 兜底查询（devops_agent_session.last_active_at > now-300s）
- R21.6.6：session 兜底查询是 repo 级（无 issue_id），会覆盖该仓库所有 issue/PR 的评论——存在误拦风险（见 R21.7）

### R21.7 check_running_task session 兜底误拦分析（B4 遗漏）

**问题**：session 兜底查询（devops_agent_session.last_active_at > now-300s AND status=active）是 repo 级，agent 完成但 session 未置 inactive 时，host 用户对该仓库任何 issue 的评论都会被误拦最多 5 分钟。

- R21.7.1：agent 执行完成后立即更新 devops_agent_session.last_active_at = NULL 或 status = 'idle'（如果当前设计是保持 active），缩小误拦窗口
- R21.7.2：session 兜底查询窗口从 300s 缩短到 60s——agent 完成后 60s 内的评论才可能被误拦（大部分 agent 完成后用户不会立即评论）
- R21.7.3：误拦场景用例补充：
  1. agent 完成 → session last_active_at 更新 → task status=completed
  2. 用户 30s 后对该仓库另一个 issue 评论
  3. 条件 4 agent_tasks 查询：issue_id 不同 → 不命中
  4. 条件 4 session 兜底：last_active_at > now-60s → 命中 → 误拦
  5. 60s 后用户再次评论 → session 兜底不命中 → 放行
  6. 可接受——最多 60s 误拦窗口，用户重试即可
- R21.7.4：如果 agent 执行完后立即更新 session status='idle'（R21.7.1），则 session 兜底查询 status='active' 不命中 → 无误拦

## 设计方案

### 5.1 问题本质

路径 B（用户 token 驱动）下，agent 用用户 token 调 VCS API，所有 API 操作的 actor 是用户而非 bot。现有 webhook 过滤链路的自循环检测依赖 actor 是否在 bot_usernames 列表中——路径 B 的 host 用户不在该列表中，导致 agent 自身动作的 webhook 回声不被拦截，触发重复执行。

核心矛盾：路径 B 下 agent 和用户用同一个 token（用户 token），actor 完全相同（host_user VCS username）。任何仅基于 actor 的检测都无法区分"agent 回声"和"用户主动操作"。简单判断 `actor == host_vcs_username → 跳过` 会导致用户自己发评论也被拦截，无法触发流水线。

设计约束：
- 不能将 host_user 加入 bot_usernames（会拦截用户的正常操作，见场景 17.2）
- 不能依赖评论标记（agent 直接调 API 发的评论无标记，见场景 12.1）
- 不能仅基于 actor 判断（actor 相同无法区分，见场景 18）

### 5.2 解决方案：四条件组合判断

引入 issue 状态 + task 执行状态 + 开发者指令豁免的三条件组合判断。仅当四条全部满足时，才视为 agent 回声跳过：

1. 仓库是路径 B（host_user_id > 0）
2. actor == host_vcs_username（host 用户的 VCS 平台用户名）
3. event_type in (issue_comment, note_hook_pr, issue_updated)（agent 可能发的操作类型）
4. 该 issue/PR 有正在执行的 task（agent_tasks.status in (running, dispatched) 或 devops_agent_session.last_active_at > now-300s）

条件 4 不满足 → 说明没有 agent 在执行 → actor 的操作是用户主动操作 → 放行。

开发者指令豁免：评论内容含 /done /cancel /resume 等指令时，即使四条件满足也不拦截（用户指令优先，先于自循环检测处理）。

### 5.3 host_vcs_username 获取与同步

#### 5.3.1 获取方式

优先从 managed_repos.host_vcs_username 列读取（R16.5.5 新增列），为空时 fallback 到 external_identities 查询 host_user_id 对应的 provider username（VCS 平台 login 字段，不是 grape account username）。

```python
def get_host_vcs_username(repo_key, provider):
    """获取路径 B 仓库的 host 用户 VCS 平台用户名。
    优先从 managed_repos.host_vcs_username 读取（R16.5.5），
    为空时 fallback 到 external_identities 查询。"""
    # 1. 查 managed_repos.host_vcs_username
    username = get_managed_repo_field(repo_key, "host_vcs_username")
    if username:
        return username
    # 2. fallback: 从 external_identities 查 host_user_id 对应的 provider username
    host_user_id = get_host_user_id(repo_key)
    if host_user_id > 0:
        identity = get_external_identity(host_user_id, provider.provider_name)
        return identity.get("username", "") if identity else ""
    return ""
```

#### 5.3.2 同步时机

每次验证 token 有效性（provider.verify_token）时，如果返回的 login 与 DB 中的 host_vcs_username 不一致 → 更新 DB（用户改了 VCS 平台用户名的场景）。覆盖路径：
- 托管时从 external_identities 同步写入 host_vcs_username
- token 验证（provider.verify_token）返回 login 变更时更新
- 路径 B → 路径 A 升级时清空 host_vcs_username（host_user_id=0 时无意义）

#### 5.3.3 查询失败降级

如果 host_vcs_username 查询失败（external_identities 已删除等）→ 降级为不检测（不阻断 pipeline），记录 warning。

### 5.4 task 执行状态查询

```python
def check_running_task(repo_key, issue_id):
    """检查该 issue 是否有正在执行的 task（R20.1.8 + R20.10.5）。
    
    两级查询：
    1. agent_tasks.status=running/dispatched（webhook pipeline 触发的 task）
    2. devops_agent_session.last_active_at > now-300s AND status=active
       （飞书 VOD / Chat direct 等非 webhook 执行场景）
    """
    with db_lock:
        c = db.cursor()
        # 1. 查 agent_tasks
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? "
            "AND status IN ('running','dispatched') LIMIT 1",
            (repo_key, issue_id)
        )
        if c.fetchone():
            return True
        # 2. 降级：查 devops_agent_session 最近活跃
        c.execute(
            "SELECT 1 FROM devops_agent_session WHERE repo_key=? "
            "AND status='active' AND last_active_at > datetime('now', '-300 seconds') LIMIT 1",
            (repo_key,)
        )
        return c.fetchone() is not None
```

### 5.5 开发者指令检测

开发者指令检测移到自循环检测之前（pipeline 步骤顺序调整：2.0b 开发者指令 → 2.0 自循环检测），确保用户指令不被自循环检测误拦截。

```python
def check_developer_command(note_content):
    """检查评论内容是否含开发者指令（/done /cancel /resume 等）。"""
    note_lower = (note_content or "").lower().strip()
    return any(kw in note_lower for kw in [
        "/done", "完成任务", "任务完成", "确认完成",
        "/cancel", "取消任务",
        "/resume", "恢复任务"
    ])
```

### 5.6 pipeline 步骤调整

现有 pipeline 步骤顺序：
```
2.0 自循环检测（actor in bot_usernames）
2.0a 标记检测（grape-auto-comment）
2.0b 开发者指令检测
2.1 L1 过滤
2.2 路由
```

改为：
```
2.0b 开发者指令检测（移到最前，R20.6.2）
  → 含 /done /cancel /resume → 处理指令 → skip
2.0 自循环检测（通过 handler.is_self_loop_actor 调用，R18.6.8）
  → 路径 A：actor in bot_usernames → skip
  → 路径 B：四条件组合判断 → skip
2.0a 标记检测（grape-auto-comment）
  → 系统回帖含标记 → skip
2.1 L1 过滤
2.2 路由
```

### 5.7 需求项

R20.1-R20.6 各需求项见上方「需求项」章节。pipeline 步骤调整总结：
- R20.6.1：2.0 自循环检测统一走 `handler.is_self_loop_actor()`（R18.6.8），路径 A / 路径 B 逻辑收敛到 Handler 内部
- R20.6.2：开发者指令检测移到自循环检测之前（2.0b → 2.0）

### R21.5 PR 维度 issue_id 复用

R21.5.1：agent_tasks 表利用现有 issue_id 字段存储 PR 关联标识——PR 类事件的 issue_id 已存为 PR 号或 PR 关联 Issue 号（resolve_pr_issue_id 解析结果），无需新增 pr_id 列。

R21.5.2：check_running_task 对 note_hook_pr 事件：先尝试 issue_id 查询（PR 关联的 Issue），如果查不到 → 按 repo_key + event_type='note_hook_pr' 查询（PR 维度）。

R21.5.4：如果 PR 无关联 Issue（issue_id=空或 PR 号格式如 #pr_123）→ 条件 4 降级到 R20.10.5 session 兜底查询。

### R21.6 各场景 issue_id 填法

| 场景 | issue_id 填法 | 无关联 Issue 时 |
|------|--------------|----------------|
| 飞书 VOD agent | 飞书消息关联的 Issue 号 | 填空 → session 兜底 |
| Chat direct | 会话关联的 Issue 号（conv.repo_key + conv.issue_number） | 填空 → session 兜底 |
| autopilot | 巡检关联的 Issue 号 | 填空 → session 兜底 |
| event dialogue | 事件关联的 Issue 号（evt.issue_id） | 填空 → session 兜底 |

issue_id 为空时，条件 4 的 agent_tasks 查询不会命中（WHERE issue_id=? 用空串匹配不到有值行）→ 降级到 R20.10.5 session 兜底查询。

session 兜底查询是 repo 级（无 issue_id），会覆盖该仓库所有 issue/PR 的评论——存在误拦风险（见 R21.7）。

### R21.7 session 兜底误拦：窗口 60s + idle

session 兜底查询（devops_agent_session.last_active_at > now-60s AND status=active）是 repo 级，agent 完成但 session 未置 idle 时，host 用户对该仓库任何 issue 的评论都会被误拦最多 60s。

**缓解措施**：
- R21.7.1：agent 执行完成后立即更新 devops_agent_session.last_active_at = NULL 或 status = 'idle'
- R21.7.2：session 兜底查询窗口从 300s 缩短到 60s
- R21.7.4：如果 agent 执行完后立即更新 session status='idle'（R21.7.1），则 session 兜底查询 status='active' 不命中 → 无误拦

**误拦场景用例**（R21.7.3）：
1. agent 完成 → session last_active_at 更新 → task status=completed
2. 用户 30s 后对该仓库另一个 issue 评论
3. 条件 4 agent_tasks 查询：issue_id 不同 → 不命中
4. 条件 4 session 兜底：last_active_at > now-60s → 命中 → 误拦
5. 60s 后用户再次评论 → session 兜底不命中 → 放行
6. 可接受——最多 60s 误拦窗口，用户重试即可

## 涉及代码文件

| 文件 | 函数/方法 | 职责 |
|------|----------|------|
| `src/api/event_pipeline.py` | `run_pipeline` 自循环检测段（2.0/2.0a/2.0a2/2.0b 步骤） | pipeline 步骤顺序调整：2.0b 开发者指令 → 2.0 自循环检测（路径 A `actor in bot_usernames` + 路径 B 四条件组合）→ 2.0a 标记检测 → 2.0a2 issue_updated 编辑标记 |
| `src/api/event_pipeline.py` | `check_running_task`（新增） | 两级查询：agent_tasks.status=running/dispatched + devops_agent_session.last_active_at > now-60s 兜底 |
| `src/api/event_pipeline.py` | `check_developer_command`（新增） | 评论内容含 /done /cancel /resume 等指令时豁免 |
| `src/user/db.py` | `_is_user_token_repo` | 判断仓库是否走路径 B（host_user_id > 0 且前三级 token 全空），自循环检测条件 1 依赖此判断 |
| `src/user/db.py` | `get_host_vcs_username`（新增） | 优先从 managed_repos.host_vcs_username 读取，fallback 到 external_identities 查询 |
| `src/api/routes/webhook.py` | gitcode webhook / github webhook 路由 | 调 `run_pipeline` 异步触发 pipeline，自循环检测在 pipeline 内部执行 |

## 验收标准

1. **四条件组合判断**：路径 B 仓库 + actor == host_vcs_username + event_type in (issue_comment, note_hook_pr, issue_updated) + 有正在执行的 task → 四条全满足才跳过；条件 4 不满足时放行用户操作
2. **开发者指令豁免**：评论含 /done /cancel /resume /完成任务 /取消任务 /恢复任务 时，即使四条件满足也不拦截（步骤 2.0b 优先于 2.0）
3. **host_vcs_username 获取**：优先从 managed_repos.host_vcs_username 列读取，为空时 fallback 到 external_identities；查询失败时降级为不检测 + 记录 warning
4. **host_vcs_username 同步**：provider.verify_token 返回 login 与 DB 不一致时更新 DB；路径 B → 路径 A 升级时清空
5. **task 执行状态查询**：agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('running','dispatched') LIMIT 1 有结果 → True；无结果时降级到 devops_agent_session.last_active_at > now-60s 兜底
6. **pipeline 步骤顺序**：2.0b 开发者指令 → 2.0 自循环检测（handler.is_self_loop_actor）→ 2.0a 标记检测 → 2.0a2 issue_updated 编辑标记 → 2.1 L1 过滤 → 2.2 路由
7. **向前兼容**：路径 A 仓库（host_user_id == 0）不受影响，原有 bot_usernames 检测不变
8. **PR 维度**：note_hook_pr 事件的 issue_id 存 PR 号或关联 Issue 号；PR 无关联 Issue 时降级到 session 兜底
9. **非 webhook 场景 issue_id**：飞书 VOD / Chat direct / autopilot / event dialogue 的 task 行 issue_id 填关联 Issue 号，无关联时填空 → session 兜底
10. **session 兜底误拦窗口**：查询窗口 60s（从 300s 缩短）；agent 完成后立即更新 session status='idle' 或 last_active_at=NULL → 无误拦
11. **fail_task 回帖降级**：路径 B 回帖失败时不静默（warning + COMMENT_POST_FAILED），不重试，dashboard 显示增强提示；有全局 bot token 时 best-effort 尝试
12. **执行期间过期保护**：_post_result_to_issue 失败（401）时 task 仍标记 completed + append RESULT_POST_FAILED；预检仅对 OAuth token 有效
13. **auto_upgrade 保护**：host token 过期 / 删旧 webhook 失败 / webhook 重建失败 → 仓库保持路径 B，不写 DB；邀 bot + accept + webhook + DB 全成功才算升级
14. **webhook 权限提示**：非 admin 用户托管时 setup_webhook 403 → 返回 WEBHOOK_PERMISSION_DENIED + 指引文案，前端 i18n 映射
15. **一致性**：pipeline 步骤 2.0 自循环检测统一走 `handler.is_self_loop_actor()`，消除 `if actor in bot_usernames` + `if _is_user_token_repo` 散落分支
16. **时序竞态可接受**：agent 完成到 task 状态更新 completed 之间的窗口，agent 评论可能被放行——最多多一次 pipeline 触发，由下次自循环检测兜底

## 依赖场景

- **依赖 04-token-resolver 的 Handler.is_self_loop_actor**（R18.6.8）：pipeline 步骤 2.0 自循环检测统一通过 `TokenActionHandler.is_self_loop_actor()` 调用，路径 A（actor in bot_usernames）和路径 B（四条件组合）逻辑收敛到 Handler 内部，消除 event_pipeline.py 中 `if actor in bot_usernames` + `if _is_user_token_repo` 散落分支
- **依赖 04-token-resolver 的 Handler.get_comment_token**（R18.6.9）：R20.2 fail_task 回帖降级的 token 选择（用户 token → 全局 bot token best-effort → 空）通过 Handler.get_comment_token() 实现
- **依赖 04-token-resolver 的 Handler.post_issue_comment**（R18.6.10）：R20.3 _post_result_to_issue 改为通过 handler.post_issue_comment() 调用，try-except 保护 + 失败返回 False
- **依赖 02-expiry-framework 的 R16.5.5 DB schema**：managed_repos 新增 host_vcs_username 列（R20.1.4 缓存依赖）
- **依赖 06-token-scenario-coverage 的 R20.10.5 session 兜底**：check_running_task 两级查询的第二级（devops_agent_session.last_active_at）来自 R20.10.5 非 webhook 场景自循环检测覆盖
- **被 11-r21-gaps 依赖**：R21.5-R21.7（PR 维度 + issue_id 语义 + session 兜底误拦）是 R21 gaps 的组成部分，11-r21-gaps 文档的 check_running_task 相关内容依赖本文档
