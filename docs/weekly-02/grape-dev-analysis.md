# 场景引擎绑定失效与任务卡死问题分析及解决方案

> 基于 Issue #112/#113/#114/#115 在「吕军涛的grape开发构建环境」场景下的运行数据，2026-09-12

## 一、问题现象

### 1.1 场景配置

场景 `c495ff46-c388-45bc-802d-0dfda0077845`（吕军涛的grape开发构建环境）配置如下：

| 节点 | skill | 指定引擎 (engine_runtime_ids) | 高级选项 |
|------|-------|-------------------------------|----------|
| vod | vod | [1094, 1092] | - |
| issue | issue-advance | [1094, 1097, 1100] | - |
| coding | implementation | **[2639]** | work_branch=multitask |
| qa | pr | **[2639]** | - |
| sre | grape-local-build | **[2639]** | - |

- runtime 2639 = hermes，scope=user，owner=songspace (user_id=39)，IP=203.0.113.1
- advanced_options: `{"work_branch": "multitask"}`（PR 目标分支应为 multitask）

### 1.2 实际执行情况

#### 引擎偏移汇总

| Issue | 阶段 | 场景指定引擎 | 实际执行引擎 | 偏移？ |
|-------|------|-------------|-------------|--------|
| #112 | vod | 1094,1092 | 1079 (opencode@master-002) | 是 |
| #112 | issue | 1094,1097,1100 | 1079 | 是 |
| #112 | coding | **2639** | 1079 | **是** |
| #112 | qa | **2639** | 2603 (hermes@devkit-testbot2) | **是** |
| #113 | vod | 1094,1092 | 1079 | 是 |
| #113 | issue | 1094,1097,1100 | 1079 | 是 |
| #113 | coding | **2639** | 1079 | **是** |
| #114 | vod | 1094,1092 | 1079 | 是 |
| #114 | issue | 1094,1097,1100 | 1079 | 是 |
| #114 | coding | **2639** | 1079 → 2572 → 2570 → 2604 | **是** |
| #115 | vod | 1094,1092 | 1079 | 是 |
| #115 | issue | 1094,1097,1100 | 1094 (hermes@coding-003) | 否 |
| #115 | coding | **2639** | 2570 (opencode@huaweicloud-skills-dev) | **是** |
| #115 | qa | **2639** | 2586 (hermes@devkit-testbot1) | **是** |

**结论：coding/qa/sre 阶段指定了 2639，但 100% 跑到了其他机器。vod/issue 阶段指定了特定引擎但几乎都跑到了 1079。**

#### 任务卡死汇总

| Issue | 卡死任务 | pool_engine_ids | user_id | 状态 | messages |
|-------|---------|-----------------|---------|------|----------|
| #112 | t_e437482208fb (qa) | [2639] | 38 | failed (0 messages) | 0 |
| #112 | t_5991f1214256 (sre) | [2639] | 38 | queued (仍卡着) | 0 |
| #113 | t_8a823fa41a6a (qa) | [2639] | 38 | failed (0 messages) | 0 |
| #113 | t_a537295a7531 (coding) | [2639] | 38 | queued (仍卡着) | 0 |
| #114 | t_0b969dfeb54b (qa) | [2639] | 38 | failed (0 messages) | 0 |
| #114 | t_4950a34bc4f3 (coding) | [2639] | 38 | failed (0 messages) | 0 |
| #115 | t_145f972c58c5 (coding) | [2639] | 38 | failed (0 messages) | 0 |
| #115 | t_055a262be31e (qa) | [2639] | 38 | failed (0 messages) | 0 |

**结论：所有 pool_engine_ids=[2639] 的任务全部卡死或失败，0 条 task_messages。**

#### PR 分支问题

| Issue | PR 分支 | 目标分支 | 期望目标分支 | 符合？ |
|-------|--------|---------|-------------|--------|
| #112 | feat/issue-112 | **multitask** | multitask | ✅ |
| #115 | feat/issue-115 | **multitask** | multitask | ✅ |

- #112 和 #115 的 PR 正确合入了 multitask 分支
- 但 #113 和 #114 的 coding 任务卡死/重跑，未产生 PR
- #111 的 PR 目标分支是 `local-liufei`（非 multitask），说明非此场景的 Issue 未遵循 multitask 约定

**结论：当 coding agent 成功执行时，work_branch=multitask 的 prompt 注入是生效的。但 SKILL.md 中的硬编码 `"base": "main"` 存在误导风险。**

---

## 二、根因分析

### 2.1 根因一：`_repo_user_id()` 取错用户（核心根因）

**问题链：**

```
场景创建者 songspace (user_id=39) 配置了引擎 2639 (scope=user, owner=39)
    ↓
但 _repo_user_id() 查 user_repos WHERE is_join=1 ORDER BY user_id LIMIT 1
    ↓
grape-dev/grape 有 3 人 join：38(Jason7602) < 39(songspace) < 45(HugLovesWine)
    ↓
取到 38 (Jason7602) 而非 39 (songspace)
    ↓
agent_tasks.user_id = 38
    ↓
2639 的 claim_by_tag (scope=user) 要求 task.user_id == 39
    ↓
38 ≠ 39 → 2639 无法认领
    ↓
pool_engine_ids=[2639] 阻止其他引擎认领
    ↓
2639 的 status=online → claim_by_tag 的 "全离线才清空" 检查不触发
    ↓
死锁：2639 接不了，其他引擎被挡，任务永远 queued
```

**代码位置：**
- `src/runtime/engine_select.py:143` — `_repo_user_id()` 函数
- `src/runtime/service.py:839` — `claim_by_tag()` 的 scope=user 分支
- `src/runtime/service.py:930` — pool_engine_ids "全离线才清空" 逻辑

**影响范围：** 所有多人 join 的仓库，场景指定了 scope=user 引擎时都会触发。

### 2.2 根因二：pool_engine_ids 死锁检测不充分

**当前逻辑（service.py:930）：**
```python
# 检查 pool_engine_ids 中的 id 是否全部 offline
_all_offline = True
for _eid in _allowed:
    c.execute("SELECT status FROM agent_runtimes WHERE id=?", (_eid,))
    if _e_row and (_e_row[0] or "") == "online":
        _all_offline = False
        break
if _all_offline:
    # 清空 pool_engine_ids 让 task 可被任意引擎认领
```

**问题：** 只检查 `status` 字段是否为 "online"，不检查：
1. `healthy` 是否为 1（引擎可能 status=online 但 healthy=0）
2. 引擎的 scope/owner 是否匹配 task.user_id（引擎在线但身份不匹配 → 认领不了）
3. 引擎是否有匹配的 tag（engine_capabilities）
4. 引擎的 busy_slots 是否已满

**结果：** 引擎 2639 status=online（确实在线），但 scope=user/owner=39 与 task.user_id=38 不匹配，实际无法认领。死锁检测认为"还有在线引擎"就不清空限制，导致永久卡死。

### 2.3 根因三：vod/issue 阶段引擎偏移

vod 和 issue 节点配置了 `engine_runtime_ids=[1094, 1092]` 和 `[1094, 1097, 1100]`，但这些引擎是 scope=global。任务创建时：

1. `_repo_user_id()` 返回 38
2. `pool_engine_ids` 被设为场景配置的 `[1094, 1092]` 等
3. 这些 global 引擎的 `claim_by_tag` 不受 user_id 限制，可以认领
4. 但 1079 (opencode@master-002, scope=global, tag=all) 也能认领，且可能先于 1094/1092 抢到

**根因：** `claim_by_tag` 是竞争式认领（先到先得），pool_engine_ids 理论上限定了范围，但存在以下问题：
- pool_engine_ids 中的引擎如果繁忙或响应慢，其他不在列表中的 tag=all 引擎无法介入
- 但实际上 vod/issue 的任务 pool_engine_ids 有值时，tag=all 的引擎应该被挡住
- 需要确认：vod/issue 任务是否真的带了 pool_engine_ids，还是被清空后广播了

从数据看，#112 vod 的 pool_engines 为空（`pool_engines=`），说明 pool_engine_ids 在某个环节被清空或未设置，导致变成了广播任务，被 1079 抢走。

### 2.4 根因四：SKILL.md 中 PR base 硬编码 "main"

**代码位置：** `skills/coding/implementation/references/new-dev-flow.md:124`
```
"head": "feat/issue-{number}", "base": "main",
```

虽然 prompt_builder 通过 `_build_scene_advanced_options()` 注入了：
```
- 工作基线分支：multitask（Issue 分支从该分支切出，PR/MR 目标分支必须为 multitask）
```

但 SKILL.md 中的硬编码 `"base": "main"` 仍然存在，当 agent 严格按照 SKILL.md 中的代码模板执行时，可能忽略 prompt 中的 work_branch 指引，直接用 "main" 作为 PR base。

**当前实际表现：** #112 和 #115 的 PR 正确合入了 multitask，说明 prompt 注入生效了。但这依赖于 agent 优先遵循 prompt 中的指引而非 SKILL.md 模板代码，存在不确定性。

### 2.5 根因五：初始 vod 任务创建失败

每个 Issue 的第一个事件（seq=5143/5145/5150/5156）都是 `status=failed`，报错：
> 中断：running 超时无任务执行记录，且 agent_tasks 无任何关联行——疑似创建任务失败

这说明初始 vod 任务创建后，agent_task 行没有落库或被异常清理。第二个事件（seq=5144/5146/5151/5157）才成功创建并执行 vod。这导致每个 Issue 都有重复的 vod 事件，浪费资源。

---

## 三、解决方案设计

### 3.1 修复 `_repo_user_id()` 优先取场景创建者

**问题：** `ORDER BY user_id LIMIT 1` 取到的是最早 join 的用户，不是场景创建者。

**方案：** 在 `get_scene_pool_engine_ids()` 和 pipeline enqueue 时，优先使用 `issue_state.scene_user_id`。如果 issue_state 有 scene_user_id 且 > 0，直接用它，不再调用 `_repo_user_id()`。

```python
# src/api/event_pipeline.py 修改
# 当前代码（约 L965-980）：
_issue_scene_uid = 0
if _issue_scene:
    _issue_scene, _issue_scene_uid = get_issue_scene(repo_key, _early_issue_id)
if not _issue_scene_uid:
    from src.runtime.engine_select import _repo_user_id
    _issue_scene_uid = _repo_user_id(repo_key)

# 修改为：不再回退到 _repo_user_id()，而是从 issue_state 直接取
# issue_state.scene_user_id 在场景绑定时写入，应为场景创建者的 user_id
```

**同时修复 issue_state.scene_user_id 写入逻辑：** 确保 VOD 首次识别场景时，将场景创建者的 user_id（而非 `_repo_user_id()` 的结果）写入 `issue_state.scene_user_id`。

### 3.2 增强 pool_engine_ids 死锁检测

**当前逻辑：** 只检查 `status == "online"`

**修改为：** 检查引擎是否**实际可认领**，包括 scope 匹配、healthy、probe_status、并发槽：

```python
# src/runtime/service.py claim_by_tag() 中的 pool_engine_ids 检查
_all_unable = True
for _eid in _allowed:
    c.execute("SELECT status, healthy, probe_status FROM agent_runtimes WHERE id=?", (_eid,))
    _e_row = c.fetchone()
    if not _e_row:
        continue  # 引擎不存在，视为不可用
    _status, _healthy, _probe = _e_row
    # 检查基本健康
    if _status != "online" or _healthy != 1 or _probe not in ("ok", "pending"):
        continue
    # 检查 scope 匹配
    _scope, _owner = _engine_scope_info(_eid)
    if _scope == "user":
        # 查引擎 owner 对应的 user_id
        _owner_uid = _get_user_id_by_gitcode(_owner)
        if _owner_uid != task_user_id:
            continue  # scope 不匹配，无法认领
    # 检查并发槽
    c.execute("SELECT busy_slots, max_concurrent FROM agent_runtimes WHERE id=?", (_eid,))
    _busy, _max = c.fetchone()
    if _busy >= _max:
        continue  # 已满载
    _all_unable = False
    break
if _all_unable:
    # 清空 pool_engine_ids，让任意健康引擎认领
    log.info(f"pool_engine_ids {_allowed} all unable (scope/health/slots), clearing for task {task_id}")
    c.execute("UPDATE agent_tasks SET pool_engine_ids='' WHERE id=?", (task_id,))
    db.commit()
    break
```

### 3.3 修复 SKILL.md 中 PR base 硬编码

**修改 `skills/coding/implementation/references/new-dev-flow.md`：**

将：
```json
"head": "feat/issue-{number}", "base": "main",
```

改为：
```json
"head": "feat/issue-{number}", "base": "{{WORK_BRANCH}}",
```

并添加说明：
> PR 的 base 分支使用场景配置的工作基线分支（prompt 中「工作基线分支」指定）。
> 如 prompt 中未指定工作基线分支，默认使用 "main"。

### 3.4 修复 vod/issue 阶段 pool_engine_ids 未设置

**问题：** vod 和 issue 的 pool_engine_ids 在实际任务中为空，导致变成广播任务。

**排查方向：**
1. 确认 `get_scene_pool_engine_ids()` 是否正确读取了场景的 `engine_runtime_ids`
2. 确认 vod/issue 阶段是否走了 plugin broadcast 路径（engine_id=0 时 pool_engine_ids 可能被设为空）
3. 如果 pool_engine_ids 为空，tag=all 的 global 引擎会竞争认领，无法保证引擎选择符合场景配置

**方案：** 在 `enqueue_task()` 时，如果场景配置了 `engine_runtime_ids`，无论 engine_id 是否为 0，都将 `engine_runtime_ids` 写入 `pool_engine_ids`，确保只允许指定引擎认领。

### 3.5 修复初始 vod 任务创建失败

**问题：** 每个 Issue 的首个 vod 事件都失败，报"无 agent_tasks 关联行"。

**排查方向：**
1. 检查首个 vod 事件的 pipeline 分发路径，是否在 `create_direct_task` 或 `enqueue_task` 时异常
2. 检查是否存在竞态条件（两个 vod 事件同时到达，第一个被清理）
3. 检查 sweeper 是否误删了刚创建的 task

**方案：** 在 pipeline 分发时增加重试逻辑，如果 enqueue 后 30 秒内 agent_tasks 中无对应行，自动重试一次。

---

## 四、优先级与实施计划

| 优先级 | 修复项 | 影响范围 | 预计工作量 |
|--------|--------|---------|-----------|
| P0 | 3.2 增强 pool_engine_ids 死锁检测 | 所有场景指定引擎的任务 | 2h |
| P0 | 3.1 修复 _repo_user_id / scene_user_id | 多人 join 仓库的场景引擎绑定 | 3h |
| P1 | 3.4 修复 vod/issue pool_engine_ids 未设置 | vod/issue 引擎选择不符合场景 | 2h |
| P1 | 3.3 修复 SKILL.md PR base 硬编码 | PR 合入错误分支风险 | 1h |
| P2 | 3.5 修复初始 vod 任务创建失败 | 每个 Issue 首次触发浪费资源 | 3h |

---

## 五、验证方案

### 5.1 死锁检测验证
1. 创建一个 pool_engine_ids=[2639] 的任务，user_id=38
2. 确认 30 秒内 pool_engine_ids 被自动清空
3. 确认任务被其他健康引擎认领

### 5.2 场景引擎绑定验证
1. 以 songspace (user_id=39) 身份触发新 Issue
2. 确认 issue_state.scene_user_id=39
3. 确认 agent_tasks.user_id=39
4. 确认 2639 能正常认领 coding/qa/sre 任务

### 5.3 PR 分支验证
1. coding agent 完成开发后创建 PR
2. 确认 PR base 分支为 multitask（非 main）
3. 确认 SKILL.md 模板中无硬编码 "main"

### 5.4 vod/issue 引擎验证
1. 触发新 Issue
2. 确认 vod/issue 任务带上了 pool_engine_ids
3. 确认只有场景指定的引擎能认领
