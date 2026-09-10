# 退役 dev_scene 设计文档

## 一、问题

GitCode 直接创建的 Issue 走智能推荐时被 dev_scene 回退逻辑阻断。

issue_opened 事件进入 pipeline 后（event_pipeline.py:821-844），检查 issue_state.scene_id 是否有值：
- 有值 → 跳过推荐
- 为空 → 走智能推荐

但 `get_issue_scene()`（session_store.py:109-124）在 issue_state.scene_id 为空时回退读 `managed_repos.dev_scene`：
```python
def get_issue_scene(repo_key, issue_id):
    st = get_issue_stage(repo_key, issue_id)
    if st and st.get("scene_id"):
        return st["scene_id"], ...
    return get_repo_scene(repo_key) or "", 0  # ← 回退到仓库级场景
```

HELLO-BLOG-SYS-34 仓库的 dev_scene=cloud-native（非空），导致 GitCode Issue #51 的 scene_id 虽然为空，但 get_issue_scene 返回 ("cloud-native", 0)，pipeline 认为已有场景 → 跳过推荐 → 用 cloud-native 的 vod 技能直接执行。

## 二、设计决策

新架构下场景是 Issue 级的——每个 Issue 独立选场景（Task Center 用户手选 / GitCode Issue 智能推荐）。仓库级「默认场景」（dev_scene）的概念冗余，应退役。

### 保留的能力
- 仓库托管时自动复刻"通用开发"场景数据到 devops_pipeline_scene 表（create_custom_scene 照常）
- ensure_default_agent_sessions 仍用 default mapping 创建 5 个 agent 会话（代码已有空值兜底：`get_repo_scene(repo_key) or "default"`）

### 去掉的能力
- dev_scene 作为 issue 级场景为空时的回退值
- apply_scene 写 dev_scene 指针
- apply_default_scene_to_repo 写 dev_scene 指针
- dashboard / settings「设为默认」按钮及对应前端逻辑
- agents 页面脱离场景上下文配引擎（`_update_scene_engine_ids` 读 dev_scene）

## 三、改动清单

### 3.1 核心修复（修 bug 根因）

#### 文件：src/api/session_store.py
**函数：get_issue_scene（第109-124行）**

去掉 dev_scene 回退。issue_state.scene_id 为空时返回 ("", 0)。

改前：
```python
def get_issue_scene(repo_key, issue_id):
    st = get_issue_stage(repo_key, issue_id)
    if st and st.get("scene_id"):
        return st["scene_id"], st.get("scene_user_id", 0) or 0
    try:
        from src.user.db import get_repo_scene
        return get_repo_scene(repo_key) or "", 0
    except Exception:
        return "", 0
```

改后：
```python
def get_issue_scene(repo_key, issue_id):
    st = get_issue_stage(repo_key, issue_id)
    if st and st.get("scene_id"):
        return st["scene_id"], st.get("scene_user_id", 0) or 0
    return "", 0
```

#### 文件：src/api/event_pipeline.py
**函数：resolve_skill_from_pipeline（第39-62行）**

去掉 dev_scene 回退。scene_id 为空时返回空技能。

改前：
```python
if not scene_id:
    try:
        from src.user.db import get_repo_scene
        scene_id = get_repo_scene(repo_key) or ""
    except Exception:
        scene_id = ""
if not scene_id:
    return ""
```

改后：
```python
if not scene_id:
    return ""
```

### 3.2 退役 dev_scene 写入

#### 文件：src/api/routes/scenes.py
**函数：apply_scene（第1475-1482行）**

去掉写 dev_scene 的代码块。

删除：
```python
    # 更新 managed_repos.dev_scene
    try:
        with db_lock:
            c2 = db.cursor()
            c2.execute("UPDATE managed_repos SET dev_scene=? WHERE repo_key=?", (body.scene_id, repo_key))
            db.commit()
    except Exception as e:
        log.warning(f"Scene apply: failed to update dev_scene: {e}")
```

apply_scene 函数本身保留（以防有其他调用方），仍更新 devops_agent_session。

#### 文件：src/api/pipeline_config.py
**函数：apply_default_scene_to_repo（第448-453行）**

去掉 set_repo_scene 调用。create_custom_scene 保留。

删除：
```python
        # 同步写 managed_repos.dev_scene
        try:
            from src.user.db import set_repo_scene
            set_repo_scene(repo_key, default_name)
        except Exception as _e:
            log.warning(f"apply_default_scene_to_repo: set dev_scene failed: {_e}")
```

### 3.3 退役 dev_scene 读取（UI 展示适配）

#### 文件：src/api/routes/dashboard.py（第998-1023行、第1128-1142行）

dashboard 读取 dev_scene 用于：
1. 展示当前场景 + pipeline nodes
2. 读取 advanced_options

代码已有空值兜底（第1017行）：`if not dev_scene or dev_scene not in scene_ids: dev_scene = scenes[0]["id"]`。dev_scene 为空时自动选第一个场景展示，不影响 UI。

改动：去掉读 dev_scene 的 DB 查询，直接置空，让兜底逻辑接管。

改前（第999-1005行）：
```python
    dev_scene = ""
    try:
        with db_lock:
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                dev_scene = row[0]
    except Exception:
        pass
```

改后：
```python
    dev_scene = ""
```

advanced_options 读取（第1131行 `if dev_scene:`）自动走空分支，不影响。

返回体中 `dev_scene` 字段（第1147行）保留，值始终为空。前端 dashboard.js 已有兜底（第63行）：`data.dev_scene && scenes.find(...) ? data.dev_scene : scenes[0].id`。

#### 文件：src/api/routes/agents.py

**_update_scene_engine_ids（第15-50行）**

这个函数读 dev_scene 找场景，更新 node 的 engine_runtime_ids。dev_scene 为空时直接 return。

改动：函数保留但变为空操作（dev_scene 始终为空 → 永远走 return）。无需改代码，自然失效。

**get_agent_detail（第225-235行）**

读 dev_scene 查引擎集合（UI 回显）。代码已有兜底（第225行 `_scene_id = "default"`，第232行 `if _row and _row[0]`）。

改动：去掉读 dev_scene 的 DB 查询。

改前：
```python
        _scene_id = "default"
        _uid = user.get("id", 0) if isinstance(user, dict) else 0
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
                _row = c.fetchone()
                if _row and _row[0]:
                    _scene_id = _row[0]
        except Exception:
            pass
```

改后：
```python
        _scene_id = ""
        _uid = user.get("id", 0) if isinstance(user, dict) else 0
```

`get_scene_engine_ids("")` 返回空列表 → pool 为空 → 前端回显无引擎集合。引擎配置改走 dashboard 场景编辑模式。

#### 文件：src/api/routes/pipeline.py（第307-313行）

pipeline 页面读 dev_scene 展示配置。代码已有兜底（第309行 `scene_id = "default"`）。

改前：
```python
    scene_id = "default"
    try:
        scene_id = get_repo_scene(repo_key) or "default"
    except Exception:
        pass
```

改后：
```python
    scene_id = ""
```

`get_pipeline_config("")` 返回 None → pipeline_config 为 None → 前端展示空 pipeline。

#### 文件：src/api/routes/events.py（第413-418行）

重排事件时附带 scene_id 元数据。

改前：
```python
                scene_id = "default"
                try:
                    from src.user.db import get_repo_scene
                    scene_id = get_repo_scene(evt["repo_key"]) or "default"
                except Exception:
                    pass
```

改后：
```python
                scene_id = ""
```

纯元数据，不影响逻辑。

#### 文件：src/runtime/repogit.py（第106-128行）

读 work_branch 时 get_issue_scene 优先读 issue scene，空则回退 dev_scene。

改动：get_issue_scene 改后不再回退 dev_scene（3.1 已改），issue 无场景 → scene_id 为空 → 直接返回空 work_branch。repogit.py 第123-128行的 dev_scene 回退代码变为死路径（get_issue_scene 已返回空 → scene_id 已为空 → 第129行 return ""）。

可选：删除第123-128行的 dev_scene 回退查询，减少无用 DB 查询。不删也不影响正确性。

### 3.4 前端改动

#### 文件：src/api/static/js/pages/dashboard.js

1. **「设为默认」按钮**（第367行、第593行）

去掉按钮和 `_applySceneFromCard` 函数（第239-257行、第780-795行）。

场景卡片上不再展示「设为默认」按钮，也不展示「当前默认」badge。场景详情页顶部去掉应用按钮区域。

2. **dev_scene 兜底**（第62-65行）

保留现有兜底逻辑，dev_scene 为空时自动选第一个场景展示。无改动。

3. **isApplied 判断**（第499-500行）

`isApplied = sceneId === currentScene` 始终为 false（currentScene 为空 → 不匹配任何 sceneId）。「当前默认」badge 不再展示。无改动。

#### 文件：src/api/static/js/pages/settings.js

**applyScene 函数**（第286-292行）

去掉。settings 页面不再有场景选择下拉和应用按钮。

#### 文件：src/api/static/js/pages/scene-market.js

**_applyScene 函数**（第560行起）

场景市场的「应用」按钮改为只调创建场景的端点（去掉内部 apply_scene 调用）。后端 `/scenes/market/{id}/apply` 改为只 create_custom_scene，不调 apply_scene。

### 3.5 历史数据清理

```sql
UPDATE managed_repos SET dev_scene = '';
```

清空所有仓库的 dev_scene 残留值，让 UI 展示与实际行为一致。

## 四、不改动的部分

| 项目 | 原因 |
|------|------|
| managed_repos.dev_scene 列 | 保留列，不删。DDL 默认值从 'default' 改为 '' 即可，避免历史代码报错 |
| get_repo_scene / set_repo_scene 函数 | 保留函数，不再有调用方主动使用，但不删除以防遗漏 |
| ensure_default_agent_sessions | 代码已有空值兜底（`or "default"`），dev_scene 为空时照常用 default mapping |
| apply_scene 函数 | 保留函数，去掉写 dev_scene 那段。仍更新 devops_agent_session |
| agent-skill-mapping.json | 保留，ensure_default_agent_sessions 读它的 default mapping |
| default-scene.json | 保留，apply_default_scene_to_repo 仍读它决定复刻哪个场景 |
| configs/agent-skill-mapping.json 里的 default scene | 保留，仅用于仓库注册时创建 agent 会话 |

## 五、影响矩阵

| 场景 | 改前 | 改后 |
|------|------|------|
| GitCode 直接创建 Issue | get_issue_scene 回退 dev_scene → 跳过推荐 → 用仓库场景执行 | scene_id 空 → 智能推荐正确触发 |
| Task Center 创建 Issue | set_issue_scene 先写入 → webhook 拿到 scene_id → 跳过推荐 | 不变 |
| 仓库托管 | 复刻场景 + 写 dev_scene | 复刻场景，不写 dev_scene |
| dashboard 展示 | 读 dev_scene 展示当前场景 | dev_scene 为空 → 自动选第一个场景展示 |
| 场景市场「应用」 | create + apply_scene（写 agent sessions + dev_scene） | 只 create（场景数据写入 DB） |
| agents 页面配引擎 | 读 dev_scene 找场景 → 更新 node engine_runtime_ids | _update_scene_engine_ids 失效 → 引擎配置改走 dashboard 场景编辑模式 |
| settings 页面 | 有场景选择下拉 + 应用按钮 | 去掉 |
