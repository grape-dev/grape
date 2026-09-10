# issue_opened 智能推荐双向兜底方案

## 一、需求

**目标**：用智能推荐替换旧的"默认场景"机制，作为 GitCode 直接创建 Issue 时的兜底。Task Center 创建任务时用户仍可自选场景。

**背景**：
- 旧机制：仓库设一个默认场景（dev_scene），GitCode 直接进来的 Issue 都走这个场景。多人协作下默认场景被改会影响所有人。
- 新机制：GitCode 直接进来的 Issue 走智能推荐，由 LLM 根据任务描述匹配最优场景。Task Center 创建的任务走用户自选场景。

**双向兜底**：
- 正向：鼓励用户在 Task Center 创建任务并自选场景（精准、可控）
- 反向：GitCode 直接创建的 Issue 自动走智能推荐（不需要用户干预）

## 二、当前流程 vs 新流程

### 当前流程

```
issue_opened
  ├─ Task Center 创建：set_scene_override_flag(True/False) → webhook → run_pipeline
  │   ├─ allow_scene_override=True → 智能推荐 → 推荐 scene_id → 重派 VOD
  │   └─ allow_scene_override=False → 直接用用户选的 scene_id 走 VOD
  └─ GitCode 直接创建（webhook）：无 issue_state → allow_scene_override=False → 用 dev_scene 走 VOD
```

问题：
- GitCode 直接创建的 Issue 不走推荐，走 dev_scene
- dev_scene 被协作者修改后影响所有直接创建的 Issue
- allow_scene_override 开关 + 竞态重试增加了复杂度

### 新流程

```
issue_opened
  ├─ Task Center 创建（用户自选场景）：写 scene_id 到 issue_state → webhook → run_pipeline
  │   → issue_state 有 scene_id → 跳过推荐，直接用用户选的场景走 VOD
  │
  └─ GitCode 直接创建（无 scene_id）：webhook → run_pipeline
      → issue_state 无 scene_id → 智能推荐 → 推荐 scene_id → 重派 VOD
```

**判断逻辑**：issue_state.scene_id 有值 → 用户已选场景，跳过推荐；scene_id 为空 → 走智能推荐。

不再需要 allow_scene_override 开关。不再需要 dev_scene 作为默认场景。

### 推荐失败处理

推荐失败（如仓库无场景配置、LLM 无法匹配）时，**不兜底到 dev_scene**。承认推荐失败，流水线终止，在日志和 Issue 评论中说明原因。不浪费用户资源跑错误场景。

## 三、旧流程组件评估

### 删除

| 组件 | 文件 | 删除原因 |
|------|------|----------|
| `set_scene_override_flag()` | session_store.py | 不再需要开关 |
| `get_scene_override_flag()` | session_store.py | 同上 |
| allow_scene_override 竞态重试 | event_pipeline.py | 不再读 allow_scene_override，无竞态 |
| Task Center 的 set_scene_override_flag | tasks.py | 删除 set_scene_override_flag 调用，保留 vod-auto 判断逻辑（选 vod-auto 不写 scene_id） |
| `scene_vod_auto` locale | zh/tasks.json, en/tasks.json | 文案保留"智能推荐"不变，不再删除 |
| dev_scene 自动带入 issue_state | session_store.py set_issue_stage | 不再用 dev_scene 作为初始 scene_id |

### 保留

| 组件 | 文件 | 说明 |
|------|------|------|
| Task Center 场景选择 | tasks.py + tasks.js | 用户仍可自选场景，选了就写 scene_id |
| `set_issue_scene()` | session_store.py | Task Center 选场景时写入 + 推荐完成后写入 |
| `get_issue_scene()` | session_store.py | 读取 issue 级场景（A2A 交办用） |
| `set_issue_stage()` | session_store.py | issue_opened 初始化 issue_state（不再带入 dev_scene） |
| `build_scene_recommender_prompt()` | prompt_builder.py | 推荐 prompt 构建 |
| `_load_scene_recommend_config()` | prompt_builder.py | 读取用户经验规则 |
| `_sanitize_recommend_config()` | prompt_builder.py | 场景删除时自检 |
| `_redispatch_vod_after_recommend()` | event_pipeline.py | 推荐完成后重派 VOD |
| `complete_task` 中的推荐完成处理 | service.py | 远程 worker 完成回调 |
| opencode 引擎限定 | event_pipeline.py | 推荐任务强制 opencode 认领 |
| 智能推荐规则弹窗 | dashboard.js | 用户配置兜底场景 + 路由规则 |
| `scene_recommend_config` 字段 | managed_repos 表 | 存储用户经验规则 |
| 智能推荐规则 API | repos.py | GET/PUT 推荐配置 |
| `allow_scene_override` 列 | issue_state 表 DDL | 列保留不删（兼容旧库），不再读写 |

### 修改

| 组件 | 文件 | 修改内容 |
|------|------|----------|
| issue_opened 路由 | event_pipeline.py | 删除 allow_scene_override 读取 + 竞态重试；改为检查 issue_state.scene_id：有值跳过推荐，空值走推荐 |
| `set_issue_stage` | session_store.py | 不再从 dev_scene 带入 scene_id，初始为空 |
| Task Center 场景选择 | tasks.py | 删除 vod-auto 分支和 set_scene_override_flag；保留用户选具体场景时 set_issue_scene |
| 前端 Task Center | tasks.js | 场景下拉框保留，默认选项改为"留空（自动推荐）"而非"智能推荐" |

## 四、实现详情

### 4.1 event_pipeline.py — issue_opened 路由

**现在：**
```python
_is_scene_recommend = False
if event_type == "issue_opened":
    agent_type = "vod"
    _rid_early = resolve_issue_id(...)
    if _rid_early:
        for _attempt in range(3):  # 竞态重试
            _flag = get_scene_override_flag(repo_key, _rid_early)
            if _flag:
                _is_scene_recommend = True
                break
            time.sleep(0.5)
```

**改为：**
```python
if event_type == "issue_opened":
    agent_type = "vod"
    # 判断是否需要推荐：issue_state 有 scene_id → 用户已选场景，跳过推荐
    # GitCode 直接创建的 Issue 无 scene_id → 走智能推荐
    _rid_early = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
    _is_scene_recommend = True  # 默认走推荐
    if _rid_early:
        try:
            from src.api.session_store import get_issue_scene
            _existing_scene, _ = get_issue_scene(repo_key, _rid_early)
            if _existing_scene:
                _is_scene_recommend = False
                log.info(f"Pipeline: issue_opened 已有场景 {_existing_scene}，跳过推荐")
        except Exception:
            pass
```

### 竞态消除方案

**问题**：Task Center 创建 Issue 后 GitCode webhook 立即触发，可能比 `set_issue_scene` 先到。此时 scene_id 为空，会多跑一次推荐。

**解决**：issue_opened 路由中检查 scene_id 时加短延迟重试。每 0.5 秒检查一次，共检查 3 次（最多等待 1 秒）：

- 第 1 次查到 scene_id → 用户已选场景，跳过推荐，直接走 VOD
- 第 2、3 次查到 scene_id → 同上
- 3 次都没查到 → GitCode 直接创建的 Issue，走智能推荐

**event_pipeline.py issue_opened 路由：**
```python
if event_type == "issue_opened":
    agent_type = "vod"
    _rid_early = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
    _is_scene_recommend = True  # 默认走推荐
    if _rid_early:
        import time as _time
        for _attempt in range(3):
            try:
                from src.api.session_store import get_issue_scene
                _existing_scene, _ = get_issue_scene(repo_key, _rid_early)
                if _existing_scene:
                    _is_scene_recommend = False
                    log.info(f"Pipeline: issue_opened 已有场景 {_existing_scene}（attempt={_attempt+1}），跳过推荐")
                    break
                if _attempt < 2:
                    _time.sleep(0.5)
            except Exception:
                break
    # _is_scene_recommend=True → 走推荐；False → 用已有 scene_id 走 VOD
```

**效果**：
- GitCode 直接创建 → 3 次检查都没 scene_id → 走推荐（等待 1 秒，可接受）
- Task Center 选场景 + set_issue_scene 先到 → 第 1 次就查到 → 跳过推荐，不浪费
- Task Center 选场景 + webhook 先到 → 第 2 或 3 次查到 → 跳过推荐，最多等 0.5-1 秒

**为什么不直接用之前的"推荐完成后检查覆盖"方案**：
- 那个方案推荐已经跑了，token 已消耗
- 这个方案在推荐前就判断，用户选了场景就不跑推荐，零浪费

### 4.2 tasks.py — Task Center 建任务

**现在：**
```python
# 2) 场景选择 + 智能推荐开关
try:
    from src.api.session_store import set_scene_override_flag
    if body.scene_id and body.scene_id != "vod-auto":
        set_scene_override_flag(repo_key, f"#{issue_num}", False)
        set_issue_scene(repo_key, f"#{issue_num}", body.scene_id, ...)
    else:
        set_scene_override_flag(repo_key, f"#{issue_num}", True)
except Exception:
    ...
```

**改为：**
```python
# 2) 场景选择：用户选了具体场景 → 写入 scene_id；选智能推荐 → 不写，等 webhook 触发推荐
try:
    if body.scene_id and body.scene_id != "vod-auto":
        from src.api.session_store import set_issue_scene
        from src.runtime.engine_select import _repo_user_id
        scene_uid = _repo_user_id(repo_key) or 0
        set_issue_scene(repo_key, f"#{issue_num}", body.scene_id, scene_user_id=scene_uid)
        # 选了具体场景 → scene_id 有值 → issue_opened 跳过推荐
    # 选智能推荐（vod-auto）或留空 → 不写 scene_id → issue_opened 走推荐
except Exception as _e:
    from src.config import log
    log.warning(f"create_task: set_issue_scene failed: {_e}")
```

删除 set_scene_override_flag。保留 vod-auto 选项：选了 vod-auto 不写 scene_id，webhook 进来时 scene_id 为空 → 走推荐。选了具体场景写 scene_id → 跳过推荐。

### 4.3 session_store.py — set_issue_stage

**现在：**
```python
# 新建：从 managed_repos.dev_scene 带入 scene_id
try:
    from src.user.db import get_repo_scene
    _init_scene = get_repo_scene(repo_key) or ""
except Exception:
    pass
c.execute("INSERT INTO issue_state (... scene_id ...) VALUES (... ? ...)", (..., _init_scene, ...))
```

**改为：**
```python
# scene_id 初始为空，由 Task Center set_issue_scene 或智能推荐写入
c.execute("INSERT INTO issue_state (... scene_id ...) VALUES (... '' ...)", (..., "", ...))
```

### 4.4 tasks.js — 前端

场景下拉框保留，三个选项不变：
- "智能推荐"（value=""，即 vod-auto）→ 不写 scene_id → 走推荐
- 具体场景列表 → 选了写 scene_id → 跳过推荐

默认选中"智能推荐"。文案 `scene_vod_auto` 保持"智能推荐"不变。

### 4.5 dev_scene 处理

dev_scene（managed_repos.dev_scene）不再在 issue_opened 时带入 issue_state。`get_issue_scene` 在 scene_id 为空时仍回退到 dev_scene，但这只用于 A2A 读取场景时的兜底（推荐完成前 scene_id 为空，A2A 不会在此期间触发）。

dev_scene 字段保留在表中，但不再影响 issue_opened 流程。后续可考虑删除。

## 五、文件变更清单

| # | 文件 | 操作 |
|---|------|------|
| 1 | `src/api/event_pipeline.py` | 删除 allow_scene_override 读取 + 旧竞态重试；改为检查 issue_state.scene_id（3 次 0.5 秒重试），有值跳过推荐，无值走推荐 |
| 2 | `src/api/routes/tasks.py` | 删除 set_scene_override_flag 调用；保留 vod-auto 逻辑（选 vod-auto 不写 scene_id，选具体场景写 scene_id） |
| 3 | `src/api/session_store.py` | 删除 `set_scene_override_flag()` / `get_scene_override_flag()`；`set_issue_stage` 不再带入 dev_scene |
| 4 | `src/api/static/js/pages/tasks.js` | 场景下拉框不变，保留"智能推荐"默认选项 |
| 5 | `src/api/static/locales/zh/tasks.json` | `scene_vod_auto` 保持"智能推荐"不变 |
| 6 | `src/api/static/locales/en/tasks.json` | 同上 |
