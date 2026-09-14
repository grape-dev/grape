# 场景高级选项：工作分支 + 自定义 key:value 扩展

> 背景：当前场景中心仅支持配置 Agent 节点流水线（skill/引擎/流转），缺少项目级工作分支控制和
> 自定义参数注入能力。agent 的 worktree 分支基线硬编码为 `main → master` 兜底，无法按场景指定
> 其他分支作为基线。同时 prompt 中也无法注入项目自定义配置（如部署环境、测试命令等）。
>
> 本设计在场景级新增「高级选项」，包含强制的工作分支字段和用户自定义 key:value 扩展，
> 前者改变 worktree 基线分支来源，后者注入执行类 agent 的 prompt。

## 一、需求

### 1.1 功能需求

| # | 需求 | 说明 |
|---|------|------|
| 1 | 高级选项入口 | Dashboard 场景选择器区域，与「编辑场景」同级，新增「高级选项」按钮 |
| 2 | 工作分支字段 | 强制字段，默认 `main`；支持从仓库现有分支中选择；支持新建分支（调 GitCode API 在仓库中创建） |
| 3 | 自定义 key:value | 用户自由增删行，无数量限制；注入到 coding/qa/sre agent 的 prompt |
| 4 | 数据层级 | 场景级，存储在 `devops_pipeline_scene` 表，随场景绑定 |
| 5 | 工作分支作用 | 作为 base branch，从该分支切出 `feat/issue-{N}`（改变 `_default_base_branch` 来源） |

### 1.2 非目标

- 不替换现有的 `feat/issue-{N}` 自动分支逻辑（仅改变其 base 来源）
- 不改变 vod/issue agent 的 prompt（仅注入执行类 agent）
- 不改变场景节点流水线配置（高级选项独立于 nodes_json）

## 二、数据模型

### 2.1 表结构变更

`devops_pipeline_scene` 表新增一列：

```sql
ALTER TABLE devops_pipeline_scene ADD COLUMN advanced_options TEXT DEFAULT '{}';
```

当前表结构（`src/api/db_setup.py` L480）：

```sql
CREATE TABLE IF NOT EXISTS devops_pipeline_scene (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    repo_key    TEXT NOT NULL DEFAULT '',
    nodes_json  TEXT DEFAULT '[]',
    onboarding  TEXT DEFAULT 'repo-onboarding',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, repo_key)
)
```

新增列后：

```sql
    advanced_options TEXT DEFAULT '{}'   -- 场景高级选项 JSON
```

### 2.2 JSON 结构

```json
{
  "work_branch": "main",
  "custom_fields": [
    {"key": "deploy_env", "value": "staging"},
    {"key": "test_command", "value": "npm test"}
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `work_branch` | string | 是 | 工作基线分支名，默认 `"main"` |
| `custom_fields` | array | 否 | 自定义 key:value 列表，每项 `{key: string, value: string}` |
| `custom_fields[].key` | string | 是 | 配置项名称 |
| `custom_fields[].value` | string | 是 | 配置项值 |

### 2.3 历史数据兼容

- 已有 `devops_pipeline_scene` 记录：`advanced_options` 默认 `'{}'`，解析为空配置
- 空配置时 `work_branch` 为空 → repogit 走原逻辑（`main → master` 兜底），不影响现有行为
- 空配置时 `custom_fields` 为空 → prompt 不注入自定义段落，不影响现有 prompt

## 三、修改内容

### 3.1 DB 迁移

**文件**: `src/api/db_setup.py`

在现有 `_ensure_column` 迁移区域（L2105 附近 `managed_repos` 迁移之后）新增：

```python
# ===== devops_pipeline_scene 高级选项列 =====
try:
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "devops_pipeline_scene", "advanced_options", "TEXT DEFAULT '{}'")
        db.commit()
        log.info("Added devops_pipeline_scene.advanced_options column")
except Exception as e:
    log.warning(f"Add devops_pipeline_scene.advanced_options column failed: {e}")
```

幂等迁移，已有数据不受影响。与 `display_name` 列的迁移模式一致（`_try_set_display_name` 同款兜底）。

### 3.2 后端 API（4 个新接口）

**文件**: `src/api/routes/scenes.py`

#### 3.2.1 GET `/api/scenes/{scene_id}/advanced-options`

读取场景高级选项。

```
请求: GET /api/scenes/{scene_id}/advanced-options?repo={owner}/{repo}
鉴权: get_current_user
```

```python
@router.get("/{scene_id}/advanced-options")
def get_advanced_options(scene_id: str, user: dict = Depends(get_current_user), repo: str = ""):
    """读取场景高级选项"""
    # 1. 解析 repo_key
    # 2. 查 devops_pipeline_scene WHERE scene_id=? AND repo_key=? AND is_active=1
    # 3. 解析 advanced_options JSON
    # 4. 返回 {work_branch, custom_fields}
```

响应：
```json
{
  "work_branch": "main",
  "custom_fields": [
    {"key": "deploy_env", "value": "staging"}
  ]
}
```

场景不存在或未配置时返回：
```json
{"work_branch": "main", "custom_fields": []}
```

#### 3.2.2 PUT `/api/scenes/{scene_id}/advanced-options`

保存场景高级选项。

```
请求: PUT /api/scenes/{scene_id}/advanced-options?repo={owner}/{repo}
Body: {"work_branch": "develop", "custom_fields": [{"key": "env", "value": "prod"}]}
鉴权: get_current_user
```

```python
class AdvancedOptionsRequest(BaseModel):
    work_branch: str = "main"
    custom_fields: list = []   # [{key: str, value: str}]

@router.put("/{scene_id}/advanced-options")
def save_advanced_options(scene_id: str, body: AdvancedOptionsRequest,
                          user: dict = Depends(get_current_user), repo: str = ""):
    """保存场景高级选项"""
    # 1. 解析 repo_key
    # 2. 校验 work_branch 非空（强制字段）
    # 3. 校验场景存在（_query_scene_by_id）
    # 4. 序列化 JSON 写入 advanced_options 列
    # 5. 返回 {"message": "高级选项已保存"}
```

校验规则：
- `work_branch` 非空（空则 400）
- `custom_fields` 中每项的 `key` 和 `value` 非空
- `custom_fields` 中 `key` 不重复（重复则 400）

#### 3.2.3 GET `/api/scenes/branches/{owner}/{repo}`

扫描仓库分支列表供前端选择。

```
请求: GET /api/scenes/branches/{owner}/{repo}
鉴权: get_current_user
```

```python
@router.get("/branches/{owner}/{repo}")
def list_repo_branches(owner: str, repo: str, user: dict = Depends(get_current_user)):
    """列出仓库所有分支"""
    # 1. get_bot_token_by_repo(owner, repo) 拿 bot token
    # 2. 调 GitCode API: GET /api/v5/repos/{owner}/{repo}/branches?per_page=100
    # 3. 返回 [{name, protected, default}]
```

调用 GitCode API（已确认端点可用，参考 `src/user/repo_manager.py` 的 `_req` 封装）：
```
GET https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches?per_page=100&access_token={token}
```

响应：
```json
{
  "branches": [
    {"name": "main", "protected": true, "default": true},
    {"name": "develop", "protected": false, "default": false}
  ]
}
```

分页处理：per_page=100，若返回 100 条则继续翻页（最多 5 页 = 500 分支）。

#### 3.2.4 POST `/api/scenes/branches/{owner}/{repo}`

在仓库中创建新分支。

```
请求: POST /api/scenes/branches/{owner}/{repo}
Body: {"branch_name": "develop", "refs": "main"}
鉴权: get_current_user
```

```python
class CreateBranchRequest(BaseModel):
    branch_name: str
    refs: str = "main"   # 起始分支，默认 main

@router.post("/branches/{owner}/{repo}")
def create_repo_branch(owner: str, repo: str, body: CreateBranchRequest,
                        user: dict = Depends(get_current_user)):
    """在仓库中创建新分支"""
    # 1. get_bot_token_by_repo(owner, repo)
    # 2. 调 GitCode API: POST /api/v5/repos/{owner}/{repo}/branches
    #    body: {"branch_name": body.branch_name, "refs": body.refs}
    # 3. 返回 {"name": body.branch_name, "message": "分支已创建"}
```

调用 GitCode API：
```
POST https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches
Body: {"branch_name": "develop", "refs": "main"}
```

错误处理：
- 分支已存在（409）→ 返回 409 + 提示
- 权限不足（403）→ 返回 403 + 提示需维护者权限
- token 无效 → 返回 503

### 3.3 Prompt 注入

**文件**: `src/prompt_builder.py`

在 `build_devops_agent_prompt` 函数中（L430 `_build_project_info_section` 调用之后）新增注入：

```python
# 场景高级选项注入（仅执行类 agent）
if agent_type in ("coding", "qa", "sre"):
    _adv = _build_scene_advanced_options(repo_key, agent_type)
    if _adv:
        parts.append(_adv)
```

新增函数：

```python
def _build_scene_advanced_options(repo_key: str, agent_type: str) -> str:
    """注入场景高级选项：工作分支 + 自定义配置（仅执行类 agent）。"""
    try:
        from src.session_manager import db, db_lock
        # 1. 读 managed_repos.dev_scene 拿当前场景 scene_id
        with db_lock:
            c = db.cursor()
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        scene_id = row[0]
        # 2. 读 devops_pipeline_scene.advanced_options
        with db_lock:
            c = db.cursor()
            c.execute("SELECT advanced_options FROM devops_pipeline_scene "
                      "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                      (scene_id, repo_key))
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        import json as _json
        opts = _json.loads(row[0]) if row[0] else {}
        if not opts:
            return ""
        lines = ["\n## 场景高级选项"]
        wb = opts.get("work_branch", "")
        if wb:
            lines.append(f"- 工作基线分支：{wb}（Issue 分支从该分支切出）")
        fields = opts.get("custom_fields", [])
        if fields:
            lines.append("- 项目自定义配置：")
            for f in fields:
                _k = f.get("key", "")
                _v = f.get("value", "")
                if _k:
                    lines.append(f"  - {_k}: {_v}")
        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as _e:
        from src.config import log as _log
        _log.warning(f"prompt_builder: inject advanced options failed: {_e}")
        return ""
```

注入示例（agent 看到的内容）：
```
## 场景高级选项
- 工作基线分支：develop（Issue 分支从该分支切出）
- 项目自定义配置：
  - deploy_env: staging
  - test_command: npm test
```

### 3.4 repogit.py 分支基线变更

**文件**: `src/runtime/repogit.py`

修改 `_default_base_branch`（L106）：

当前实现：
```python
def _default_base_branch(base: str, token: str = "") -> str:
    """从 refs/remotes/origin/* 推断默认分支（main → master 兜底）。"""
    for cand in ("main", "master"):
        r = _run_git(["-C", base, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{cand}"], token=token)
        if r is not None and r.returncode == 0:
            return cand
    return "main"
```

修改后：
```python
def _default_base_branch(base: str, token: str = "", repo_key: str = "") -> str:
    """推断默认分支：优先读场景高级选项 work_branch，其次 main → master 兜底。"""
    # 1. 优先读场景配置的 work_branch
    if repo_key:
        _wb = _read_scene_work_branch(repo_key)
        if _wb:
            # 校验该分支在远程存在（避免配置错误导致 worktree 失败）
            r = _run_git(["-C", base, "show-ref", "--verify", "--quiet",
                          f"refs/remotes/origin/{_wb}"], token=token)
            if r is not None and r.returncode == 0:
                return _wb
            log.warning(f"repogit: scene work_branch '{_wb}' not found in remote, fallback to default")
    # 2. 原逻辑兜底
    for cand in ("main", "master"):
        r = _run_git(["-C", base, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{cand}"], token=token)
        if r is not None and r.returncode == 0:
            return cand
    return "main"
```

新增函数：
```python
def _read_scene_work_branch(repo_key: str) -> str:
    """读场景高级选项的 work_branch（managed_repos.dev_scene → devops_pipeline_scene.advanced_options）。"""
    try:
        from src.session_manager import db, db_lock
        import json as _json
        with db_lock:
            c = db.cursor()
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        scene_id = row[0]
        with db_lock:
            c = db.cursor()
            c.execute("SELECT advanced_options FROM devops_pipeline_scene "
                      "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                      (scene_id, repo_key))
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        opts = _json.loads(row[0]) if row[0] else {}
        return opts.get("work_branch", "") or ""
    except Exception:
        return ""
```

调用链修改（`ensure_issue_worktree` L169）：

```python
# 当前：
base_branch = _default_base_branch(base, token)
# 改为：
base_branch = _default_base_branch(base, token, repo_key)
```

`ensure_issue_worktree` 已有 `repo_key` 参数（L153），传递链完整。

### 3.5 Dashboard API 返回值

**文件**: `src/api/routes/dashboard.py`

在 dashboard 返回值（L1122）增加 `advanced_options` 字段：

```python
# 读取当前场景的高级选项
advanced_options = {}
try:
    if dev_scene:
        with db_lock:
            c.execute("SELECT advanced_options FROM devops_pipeline_scene "
                      "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                      (dev_scene, repo_key))
            row = c.fetchone()
            if row and row[0]:
                import json as _json
                advanced_options = _json.loads(row[0])
except Exception:
    pass

return {
    "repo": repo_key,
    "role": role,
    "dev_scene": dev_scene,
    "advanced_options": advanced_options,  # 新增
    "_scenes": scenes,
    "metrics": metrics,
    "agents": agent_sessions,
    "agent_engines": agent_engines,
    "scene_nodes": scene_nodes,
    "host_status": host_status,
}
```

### 3.6 前端 UI

**文件**: `src/api/static/js/pages/dashboard.js`

在场景选择器区域（`renderSceneSelector` 函数中），与「编辑场景」按钮同级新增「高级选项」按钮。

UI 结构：
```
┌─────────────────────────────────────┐
│  场景: [通用开发 ▾]  [编辑场景] [高级选项]  │
└─────────────────────────────────────┘
```

点击「高级选项」弹出 modal（符合主人偏好 popup/modal 风格）：

```
┌──────────────── 高级选项 ────────────────┐
│                                          │
│  工作分支:  [main          ▾]  [新建分支]  │
│                                          │
│  自定义配置:                               │
│  ┌────────────┬───────────────┬───┐     │
│  │ deploy_env │ staging       │ × │     │
│  │ test_cmd   │ npm test      │ × │     │
│  └────────────┴───────────────┴───┘     │
│  [+ 添加配置项]                          │
│                                          │
│              [取消]  [保存]               │
└──────────────────────────────────────────┘
```

交互流程：
1. 打开 modal → 调 `GET /api/scenes/{scene_id}/advanced-options` 加载已存配置
2. 工作分支下拉 → 调 `GET /api/scenes/branches/{owner}/{repo}` 填充选项
3. 点击「新建分支」→ 展开输入框（分支名 + 起始分支下拉）→ 调 `POST /api/scenes/branches/{owner}/{repo}` 创建 → 刷新分支列表 → 自动选中新分支
4. 自定义配置 → 动态增删行（key/value 输入框 + 删除按钮 + 添加按钮）
5. 保存 → 调 `PUT /api/scenes/{scene_id}/advanced-options` → 关闭 modal → toast 提示

## 四、改动影响

### 4.1 影响矩阵

| 文件 | 改动类型 | 新增行数 | 修改行数 | 影响范围 |
|------|---------|---------|---------|---------|
| `src/api/db_setup.py` | 新增迁移 | ~8 | 0 | DB schema，幂等 |
| `src/api/routes/scenes.py` | 新增 4 接口 + 1 Model | ~110 | 0 | 新路由，不影响现有 |
| `src/prompt_builder.py` | 新增函数 + 注入点 | ~35 | 2 | coding/qa/sre prompt |
| `src/runtime/repogit.py` | 修改函数 + 新增函数 | ~25 | 5 | worktree base 分支 |
| `src/api/routes/dashboard.py` | 返回值加字段 | ~12 | 1 | dashboard 响应 |
| `src/api/static/js/pages/dashboard.js` | 新增 modal + 按钮 | ~150 | 0 | 前端 UI |
| **合计** | | **~340** | **~8** | |

### 4.2 不受影响的部分

- 现有场景节点流水线配置（nodes_json）：高级选项独立存储，不碰 nodes
- 场景应用流程（apply_scene）：不修改，高级选项通过独立接口保存
- vod/issue agent prompt：不注入高级选项
- 已有 worktree：只在新建 worktree 时读 work_branch，已有 worktree 不受影响
- Webhook 事件处理流程：不涉及

### 4.3 风险分析

| 风险点 | 等级 | 说明 | 缓解措施 |
|--------|------|------|---------|
| repogit 分支基线变更 | 中 | `_default_base_branch` 改变 base 来源，影响所有新建 worktree | 1. 配置读取失败走原逻辑兜底；2. 校验 work_branch 在远程存在才使用；3. 不影响已有 worktree |
| GitCode API 分页 | 低 | 仓库分支 >100 时需翻页 | 最多 5 页（500 分支），覆盖绝大多数仓库 |
| GitCode API 权限 | 低 | bot token 需维护者权限才能创建分支 | 沿用 host_repo 的 bot token，已有维护者权限；403 时返回提示 |
| prompt 注入膨胀 | 低 | 自定义字段过多导致 prompt 变长 | 无数量限制但每项一行，实际影响极小 |
| 并发写入 | 低 | 同时保存高级选项和编辑场景 | DB 写操作有 db_lock，SQLite 串行化 |

## 五、设计测试用例

### 5.1 DB 迁移测试

| 用例 ID | 描述 | 前置条件 | 操作 | 预期结果 |
|---------|------|---------|------|---------|
| DB-01 | 新库迁移 | 全新 DB | 执行 db_setup | `advanced_options` 列存在，默认值 `'{}'` |
| DB-02 | 已有库幂等迁移 | 库已有 devops_pipeline_scene 数据，无 advanced_options 列 | 执行 db_setup | 列添加成功，已有行 `advanced_options='{}'` |
| DB-03 | 重复迁移 | 库已有 advanced_options 列 | 再次执行 db_setup | 不报错，不重复添加 |

### 5.2 API 接口测试

| 用例 ID | 描述 | 请求 | 预期结果 |
|---------|------|------|---------|
| API-01 | 读取空高级选项 | `GET /api/scenes/{sid}/advanced-options?repo=o/r`（场景未配置） | 200, `{"work_branch":"main","custom_fields":[]}` |
| API-02 | 读取已存高级选项 | 同上（场景已配置） | 200, 返回存储的 work_branch + custom_fields |
| API-03 | 读取不存在的场景 | `GET /api/scenes/nonexist/advanced-options` | 200, 返回默认值 |
| API-04 | 保存高级选项 | `PUT /api/scenes/{sid}/advanced-options` body=`{"work_branch":"develop","custom_fields":[{"key":"env","value":"prod"}]}` | 200, `"高级选项已保存"` |
| API-05 | 保存时 work_branch 为空 | body=`{"work_branch":"","custom_fields":[]}` | 400, `"工作分支不能为空"` |
| API-06 | 保存时 key 重复 | body=`{"work_branch":"main","custom_fields":[{"key":"a","value":"1"},{"key":"a","value":"2"}]}` | 400, `"配置项 key 不能重复"` |
| API-07 | 列出仓库分支 | `GET /api/scenes/branches/{owner}/{repo}` | 200, `{"branches":[{name,protected,default},...]}` |
| API-08 | 列出分支 - 仓库无权限 | bot token 无权限 | 200, `{"branches":[]}` 或 403 |
| API-09 | 创建新分支 | `POST /api/scenes/branches/{owner}/{repo}` body=`{"branch_name":"develop","refs":"main"}` | 200, `{"name":"develop","message":"分支已创建"}` |
| API-10 | 创建已存在分支 | 同上但分支名已存在 | 409, `"分支已存在"` |
| API-11 | 创建分支 - 无权限 | bot token 无写权限 | 403, `"需要维护者权限"` |

### 5.3 Prompt 注入测试

| 用例 ID | 描述 | 前置条件 | 预期结果 |
|---------|------|---------|---------|
| PT-01 | coding agent 注入 | 场景配置 work_branch=develop, custom_fields=[{env:staging}] | prompt 含「工作基线分支：develop」和「env: staging」 |
| PT-02 | qa agent 注入 | 同上 | 同上 |
| PT-03 | sre agent 注入 | 同上 | 同上 |
| PT-04 | vod agent 不注入 | 同上 | prompt 不含高级选项段落 |
| PT-05 | issue agent 不注入 | 同上 | prompt 不含高级选项段落 |
| PT-06 | 无配置不注入 | 场景未配置 advanced_options | prompt 不含高级选项段落 |
| PT-07 | 配置读取异常 | DB 异常 | prompt 不含高级选项段落，不崩溃（try/except 兜底） |
| PT-08 | 空自定义字段 | work_branch=main, custom_fields=[] | prompt 含工作分支行，不含自定义配置行 |

### 5.4 repogit 分支基线测试

| 用例 ID | 描述 | 前置条件 | 操作 | 预期结果 |
|---------|------|---------|------|---------|
| RG-01 | 有 work_branch 配置 | 场景 work_branch=develop，远程有 develop 分支 | 新建 Issue worktree | `feat/issue-{N}` 从 `origin/develop` 切出 |
| RG-02 | 无配置走原逻辑 | 场景未配置 advanced_options | 同上 | `feat/issue-{N}` 从 `origin/main`（或 master）切出 |
| RG-03 | work_branch 远程不存在 | 场景 work_branch=nonexist，远程无此分支 | 同上 | 走原逻辑兜底（main/master），日志 warning |
| RG-04 | DB 读取异常 | DB 连接异常 | 同上 | 走原逻辑兜底，不崩溃 |
| RG-05 | 已有 worktree 不受影响 | worktree 已存在 | 触发 ensure_issue_worktree | 直接复用已有 worktree，不重新切分支 |
| RG-06 | repo_key 传递完整 | 调用链 ensure_issue_worktree → _default_base_branch | 检查 repo_key 到达 | repo_key 非空传入 _default_base_branch |

### 5.5 前端 UI 测试

| 用例 ID | 描述 | 操作 | 预期结果 |
|---------|------|------|---------|
| UI-01 | 按钮显示 | 进入 Dashboard | 场景选择器旁有「高级选项」按钮 |
| UI-02 | 打开 modal | 点击「高级选项」 | modal 弹出，含工作分支下拉 + 自定义配置区 + 保存/取消 |
| UI-03 | 加载已存配置 | 场景已配置高级选项 | modal 打开后工作分支选中已存值，自定义配置行已填充 |
| UI-04 | 分支列表加载 | 点击工作分支下拉 | 下拉项为仓库现有分支列表 |
| UI-05 | 新建分支 | 点击「新建分支」→ 输入分支名 → 确认 | 分支列表刷新包含新分支，自动选中 |
| UI-06 | 新建分支 - 已存在 | 输入已存在分支名 | toast 提示「分支已存在」 |
| UI-07 | 添加自定义配置 | 点击「+ 添加配置项」 | 新增一行 key/value 输入框 |
| UI-08 | 删除自定义配置 | 点击某行「×」 | 该行删除 |
| UI-09 | 保存配置 | 点击「保存」 | modal 关闭，toast「高级选项已保存」 |
| UI-10 | 保存校验 - work_branch 空 | 清空工作分支 → 保存 | toast 提示「工作分支不能为空」 |
| UI-11 | 取消不保存 | 修改后点击「取消」 | modal 关闭，不保存修改 |

### 5.6 集成测试

| 用例 ID | 描述 | 流程 | 预期结果 |
|---------|------|------|---------|
| IT-01 | 端到端完整流程 | 配置 work_branch=develop → 创建 Issue → coding agent 执行 | worktree 从 develop 切出；prompt 含「工作基线分支：develop」 |
| IT-02 | 切换场景不影响高级选项 | 场景 A 配置 work_branch=dev → 切换到场景 B | 场景 B 的 work_branch 独立（A 的配置不跟随） |
| IT-03 | 历史场景兼容 | 已有场景无 advanced_options → 新建 Issue | worktree 走原逻辑（main/master），prompt 无高级选项段 |
| IT-04 | 新建分支并选用 | 在 modal 新建 develop 分支 → 保存为 work_branch → 创建 Issue | worktree 从新建的 develop 分支切出 |

## 六、实施顺序

1. DB 迁移（`db_setup.py`）
2. 后端 API（`scenes.py` 4 个接口）
3. Dashboard 返回值（`dashboard.py`）
4. Prompt 注入（`prompt_builder.py`）
5. repogit 分支基线（`repogit.py`）
6. 前端 UI（`dashboard.js`）
7. 测试验证

## 七、验收标准

- [ ] DB 迁移幂等，已有数据不受影响
- [ ] 4 个 API 接口功能正确，错误处理完备
- [ ] Dashboard 返回 advanced_options 字段
- [ ] coding/qa/sre agent prompt 包含高级选项段落
- [ ] vod/issue agent prompt 不包含高级选项段落
- [ ] worktree 从配置的 work_branch 切出
- [ ] work_branch 远程不存在时走原逻辑兜底
- [ ] 前端 modal 正确加载/保存配置
- [ ] 分支列表正确加载，新建分支功能正常
- [ ] 历史场景无配置时行为不变
