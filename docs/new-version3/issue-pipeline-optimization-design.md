# Issue Pipeline 时序优化方案

> 基于 GitCode Issue #7（signway/fluxgate）事故复盘。
> 分支：fix/issue-pipeline-optimization（基于 weekly-01 6771f07e）

## 一、事故回顾

Issue #7 全链路 30 分钟内完成，但出现了两个核心问题：

**问题一：Agent 回帖评论触发 webhook 回声，Issue Agent 被反复唤醒**

```
每个 Agent 完成后系统自动回帖到 Issue
  → GitCode 触发 issue_comment webhook → 回到 grape
    → 自循环检测失效（bot_username 配置与实际 token 账户不匹配）
      → 阶段矩阵路由：issue_comment + stage=coding/qa/sre → 给 Issue Agent
        → Issue Agent 被触发 → 审视后 A2A 交办 Coding 重构
```

QA #1 已通过并交给 SRE，正常流程应到 SRE 就结束。但 webhook 回声触发了 Issue Agent，导致 A2A 交办 Coding 重构，出现多余的 QA #2。

**问题二：QA 失败回退时 SRE 没被取消**

```
21:54  QA #1 误判通过 → A2A → SRE 启动
21:57  Issue Agent 被 webhook 回声触发 → A2A 交办 Coding 重构 → QA #2
21:59  QA #2 失败 → A2A 回退 Coding
       ↑ SRE 没被取消，继续跑
22:00  SRE 发布完成（不合格版本）
```

## 二、根因

### 根因一：A2A 回退时不取消下游任务

**位置**：`src/api/routes/webhook.py:327-368`

A2A 端点收到回退请求（如 QA→Coding）时，`set_issue_stage` 只改了 stage，没有取消已分派的 SRE 任务。

系统已有 `_cancel_issue_tasks`（events.py:695-710）和 `cancel_direct_task`（event_pipeline.py:232-275），但只在手动 API `flow_reset` 中调用。A2A 回退路径不自动调用。

并且 `_cancel_issue_tasks` 只调 `cancel_task`（DB 状态改为 cancelled），不调 `cancel_direct_task`（杀进程）。direct 模式下 DB 状态改了但 agent 子进程继续跑。

### 根因二：token 错乱导致自循环检测失效

**位置**：`src/api/event_pipeline.py:644-668`

**问题本质是 token 错乱——系统用错了 token。**

grape 的 Agent 执行时通过 `get_gitcode_token` 获取 GitCode API token 来发评论、PATCH Issue 等。自循环检测用 `managed_repos.bot_username` 检查 webhook 回来的 actor 是否是 bot。

当 `managed_repos.bot_gitcode_token` 对应的实际 GitCode 账户名与 `managed_repos.bot_username` 配置的值不一致时，自循环检测失效：

- Agent 用 token 发评论 → GitCode webhook 回来的 actor = token 对应的账户名
- bot_username 配的是另一个名字 → actor 不在 bot_usernames 列表中 → 自循环检测不拦截
- webhook 回声进入阶段矩阵路由 → Issue Agent 被触发

实测数据：

| 仓库 | bot_gitcode_token | GET /user 返回 | managed_repos.bot_username | 自循环检测 |
|------|-------------------|---------------|---------------------------|-----------|
| xiaowu2119/hello-skill | m4kj4Jfi... | grape_dev_bot | grape_dev_bot | ✅ 匹配，正常拦截 |
| signway/fluxgate | fMCnxDyW... | xiaowu2119 | grape_dev_bot | ❌ 不匹配，拦截失效 |

这是 token 配置问题，属于运维层面。代码层面无法区分"Agent 用 token 做的操作"和"用户用同一个 token 做的操作"——当 token 是用户个人 token 时，两者 actor 相同。配置正确时（专用 bot 账户 + bot_username 匹配）自循环检测天然工作，不需要额外代码。

**系统回帖不是流程交互，是通知副作用。** 系统回帖（`_post_result_to_issue`）是 Agent 完成后把结果发到 GitCode Issue 通知用户，纯展示用途。Agent 之间的流程推进全部通过 A2A 协议调用（curl /api/a2a），不走 GitCode webhook。但 GitCode 的 webhook 机制不区分"bot 发的评论"和"用户发的评论"——只要有评论就触发 webhook 回到 grape。grape 自己发的评论又被 GitCode 通知回来，这就是回声。回声不是系统设计的流程，是 GitCode webhook 机制的副作用。

### 根因三：A2A 端点无法校验 QA 结果（A2A gate 不可行）

**位置**：`src/api/event_pipeline.py:1646-1699`

QA agent 在 `stream_agent` 循环中途通过 curl 调 `/api/a2a`。A2A 端点立即同步处理。此时 QA event 仍为 running，result 还没写入 events 表。

推演证明任何 A2A gate 都无法正确工作：

```
QA #3 通过 → 调 A2A→SRE → 此时 QA #3 event 仍 running，result 为空
  查最新 QA event → running → 拦截 → 错误（QA #3 刚通过了）
  查最新 completed QA event → QA #2（failed）→ 拦截 → 错误（QA #3 已通过）
```

所以不在 A2A 端点做 gate，而是在 QA 失败后回退时取消已启动的 SRE。

## 三、修复方案

两个方案各自独立工作，互不依赖。不依赖 token 配置是否正确。

### 方案A：A2A 回退时取消下游任务（含杀进程）

**目标**：A2A 回退时取消已分派的下游任务并杀掉进程。如果 SRE 已完成则告警。不管什么原因导致回退，保证不合格产物不被发布。

**改动文件**：
- `src/api/routes/webhook.py`——A2A 端点回退检测 + 内联取消逻辑

**实现**：

webhook.py A2A 端点中，现有代码（:348-358）已有 `_stage_order` 和 `_cur_idx` / `_target_idx` 的计算，但只在跳步条件内执行。改为提到 if 块外面，并新增回退检测：

```python
# _stage_order 计算提到 if 块外面，确保回退场景也能获取
_stage_order = list(_pipeline_next.keys())
_cur_idx = _stage_order.index(_current_stage) if _current_stage in _stage_order else -1
_target_idx = _stage_order.index(agent_name) if agent_name in _stage_order else -1

# 跳步修正（原有逻辑）
if _expected_next and _expected_next != "done" and agent_name != _expected_next:
    if _target_idx > _cur_idx + 1:
        agent_name = _expected_next
        _target_idx = _stage_order.index(agent_name)

# 新增：回退检测 + 取消下游任务
if _target_idx < _cur_idx and _cur_idx > 0:
    _cancel_agents = [a for a in _stage_order[_cur_idx:] if a != "done"]
    try:
        from src.session_manager import db, db_lock
        from src.runtime.service import cancel_task
        from src.api.event_pipeline import cancel_direct_task
        from src.api.event_store import update_event_status

        with db_lock:
            c = db.cursor()
            placeholders = ",".join("?" * len(_cancel_agents))
            c.execute(
                f"SELECT id, event_seq, agent_type FROM agent_tasks "
                f"WHERE repo_key=? AND issue_id=? AND agent_type IN ({placeholders}) "
                f"AND status IN ('queued','dispatched','running')",
                (repo_key, f"#{issue_number}", *_cancel_agents))
            tasks = c.fetchall()

        _cancelled = 0
        _errors = []
        _sre_completed_alert = False

        for tid, tseq, ttype in tasks:
            try:
                cancel_task(tid)
                _cancelled += 1
            except Exception as _ce:
                _errors.append(f"cancel_task({ttype}:{tid}): {_ce}")
            try:
                if tseq:
                    cancel_direct_task(tseq)
            except Exception as _de:
                _errors.append(f"cancel_direct_task({ttype}:{tseq}): {_de}")
            try:
                if tseq:
                    update_event_status(tseq, "cancelled")
            except Exception:
                pass

        # SRE 已完成检测：查 events 表（权威状态）
        if "sre" in _cancel_agents:
            with db_lock:
                c2 = db.cursor()
                c2.execute(
                    "SELECT seq FROM events WHERE repo_key=? AND issue_id=? "
                    "AND agent_type='sre' AND status='completed' "
                    "ORDER BY seq DESC LIMIT 1",
                    (repo_key, f"#{issue_number}"))
                _sre_row = c2.fetchone()
            if _sre_row:
                _sre_completed_alert = True

        if _sre_completed_alert:
            log.warning(f"A2A: SRE already completed for issue=#{issue_number} — "
                        f"manual review needed, published artifact may need rollback")
        if _errors:
            log.warning(f"A2A: cancel errors: {_errors}")
        log.info(f"A2A: rollback cancel — {_cancelled} tasks cancelled, "
                 f"sre_alert={_sre_completed_alert}")
    except Exception as _re:
        log.warning(f"A2A: rollback cancel failed: {_re} — A2A continues, cancel may not have taken effect")
```

**异常处理**：

| 异常场景 | 处理方式 | 影响 |
|----------|----------|------|
| DB 异常 | except 兜底，不阻塞 A2A 交棒 | 回退正常执行，取消可能未生效 |
| cancel_task 失败 | 记 errors，继续其他 task | 该 task 未取消 |
| cancel_direct_task 进程已退出 | 返回 True，无副作用 | 无影响 |
| cancel_direct_task remote 模式 | 返回 False，DB 已 cancelled | worker 下次检测 |
| _pipeline_next 读取失败 | _stage_order 空 → 不取消 | 安全降级 |
| SRE 正在 push | SIGTERM 杀进程 | 需人工检查 |
| SRE 已 completed | 查不到活跃 task | 告警日志，无法撤销已发布产物 |

**边界情况**：

| 边界场景 | 行为 | 正确？ |
|----------|------|--------|
| QA→Coding 回退，SRE running | cancel_task + cancel_direct_task（杀进程） | ✅ 核心目标 |
| QA→Coding 回退，SRE queued | cancel_task（DB cancelled） | ✅ |
| QA→Coding 回退，SRE dispatched | cancel_task + cancel_direct_task | ✅ |
| QA→Coding 回退，SRE completed | 告警日志 | ✅ 无法取消 |
| Coding→Issue 回退 | 取消 coding+qa+sre 全部下游 | ✅ |
| 正常前进（QA→SRE） | _target_idx > _cur_idx → 不触发取消 | ✅ 不影响 |
| blog-publish 场景（跳过 qa） | stage_order 动态读取 | ✅ 兼容 |
| 用户手动 flow_reset | 仍用原有 _cancel_issue_tasks | ✅ 不冲突 |

### 方案B：系统回帖加隐藏标记，webhook 检测标记跳过

**目标**：系统自动回帖的评论不触发 webhook 回声。不依赖 bot_username 配置，独立工作。

系统回帖是 Agent 完成后把结果发到 GitCode Issue 通知用户的单向通知，不是流程交互。Agent 之间的流程推进通过 A2A 协议调用，不走 GitCode webhook。但 GitCode webhook 机制不区分"bot 发的评论"和"用户发的评论"——只要有评论就触发 webhook 回到 grape。grape 自己发的评论又被 GitCode 通知回来，这就是回声。方案B 通过在系统回帖中加隐藏标记，让 grape 识别出"这是我自己发的评论，不用再处理"。

**改动文件**：
- `src/api/event_pipeline.py`——3 个回帖函数加标记 + run_pipeline 检测标记 + _sync_issue_comments_to_conversation 过滤标记

**实现**：

改动点 1：`_post_result_to_issue`（:1834）评论末尾加标记

```python
_comment = f"[{agent_type.upper()} Agent] {result_text[:3000]}"
_comment += "\n<!-- grape-auto-comment -->"
```

改动点 2：`_post_subtask_completion_to_parent`（:1873）和 `_post_all_subtasks_done`（:1892）同步加标记

改动点 3：run_pipeline（:668 之后）增加标记检测

```python
if event_type in ("issue_comment", "note_hook_pr"):
    _payload = evt.get("payload", {}) or {}
    _oa = _payload.get("object_attributes", {}) or {}
    _note = str(_oa.get("note") or _payload.get("body") or "")
    if "<!-- grape-auto-comment -->" in _note:
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, "system_auto_comment")
        return result
```

改动点 4：`_sync_issue_comments_to_conversation`（:1973 附近）写入会话前过滤标记

```python
body = cm.get("body", "").replace("<!-- grape-auto-comment -->", "").rstrip()
```

**已知限制**：只覆盖系统回帖。skill curl 评论（Agent 通过 curl 自己发的评论）无标记，依赖自循环检测（token 配置正确时）和去重检查（兜底）。

## 四、方案协作关系

```
方案B：拦截系统回帖的 webhook 回声（不依赖 token 配置）
  → grape 自己发的评论，GitCode 通知回来时，grape 识别出"这是我自己发的"，不处理
  → 减少 Issue Agent 被回声误触发的概率

方案A：回退时取消下游任务 + 杀进程（不依赖 token 配置）
  → 不管 Issue Agent 是否被回声触发，回退时都取消下游
  → 保证不合格产物不被发布

两个方案各自独立工作，互不依赖。

token 配置正确时（专用 bot 账户 + bot_username 匹配）：
  自循环检测天然拦截所有 bot 回声
  方案B 冗余兜底，方案A 兜底回退

token 配置不正确时：
  自循环检测失效
  方案B 拦系统回帖，去重兜底 skill curl 评论
  方案A 兜底回退——即使中间过程错乱（多余 Agent 执行/评论），最坏后果（不合格版本发布）被兜住
```

## 五、事故路径覆盖验证

```
配置正确时（自循环检测生效）：
  QA #1 通过 → SRE 启动
  → 回帖 → webhook 回声 → 自循环检测拦截 → 跳过
  → Issue Agent 不被触发 → 不会出现 QA #2 → SRE 正常发布 ✅

配置不正确时（自循环检测失效）：
  → 系统回帖（含标记）→ 方案B 拦截 ✅
  → skill curl 评论（无标记）→ 可能触发 Issue Agent
  → 去重检查兜底（同类型 running → skip）
  → 如果没拦住 → A2A 交办 → QA #2 失败 → 回退
  → 方案A：取消 SRE → 不合格产物不被发布 ✅
```

## 六、验证结论

### 方案B 系统标记验证（已通过）

实测：向 GitCode Issue #7 发送含 `<!-- grape-auto-comment -->` 标记的评论，GitCode API 返回 body 完整保留标记，未被剥离。webhook payload 的 `object_attributes.note` 就是 GitCode 存储的 body，webhook 推回来时标记一定在。

## 七、改动影响评估

| 改动 | 文件 | 影响范围 | 风险 | 缓解 |
|------|------|----------|------|------|
| 方案A | webhook.py | A2A 回退路径 | 中 | 只在回退时触发；动态 stage order；except 兜底不阻塞 A2A |
| 方案B | event_pipeline.py | issue_comment/note_hook_pr 路由 | 低 | 只跳过含标记的评论；用户评论不受影响 |

## 八、改动文件清单

| 文件 | 方案 | 改动内容 |
|------|------|----------|
| src/api/routes/webhook.py | A | _stage_order 提到 if 块外 + 回退检测 + 内联取消逻辑 |
| src/api/event_pipeline.py | B | _post_result_to_issue 加标记 + _post_subtask_completion_to_parent 加标记 + _post_all_subtasks_done 加标记 + run_pipeline 标记检测 + _sync_issue_comments_to_conversation 过滤标记 |
| tests/test_webhook_echo_and_rollback.py | A+B | 新增测试 |
| tests/conftest.py | — | Python 3.9 测试兼容（如需） |

## 九、验收标准

1. 方案A：回退时运行中的 SRE 被取消（DB cancelled + 杀进程），已结束的 SRE 不误取消只告警
2. 方案B：系统回帖不触发 Issue Agent，用户评论仍正常触发
3. 正常前进（QA→SRE）不受影响
4. 新增测试覆盖，现有测试通过（pytest）
