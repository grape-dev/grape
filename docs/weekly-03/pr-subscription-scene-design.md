# PR 订阅场景功能 — 设计方案

## 一、需求总结

在仓库场景体系中新增「PR 订阅场景」类型。当 PR 与任意 Issue 无关联时，触发此场景的 QA（和可选 SRE）Agent 工作。审查结果回帖到 PR 评论。

核心约束：
- 一个仓库最多一个 PR 订阅场景
- 只有 QA 和 SRE 两个 Agent，QA 必选，SRE 可选，其余 Agent 不显示
- 不含 VOD/Issue Agent，不走 Issue 生命周期
- 不出现在兜底场景下拉、场景市场、Dashboard 首个选中
- 无高级选项（工作分支无意义）
- 触发条件：PR 有新 commit + 无关联 Issue

---

## 二、数据层改动

### 2.1 DB Schema：devops_pipeline_scene 加 scene_type 列

文件：`src/api/db_setup.py`

```sql
ALTER TABLE devops_pipeline_scene ADD COLUMN scene_type TEXT DEFAULT 'standard'
```

用 `_ensure_column()` 幂等添加（参考第 2865 行 `advanced_options` 的添加方式）。
默认值 `'standard'` 保证旧数据兼容，不需要数据迁移。

### 2.2 create_custom_scene / update_custom_scene 适配

文件：`src/api/pipeline_config.py`

- `create_custom_scene()` 第 301 行：加 `scene_type: str = "standard"` 参数，INSERT 语句加 `scene_type` 列
- `update_custom_scene()` 第 365 行：不接受 `scene_type` 更新（创建后不可变更）
- `_query_scenes_from_db()` 第 94 行：SELECT 加 `scene_type` 列，返回值加 `scene_type` 字段
- `_query_scene_by_id()` 第 165 行：同上
- `get_scenes()` 第 198 行：返回值加 `scene_type`

### 2.3 影响评估

| 影响面 | 评估 |
|--------|------|
| 旧场景数据 | 默认值 `standard` 兼容，无影响 |
| 现有查询 | SELECT 加列是纯增量，不改变现有返回结构 |
| pipeline_config 返回 | 新增 `scene_type` 字段，前端旧版本忽略此字段不影响 |
| 场景同步（scene_sync.py） | 官方场景不存于 devops_pipeline_scene，无影响 |

---

## 三、API 层改动

### 3.1 CreateSceneRequest 加 scene_type

文件：`src/api/routes/scenes.py` 第 1376 行

```python
class CreateSceneRequest(BaseModel):
    scene_id: str = ""
    name: str
    description: str = ""
    scope: str = "repo"
    nodes: list[SceneNodeInput] = []
    icon: str = ""
    scene_type: str = "standard"  # 新增
```

### 3.2 create_scene() 校验逻辑

文件：`src/api/routes/scenes.py` 第 1451 行

在现有校验之后增加：

```python
if body.scene_type == "pr_subscription":
    # 1. 唯一性校验：同仓库最多一个 PR 订阅场景
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id FROM devops_pipeline_scene "
            "WHERE repo_key=? AND scene_type='pr_subscription' AND is_active=1 LIMIT 1",
            (repo_key,))
        if c.fetchone():
            raise HTTPException(status_code=409, detail={
                "code": 409, "message": "该仓库已存在 PR 订阅场景，每个仓库最多一个"})

    # 2. nodes 校验：只允许 qa 和 sre，QA 必须启用且有 skill
    allowed_tags = {"qa", "sre"}
    node_tags = {n.tag for n in body.nodes}
    invalid = node_tags - allowed_tags
    if invalid:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": f"PR 订阅场景只允许 QA 和 SRE Agent，不允许: {','.join(sorted(invalid))}"})
    qa_node = next((n for n in body.nodes if n.tag == "qa"), None)
    if not qa_node or not qa_node.enabled or not qa_node.skill:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "PR 订阅场景必须包含 QA Agent 且已配置技能"})
```

### 3.3 update_scene() 校验逻辑

文件：`src/api/routes/scenes.py` 第 1496 行

- 查询现有场景的 `scene_type`，如果是 `pr_subscription`，同样校验 nodes 只含 qa/sre 且 QA 必选
- 不允许通过 update 修改 `scene_type`

### 3.4 list_scenes_with_pipeline / get_scene_detail 返回 scene_type

文件：`src/api/routes/scenes.py`

- `list_scenes_with_pipeline()` 第 104 行：result.append 加 `"scene_type": s.get("scene_type", "standard")`
- `get_scene_detail()` 第 1309 行：返回加 `"scene_type"`

### 3.5 apply_scene 拒绝 PR 订阅场景

文件：`src/api/routes/scenes.py` 第 1555 行

```python
def apply_scene(owner, repo, body, user):
    # ... 现有逻辑 ...
    # 查场景 scene_type，PR 订阅场景不允许 apply
    from src.api.pipeline_config import _query_scene_by_id
    existing = _query_scene_by_id(body.scene_id, repo_key=repo_key)
    if existing and existing.get("scene_type") == "pr_subscription":
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "PR 订阅场景不能设为仓库默认场景"})
```

### 3.6 影响评估

| 影响面 | 评估 |
|--------|------|
| 标准场景创建 | `scene_type` 默认 `standard`，走原有逻辑，无影响 |
| 标准场景更新 | 不含 `scene_type` 字段时默认 `standard`，无影响 |
| apply_scene | 增加前置检查，标准场景不受影响 |
| 市场场景应用 | `apply_market_scene()` 调 `create_custom_scene`，`scene_type` 默认 `standard`，无影响 |
| 唯一性校验 | 业务层查询，非 DB UNIQUE，并发竞态概率极低（同一仓库两人同时创建 PR 订阅场景） |

---

## 四、前端改动

### 4.1 创建场景弹窗

文件：`src/api/static/js/pages/dashboard.js`

当前 `enterCreateMode()`（第 1304 行）直接进入编辑界面。改为先弹出类型选择弹窗。

弹窗逻辑：
```
点击「添加场景」→ 弹出选择弹窗
  ├─ 选「标准场景」→ 调用 enterCreateMode()（当前逻辑，5 个 Agent）
  └─ 选「PR 订阅场景」→ 调用 enterCreatePRSubscriptionMode()（新函数）
```

`enterCreatePRSubscriptionMode()` 与 `enterCreateMode()` 结构一致，区别：
- `allAgentTypes = ['qa', 'sre']`（第 1314 行改为 2 个）
- `canToggle = (tag) => tag !== 'qa'`（第 1329 行，QA 不可禁用）
- 不渲染高级选项区域（第 1381-1403 行的 `advOptionsInline` 不输出）
- `_saveCreateScene` 传 `scene_type: 'pr_subscription'`（第 1498 行 API.post body 加字段）

### 4.2 场景列表特殊标志

文件：`src/api/static/js/pages/dashboard.js` 第 341 行 `_renderLocalSceneCard()`

在 `scn-row-right` 区域，PR 订阅场景加一个药丸标签：
```javascript
var isPRSub = s.scene_type === 'pr_subscription';
var prBadge = isPRSub ? '<span style="font-size:10px;font-weight:500;padding:3px 10px;border-radius:999px;background:rgba(175,82,222,.1);color:#af52de;border:1px solid rgba(175,82,222,.2)">PR 订阅</span>' : '';
```

### 4.3 详情页适配

文件：`src/api/static/js/pages/dashboard.js`

`_renderLocalSceneDetailPage()`（第 548 行）和 `_renderSceneEditMode()`（第 889 行）：

```javascript
var isPRSub = (scene.scene_type === 'pr_subscription');
var agentOrder = isPRSub ? ['qa', 'sre'] : ['vod', 'issue', 'coding', 'qa', 'sre'];
var agentLabels = isPRSub ? ['QA Agent', 'SRE Agent'] : ['VOD Agent', 'Issue Agent', 'Coding Agent', 'QA Agent', 'SRE Agent'];
var canToggle = isPRSub
    ? (tag) => tag !== 'qa'
    : (tag) => tag !== 'vod' && tag !== 'issue';
```

需修改的硬编码位置（共 5 处）：
1. 第 899 行 `_renderSceneEditMode`：`allAgentTypes`
2. 第 1036 行 `renderAgentCards`：`canToggle`
3. 第 1314 行 `enterCreateMode`：`allAgentTypes`
4. 第 1329 行 `enterCreateMode`：`canToggle`
5. 第 1577 行 `renderAgentCards`：`canToggle`

每处增加 `isPRSub` 判断分支。

高级选项区域（第 1381-1403 行）：PR 订阅场景不渲染。

### 4.4 兜底场景下拉排除

文件：`src/api/static/js/pages/dashboard.js` 第 1721-1726 行

```javascript
sceneOptions = scenes.filter(function(s) {
    return s.scene_type !== 'pr_subscription';
}).map(function(s) {
    var disp = s.display_name || s.name || s.id;
    return '<option value="' + s.id + '">' + disp + '</option>';
}).join('');
```

### 4.5 影响评估

| 影响面 | 评估 |
|--------|------|
| 标准场景创建流程 | 多一步弹窗选择，选「标准场景」后走原逻辑 |
| 标准场景详情页 | `isPRSub=false` 走原逻辑，无影响 |
| 标准场景列表 | 多一个 `prBadge` 判断，标准场景不输出 badge |
| 兜底场景下拉 | 过滤掉 PR 订阅场景，标准场景不受影响 |
| 硬编码修改 | 5 处 `allAgentTypes`/`canToggle` 加分支，模式一致 |

---

## 五、事件管道改动（核心）

### 5.1 PR 无关联 Issue 时触发 PR 订阅场景

文件：`src/api/event_pipeline.py` 第 936 行附近

当前逻辑（第 920-950 行）：
```python
if event_type in PR_EVENT_TYPES:
    _default = PR_DEFAULT_AGENT  # = ""
elif event_type in ISSUE_EVENT_TYPES:
    _default = "issue"
else:
    _default = NON_ISSUE_EVENT_ROUTE.get(event_type, "")
agent_type = _resolve_issue_stage_agent(repo_key, event_type, evt.get("payload", {}) or {}, _default)
# agent_type 为空 → skip
```

改动后：
```python
# ... 现有路由逻辑 ...
if not agent_type and event_type in PR_EVENT_TYPES:
    # PR 无关联 Issue → 尝试 PR 订阅场景
    agent_type = _try_pr_subscription_scene(repo_key, evt, event_type)
    if agent_type:
        log.info(f"Pipeline: PR subscription scene triggered -> agent_type='{agent_type}'")

if not agent_type:
    # 维持原 skip 逻辑
    ...
```

新增函数 `_try_pr_subscription_scene()`：
```python
def _try_pr_subscription_scene(repo_key: str, evt: dict, event_type: str) -> str:
    """PR 无关联 Issue 时，查仓库 PR 订阅场景，从场景 nodes 解析 QA skill。
    返回 agent_type（"qa"）或空字符串。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT scene_id, nodes_json FROM devops_pipeline_scene "
                "WHERE repo_key=? AND scene_type='pr_subscription' AND is_active=1 LIMIT 1",
                (repo_key,))
            row = c.fetchone()
        if not row:
            return ""
        scene_id, nodes_json = row[0], row[1]
        nodes = json.loads(nodes_json or "[]")
        qa_node = next((n for n in nodes if n.get("tag") == "qa" and n.get("enabled", True)), None)
        if not qa_node or not qa_node.get("skill"):
            return ""
        # 直接设置 skill（不走 agent_session 查询）
        # 返回特殊标记，让调用方知道走 PR 订阅路径
        evt["_pr_sub_scene_id"] = scene_id
        evt["_pr_sub_skill"] = qa_node["skill"]
        evt["_pr_sub_engine_ids"] = qa_node.get("engine_runtime_ids", [])
        # SRE node（可选）
        sre_node = next((n for n in nodes if n.get("tag") == "sre" and n.get("enabled", True)), None)
        if sre_node and sre_node.get("skill"):
            evt["_pr_sub_sre_skill"] = sre_node["skill"]
            evt["_pr_sub_sre_engine_ids"] = sre_node.get("engine_runtime_ids", [])
        return "qa"
    except Exception as e:
        log.warning(f"Pipeline: PR subscription scene lookup failed: {e}")
        return ""
```

### 5.2 Skill 解析：跳过 agent_session 查询

文件：`src/api/event_pipeline.py` 第 1040-1066 行

当前第 1045 行查 `devops_agent_session` 获取 skill。PR 订阅场景不走此路径。

在 skill 解析之前增加判断：
```python
if evt.get("_pr_sub_scene_id"):
    # PR 订阅场景：直接从场景 nodes 取 skill，不走 agent_session
    skill_name = evt["_pr_sub_skill"]
    skill_id = 0
    skill_path = skill_name
    # 解析 skill_id
    try:
        from src.user.db import resolve_skill_id, get_skill_by_id
        _sid = resolve_skill_id(skill_name, repo_key=repo_key)
        if _sid:
            skill_id = _sid
            _sk = get_skill_by_id(_sid)
            if _sk:
                skill_path = _sk.get("skill_path") or skill_name
    except Exception:
        pass
    log.info(f"Pipeline (PR-sub): skill from scene nodes: {skill_name} sid={skill_id}")
```

这段代码插入到第 1097 行 `if _is_scene_recommend:` 之前，PR 订阅场景走完后跳过后续的 `is_plugin_mode()` skill 解析。

### 5.3 issue_id 处理：用伪 issue_id 参与去重

文件：`src/api/event_pipeline.py` 第 1156-1175 行

PR 订阅场景的 issue_id 当前为空/`#0`。需要改为伪 issue_id 以参与去重：

```python
# 在 PR issue_id 解析之后（第 1167-1169 行）：
if not issue_id and evt.get("_pr_sub_scene_id"):
    # PR 订阅场景：用 PR 号构造伪 issue_id 参与去重
    pr_obj = payload.get("pull_request", {}) or payload.get("merge_request", {}) or {}
    obj_attrs = payload.get("object_attributes", {}) or {}
    pr_num = pr_obj.get("number", pr_obj.get("iid", "")) or obj_attrs.get("iid", "")
    if pr_num:
        issue_id = f"#pr_{pr_num}"
        evt["_pr_sub_pr_number"] = pr_num
        log.info(f"Pipeline: PR subscription pseudo issue_id={issue_id}")
```

这样去重检查（第 1182 行 `if issue_id and agent_type`）就能生效：同一 PR 多次 push 时，`#pr_{pr_num}` 相同 + agent_type 相同 → 去重。

### 5.4 pick_engine：传入 PR 订阅场景的 scene_id

文件：`src/api/event_pipeline.py` 第 1272 行

```python
# PR 订阅场景用自己的 scene_id
_issue_scene = evt.get("_pr_sub_scene_id") or _issue_scene
```

`pick_engine()` 在 plugin 模式下统一返回 0（广播），`get_scene_pool_engine_ids()` 会从场景 node 的 `engine_runtime_ids` 限定广播范围。PR 订阅场景的 nodes 已含 `engine_runtime_ids`，所以引擎选择正常工作。

### 5.5 Prompt 构建

文件：`src/api/event_pipeline.py` + `src/prompt_builder.py`

`build_devops_agent_prompt()` 第 577 行在 issue_id 为空/`#pr_N` 时的行为：
- `extract_event_number()` 第 390 行：从 `pull_request`/`merge_request`/`object_attributes` 提取 number → 对 PR 事件能提取到 PR 号
- 第 635-642 行：`else` 分支输出 `Event: {evt_type} #{number} on {repo}` + title + body → PR 事件有 title/body
- 第 645 行 `_inject_attachments_by_role`：issue_id 为 `#pr_N`，`_issue_num_from("#pr_N")` 返回空（非数字）→ 不注入附件，安全
- 第 663-676 行：`_devops_issue_id` = `#pr_N`，`get_issue_scene()` 查不到 → `_devops_scene_id` 为空 → `_build_project_info_section` 正常输出（不依赖 issue_id）
- 第 684-688 行：`_build_scene_advanced_options` 读 `issue_state.scene_id` → 查不到 → 读 `managed_repos.dev_scene` → 为空 → 返回空 → PR 订阅场景无高级选项，符合预期
- 第 714-723 行：skill 注入正常（skill_name 已设置）

**结论：`build_devops_agent_prompt()` 在 PR 订阅场景下基本可用。** Prompt 会包含 PR title/body + 项目知识 + 技能指引 + A2A 协议。唯一缺失的是 Issue 上下文，但主人确认不需要。

需要在 prompt 中增加 PR 审查上下文提示（告知 QA 这是 PR 审查而非 Issue 任务）：
```python
# 在 build_devops_agent_prompt 中，PR 订阅场景增加提示
if evt.get("_pr_sub_scene_id"):
    parts.append(f"\n## PR 审查任务\n本任务来自 PR 订阅场景，PR 无关联 Issue。请审查 PR #{number} 的变更内容，结果回帖到 PR 评论。")
```

### 5.6 审查结果回帖到 PR

文件：`src/api/event_pipeline.py` 第 2432-2436 行

当前回帖逻辑：
```python
if new_status == "completed" and result_text and issue_id:
    _issue_num = _issue_num_from(issue_id)  # "#pr_10" → "" (非数字)
    if _issue_num and repo_gitcode_token:
        _post_result_to_issue(repository, _issue_num, result_text, repo_gitcode_token, agent_type, repo_key)
```

`_issue_num_from("#pr_10")` 返回空 → 当前不会回帖。需要增加 PR 回帖路径：

```python
# PR 订阅场景回帖到 PR 评论
if new_status == "completed" and result_text and evt.get("_pr_sub_pr_number"):
    _pr_num = str(evt["_pr_sub_pr_number"])
    if _pr_num and repo_gitcode_token:
        _post_result_to_pr(repository, _pr_num, result_text, repo_gitcode_token, agent_type, repo_key)
```

新增函数 `_post_result_to_pr()`：
```python
def _post_result_to_pr(repository: str, pr_number: str, result_text: str, bot_token: str,
                        agent_type: str, repo_key: str = ""):
    """Agent 执行完成后，将结果作为评论发回 PR。"""
    # GitCode: /repos/{owner}/{repo}/pulls/{pr_number}/comments
    # GitHub:  /repos/{owner}/{repo}/issues/{pr_number}/comments (GitHub PR 共用 Issue 评论 API)
    # 逻辑与 _post_result_to_issue 类似，先查已有评论去重，再补发
```

关键点：
- GitCode PR 评论 API：`POST /repos/{owner}/{repo}/pulls/{pr_number}/comments`（与 Issue 评论 API 不同）
- GitHub PR 评论 API：`POST /repos/{owner}/{repo}/issues/{pr_number}/comments`（与 Issue 共用）
- VcsApiClient（`vcs/api_client.py`）当前只有 `post_issue_comment`，没有 `post_pr_comment`。需要新增或直接用 urllib。

### 5.7 Session 处理

文件：`src/api/event_pipeline.py` 第 2088-2103 行

PR 订阅场景 issue_id 为 `#pr_N`，`_issue_num_from("#pr_N")` 返回空 → `_has_issue = False` → 不创建 Issue session → 走 repo 级 session（第 2101 行注释：无关联 Issue 任务不执行）。

**问题**：第 2101 行注释说「无关联 Issue 任务不执行」，意味着 PR 订阅场景的 direct 执行不会创建 session。

**解决方案**：PR 订阅场景走 plugin 模式（`pick_engine` 返回 0 → 广播 → `queue_remote`），不走 direct 执行。这与当前大多数场景的行为一致（plugin 模式是默认模式）。

如果需要 direct 执行支持，需要修改 session 逻辑让 `#pr_N` 也创建 session。但建议先只支持 plugin 模式。

### 5.8 影响评估

| 影响面 | 评估 |
|--------|------|
| 正常 PR + 有关联 Issue | `_resolve_issue_stage_agent` 返回非空 → 不进入 PR 订阅逻辑 → 无影响 |
| 正常 PR + 无关联 Issue + 无 PR 订阅场景 | `_try_pr_subscription_scene` 查不到 → 返回空 → 维持 skip → 无影响 |
| Issue 事件 | `event_type not in PR_EVENT_TYPES` → 不进入 PR 订阅逻辑 → 无影响 |
| 非 PR/Issue 事件 | 同上 → 无影响 |
| 去重 | 伪 issue_id `#pr_N` 参与去重，不与真实 Issue 冲突（`#pr_` 前缀区分） |
| pick_engine | PR 订阅场景 scene_id 传入，plugin 模式返回 0（广播），正常工作 |
| Prompt | `build_devops_agent_prompt` 在无 Issue 上下文时基本可用，增加 PR 审查提示 |
| 回帖 | 新增 `_post_result_to_pr`，不影响现有 `_post_result_to_issue` |
| Session | PR 订阅场景走 plugin 模式（广播），不依赖 Issue session |

---

## 六、Dashboard 改动

### 6.1 首个场景选中排除 PR 订阅场景

文件：`src/api/routes/dashboard.py` 第 1054-1056 行

```python
# 跳过 PR 订阅场景，选第一个标准场景
standard_scenes = [s for s in scenes if s.get("scene_type") != "pr_subscription"]
if standard_scenes:
    if not dev_scene or dev_scene not in {s["id"] for s in standard_scenes}:
        dev_scene = standard_scenes[0]["id"]
```

### 6.2 影响评估

| 影响面 | 评估 |
|--------|------|
| 标准场景列表 | PR 订阅场景仍在列表中显示（带标志），只是不作为默认选中 |
| Dashboard 数据 | `get_scenes()` 返回所有场景（含 PR 订阅），前端按 scene_type 区分渲染 |

---

## 七、测试用例设计

### 7.1 数据层测试

文件：`tests/test_pr_subscription_scene.py`（新建）

```
TC-DB-01: scene_type 列存在且默认值为 standard
  → PRAGMA table_info(devops_pipeline_scene) 包含 scene_type
  → 旧场景记录 scene_type = 'standard'

TC-DB-02: create_custom_scene 持久化 scene_type
  → create_custom_scene(scene_type='pr_subscription') → 查 DB 确认 scene_type 字段

TC-DB-03: get_scenes 返回 scene_type
  → 创建场景 → get_scenes() 结果包含 scene_type 字段
```

### 7.2 API 层测试

```
TC-API-01: 创建 PR 订阅场景成功
  → POST /scenes scene_type='pr_subscription' nodes=[{tag:'qa',skill:'qa-skill'}]
  → 200, 返回 scene_id

TC-API-02: 创建 PR 订阅场景时 nodes 含 vod → 400
  → POST /scenes scene_type='pr_subscription' nodes=[{tag:'vod',skill:'vod-skill'}]
  → 400, message 包含"只允许 QA 和 SRE"

TC-API-03: 创建 PR 订阅场景时无 QA → 400
  → POST /scenes scene_type='pr_subscription' nodes=[{tag:'sre',skill:'sre-skill'}]
  → 400, message 包含"必须包含 QA"

TC-API-04: 创建 PR 订阅场景时 QA 无 skill → 400
  → POST /scenes scene_type='pr_subscription' nodes=[{tag:'qa',skill:''}]
  → 400, message 包含"必须包含 QA"

TC-API-05: 同仓库创建第二个 PR 订阅场景 → 409
  → 先创建一个 → 再创建一个 → 409, message 包含"已存在"

TC-API-06: 不同仓库各创建 PR 订阅场景 → 都成功
  → repo A 创建 → repo B 创建 → 都 200

TC-API-07: 标准场景创建不受影响
  → POST /scenes scene_type='standard'（或不传）→ 200, 走原逻辑

TC-API-08: update_scene 不修改 scene_type
  → 创建标准场景 → PUT 尝试改 scene_type → 场景 type 不变

TC-API-09: apply_scene 拒绝 PR 订阅场景
  → 创建 PR 订阅场景 → POST /scenes/apply → 400

TC-API-10: list_scenes_with_pipeline 返回 scene_type
  → 创建标准 + PR 订阅场景 → GET /scenes/with-pipeline → 两个都有 scene_type
```

### 7.3 事件管道测试

```
TC-PIPE-01: PR 无关联 Issue + 有 PR 订阅场景 → 触发 QA
  → 仓库有 PR 订阅场景 → PR opened（title 无 #N，分支无 issue-N）
  → run_pipeline → agent_type='qa', skill 来自场景 nodes

TC-PIPE-02: PR 无关联 Issue + 无 PR 订阅场景 → 维持 skip
  → 仓库无 PR 订阅场景 → PR opened（无关联 Issue）
  → run_pipeline → agent_type='', event_skipped

TC-PIPE-03: PR 有关联 Issue → 走标准 Issue 流水线，不触发 PR 订阅
  → 仓库有 PR 订阅场景 → PR title 含 #5 → Issue #5 stage=coding
  → run_pipeline → agent_type='coding'（走阶段矩阵），不进入 PR 订阅逻辑

TC-PIPE-04: 同一 PR 多次 push → 去重生效
  → PR opened 无关联 Issue → QA 触发（running）
  → PR updated 有新 commit → 去重检查命中 → skip

TC-PIPE-05: PR 无关联 Issue + PR 订阅场景无 QA skill → skip
  → PR 订阅场景 QA node skill 为空 → _try_pr_subscription_scene 返回空 → skip

TC-PIPE-06: PR 订阅场景的 pick_engine 正常
  → PR 订阅场景 QA node 有 engine_runtime_ids
  → pick_engine 返回 0（plugin 模式广播）
  → get_scene_pool_engine_ids 返回场景配置的引擎列表

TC-PIPE-07: Issue 事件不受 PR 订阅场景影响
  → 仓库有 PR 订阅场景 → issue_opened 事件
  → run_pipeline → agent_type='vod'，不进入 PR 订阅逻辑

TC-PIPE-08: PR 订阅场景 prompt 包含 PR 审查提示
  → PR 订阅场景触发 → build_devops_agent_prompt
  → prompt 包含"PR 审查任务" + PR title/body + 项目知识 + 技能指引

TC-PIPE-09: PR 订阅场景 QA 完成后回帖到 PR
  → QA 执行完成 → _post_result_to_pr 被调用
  → GitCode/GitHub PR 评论 API 被调用

TC-PIPE-10: PR 订阅场景不含高级选项注入
  → _build_scene_advanced_options 对 PR 订阅场景返回空
  → prompt 不含工作分支/自定义配置指引
```

### 7.4 前端测试

```
TC-FE-01: 点击「添加场景」→ 弹出类型选择弹窗
  → 弹窗包含「标准场景」和「PR 订阅场景」两个选项

TC-FE-02: 选「标准场景」→ 进入原有创建界面
  → 5 个 Agent，vod/issue 不可禁用，有高级选项

TC-FE-03: 选「PR 订阅场景」→ 进入 PR 订阅创建界面
  → 只有 QA 和 SRE 两个 Agent
  → QA 不可禁用，SRE 可禁用
  → 无高级选项区域

TC-FE-04: 场景列表中 PR 订阅场景有特殊标志
  → PR 订阅场景行显示"PR 订阅"药丸标签
  → 标准场景不显示

TC-FE-05: PR 订阅场景详情页只显示 QA + SRE
  → 不显示 VOD/Issue/Coding Agent 卡片
  → 无高级选项区域

TC-FE-06: 兜底场景下拉不包含 PR 订阅场景
  → 打开智能推荐规则弹窗 → 兜底场景下拉只显示标准场景

TC-FE-07: Dashboard 首个选中不是 PR 订阅场景
  → 仓库有标准场景 + PR 订阅场景 → Dashboard 默认选中第一个标准场景
```

### 7.5 回归测试（确保现有功能不受影响）

```
TC-REG-01: 标准场景完整生命周期
  → 创建 → 编辑 → 应用 → 删除 → 全程无异常

TC-REG-02: 标准 Issue 流水线
  → Issue opened → VOD → Issue → Coding → QA → SRE → 全程不受 PR 订阅场景影响

TC-REG-03: PR 有关联 Issue 的流水线
  → PR opened + title 含 #N → 走阶段矩阵路由 → 不触发 PR 订阅

TC-REG-04: 场景市场应用
  → 从市场应用标准场景 → 正常创建，scene_type='standard'

TC-REG-05: 智能推荐
  → Issue opened 无场景 → 走推荐 → 兜底场景是标准场景 → 正常

TC-REG-06: 场景删除后清理
  → 删除 PR 订阅场景 → 可重新创建（软删除/硬删除后无残留）
```

---

## 八、实施顺序

```
Phase 1: DB + API 基础（改动点 2.1 / 2.2 / 3.1-3.4）
  → scene_type 列 + create/update/list/detail 支持
  → 测试：TC-DB-01~03, TC-API-01, 07, 08, 10
  → 可独立部署，不影响现有功能

Phase 2: API 校验 + apply_scene 排除（改动点 3.2 / 3.3 / 3.5）
  → 唯一约束 + nodes 校验 + apply 拒绝
  → 测试：TC-API-02~06, 09

Phase 3: 前端适配（改动点 4.1-4.5, 6.1）
  → 创建弹窗 + 列表标志 + 详情页 + 兜底排除 + Dashboard
  → 测试：TC-FE-01~07, TC-REG-01
  → 此时 PR 订阅场景可创建但不会触发

Phase 4: 事件管道（改动点 5.1-5.8）
  → PR 无关联 Issue 触发 + skill 解析 + 去重 + prompt + 回帖
  → 测试：TC-PIPE-01~10, TC-REG-02~03
  → 建议加 feature flag：env GRAPE_PR_SUBSCRIPTION_ENABLED=true 才启用
  → 全量回归：TC-REG-04~06
```

---

## 九、Feature Flag

事件管道改动加环境变量控制，避免上线即生效：

```python
# event_pipeline.py
import os
_PR_SUB_ENABLED = os.environ.get("GRAPE_PR_SUBSCRIPTION_ENABLED", "false").lower() == "true"

# _try_pr_subscription_scene 调用前检查
if not agent_type and event_type in PR_EVENT_TYPES and _PR_SUB_ENABLED:
    agent_type = _try_pr_subscription_scene(repo_key, evt, event_type)
```

Phase 1-3 不需要 feature flag（只影响创建/展示，不影响事件路由）。
Phase 4 上线时先 false → 验证 → true。
