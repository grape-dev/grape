# VOD 自动场景识别方案

> 状态：草案 | 日期：2026-08-27 | 分支：wise

## 一、背景

**现状**：开发场景（`managed_repos.dev_scene`）是仓库级静态配置，用户在 Dashboard 手动选择。任务执行时直接读 `get_repo_scene()` → 从 `pipelines.json` 取对应 scene 的 skill 链。

**问题**：同一个仓库可能产生不同类型的需求。例如 `songspace/grape` 仓库既改后端 Python（default 场景），又可能新增一个 skill（skill-dev 场景），还可能做 IaC 部署脚本（iac 场景）。静态场景无法按需求动态匹配流水线。

**目标**：VOD 在审视/澄清需求后，结合项目知识基线（project-graph + project-capabilities）和成员上传文档，自动判定当前 Issue 该走哪个开发场景，后续节点（issue/coding/qa/sre）按该场景的流水线 skill 执行。

## 二、设计原则

1. **Issue 级场景，非仓库级**：场景跟随 Issue 走，不是仓库一个值管所有 Issue。仓库级 `dev_scene` 保留为默认场景（向后兼容）。
2. **VOD 判定，服务端落地**：VOD Agent 在 prompt 中拿到项目知识基线 + 成员文档 + 可用场景列表，在结构化产出中给出 `recommended_scene`。服务端收到后写入 `issue_state.scene_id`。
3. **不破坏现有流程**：无 `scene_id` 时回退到 `managed_repos.dev_scene` → `default`。
4. **可人工覆盖**：用户可在 Dashboard 手动指定 Issue 的场景（覆盖 VOD 推荐）。

## 三、数据结构变更

### 3.1 issue_state 表加列

```sql
ALTER TABLE issue_state ADD COLUMN scene_id TEXT DEFAULT '';
```

- 空值 = 未判定，回退到仓库默认场景
- 非空 = VOD 判定的场景 ID（如 `default`/`iac`/`skill-dev`/`blog-publish` 等）

### 3.2 新增 `get_issue_scene()` 函数

`src/api/session_store.py`：

```python
def get_issue_scene(repo_key: str, issue_id: str) -> str:
    """读取 Issue 级场景，空则回退到仓库默认场景。"""
    st = get_issue_stage(repo_key, issue_id)
    if st and st.get("scene_id"):
        return st["scene_id"]
    return get_repo_scene(repo_key) or "default"

def set_issue_scene(repo_key: str, issue_id: str, scene_id: str):
    """写入 Issue 级场景（VOD 判定后调用）。"""
    # UPDATE issue_state SET scene_id=? WHERE repo_key=? AND issue_id=?
```

### 3.3 issue_state 查询扩展

`get_issue_stage()` 返回的 dict 增加 `scene_id` 字段。

## 四、VOD Skill 改造

### 4.1 Prompt 注入可用场景列表

`src/prompt_builder.py` 的 `_inject_project_info()` 或 VOD 专属 prompt 构建段，追加场景列表：

```
## 可用开发场景（请根据需求特征选择最匹配的场景）

| scene_id | 名称 | 适用场景 | 技能链 |
|----------|------|---------|--------|
| default | 通用开发 | 通用代码开发（Web/API/脚本） | vod → issue-advance → implementation → pr → cloud-deploy |
| cloud-native | 云原生开发 | 容器/K8s/微服务/云资源 | vod → issue-advance → implementation → pr → cloud-deploy |
| iac | IaC开发 | Terraform/资源编排 | vod → iac-issue → iac-coding → iac-qa → iac-sre |
| skill-dev | Skill开发 | Grape/Hermes 技能开发 | vod → skill-issue → skill-creator → skill-tester → skill-publish |
| api-skill-dev | API Skill开发 | REST API 技能开发 | vod → skill-issue → rest-api-skill-creator → rest-api-skill-tester → skill-publish |
| blog-publish | 博客发布 | 博客文章生成发布 | vod → blog-issue → ai-blog-generation → blog-publish |
| agent-dev | Agent开发 | 智能体开发 | vod → issue-advance → implementation → pr → cloud-deploy |

> 场景列表从 configs/pipelines.json 动态读取。
```

来源：`pipeline_config.get_scenes()` + `get_pipeline_config(sid)` 取 nodes 的 skill 链。

### 4.2 VOD 结构化产出增加 recommended_scene

VOD SKILL.md 的输出格式中，JSON 块增加字段：

```json
{
  "recommended_scene": "skill-dev",
  "scene_reason": "需求涉及为 Grape 新增一个 Hermes 技能（SKILL.md），项目知识基线显示仓库含 skills/ 目录且已有同类技能，匹配 skill-dev 场景。",
  "conclusion": "...",
  "actions": ["..."],
  ...
}
```

### 4.3 VOD SKILL.md 修改

在场景 A（A5 A2A 交办）和场景 B（B6 A2A 交办）中，增加场景判定步骤：

**新增 B5.5 场景判定（B5 结构化总结之后、B6 A2A 交办之前）**：

```markdown
#### B5.5 开发场景判定

综合以下信息判定当前 Issue 最匹配的开发场景：

1. **项目知识基线**（prompt 注入的 Project Graph + Project Capabilities）
   - 技术栈：Flask/FastAPI/Express → default；Terraform → iac；K8s → cloud-native
   - 目录结构：有 skills/ → 可能 skill-dev；有 terraform/ → iac
   - 现有能力：已有哪些 skill、API 端点

2. **成员上传文档**（project-info/member/ 下的 md/txt/json）
   - 架构决策文档、技术规范、API 设计文档
   - 用 read tool 读取 {{PROJECT_INFO_DIR}}/member/ 下的文档

3. **需求特征**（Issue title/body + 澄清结果）
   - 关键词匹配：
     - "SKILL"/"技能" → skill-dev / api-skill-dev
     - "Terraform"/"资源编排"/"IaC" → iac
     - "博客"/"文章" → blog-publish
     - "Agent"/"智能体" → agent-dev
     - "容器"/"K8s"/"微服务" → cloud-native
     - 其他 → default

4. **可用场景列表**（prompt 注入的场景表）

判定后在 JSON 产出中输出 `recommended_scene` + `scene_reason`。
若无法确定，留空——服务端回退到仓库默认场景。
```

### 4.4 A2A 交办携带场景信息

VOD 的 A2A 调用（A5/B6）payload 中增加 `recommended_scene` 字段：

```json
{
  "agent": "issue",
  "payload": {
    "repository": "...",
    "issue_number": "8",
    "context": "...",
    "recommended_scene": "skill-dev"
  }
}
```

## 五、服务端改造

### 5.1 A2A 端点接收并持久化场景

`src/api/routes/webhook.py` 的 `a2a_endpoint()`：

```python
# 解析 recommended_scene（VOD 传入）
recommended_scene = payload.get("recommended_scene", "")

if recommended_scene and issue_number:
    from src.api.session_store import set_issue_scene
    set_issue_scene(repo_key, f"#{issue_number}", recommended_scene)
    log.info(f"A2A: scene_id={recommended_scene} set for issue=#{issue_number}")
```

位置：在 `append_event` 创建事件之前（第 288 行附近），确保后续节点路由时 scene 已生效。

### 5.2 VOD done 事件解析场景（兜底）

VOD 执行完成后，`_execute_agent_inner()` 解析 final_text 中的 JSON 块，提取 `recommended_scene`：

`src/api/event_pipeline.py` 的 `_execute_agent_inner()` 中 done 事件处理段（第 1343 行附近）：

```python
if evt_type == "done":
    d = data
    exit_code = d.get("exit_code", -1)
    final_text = d.get("text", "")
    # ...
    # VOD 场景推荐兜底：解析 JSON 中的 recommended_scene
    _scene = parse_agent_output_json(final_text)
    if _scene and _scene.get("recommended_scene") and issue_id:
        from src.api.session_store import set_issue_scene
        set_issue_scene(repo_key, issue_id, _scene["recommended_scene"])
        log.info(f"VOD done: scene={_scene['recommended_scene']} set for {issue_id}")
```

**双重保障**：A2A 调用时传 + VOD done 时从 JSON 解析，任一成功即可。

### 5.3 后续节点 skill 查找使用 Issue 级场景

**核心改动点**——`run_pipeline()` 中的 skill 查找（第 686-722 行）：

```python
# 优先读 Issue 级场景，回退到仓库默认场景
from src.api.session_store import get_issue_scene
_issue_scene = "default"
if issue_id:
    _issue_scene = get_issue_scene(repo_key, issue_id) or "default"

# Plugin mode: 用 issue 级场景查 pipeline
if is_plugin_mode():
    _pipe_skill = resolve_skill_from_pipeline_scene(repo_key, agent_type, _issue_scene)
    ...
```

`resolve_skill_from_pipeline` 改为接收 scene_id 参数（或新增 `resolve_skill_from_pipeline_scene`）：

```python
def resolve_skill_from_pipeline_scene(repo_key: str, agent_type: str, scene_id: str) -> str:
    """按指定场景从 pipelines.json 查 skill。"""
    node = get_node_by_tag(scene_id, agent_type)
    if node:
        return node.get("skill", "")
    # 回退到 default
    node = get_node_by_tag("default", agent_type)
    return node.get("skill", "") if node else ""
```

### 5.4 Legacy 模式同步改动

Legacy 模式（agent-skill-mapping.json）的 skill 查找（第 696-712 行）同样改为读 Issue 级场景：

```python
from src.api.session_store import get_issue_scene
_issue_scene = get_issue_scene(repo_key, issue_id) or get_repo_scene(repo_key) or "default"
_scene = _defaults.get("scenes", {}).get(_issue_scene, _defaults.get("scenes", {}).get("default", {}))
```

### 5.5 prompt_builder 注入场景列表

`src/prompt_builder.py`：

在 `_inject_project_info()` 返回内容末尾（agent_type == "vod" 时），追加可用场景列表：

```python
if agent_type == "vod":
    from src.api.pipeline_config import get_scenes, get_pipeline_config
    scenes = get_scenes()
    lines.append("\n## 可用开发场景")
    lines.append("| scene_id | 名称 | 技能链 |")
    lines.append("|----------|------|--------|")
    for s in scenes:
        cfg = get_pipeline_config(s["id"])
        if cfg:
            chain = " → ".join(n.get("skill", "") for n in cfg.get("nodes", []))
            lines.append(f"| {s['id']} | {s['name']} | {chain} |")
    lines.append("> 请根据项目知识基线 + 需求特征判定 recommended_scene，在 JSON 产出中输出。")
```

## 六、完整数据流

```
issue_opened
  │
  ▼
run_pipeline() → agent_type=vod → 创建 event(seq=N) → 执行 VOD
  │
  │  VOD prompt 包含：
  │  - 项目知识基线（project-graph + project-capabilities）
  │  - 成员上传文档路径（project-info/member/）
  │  - 可用场景列表（从 pipelines.json 动态读取）
  │
  ▼
VOD Agent 执行
  │  1. 读取项目知识基线
  │  2. 读取成员文档（如有）
  │  3. 审视/澄清需求
  │  4. 判定开发场景 → recommended_scene
  │  5. A2A 交办 Issue Agent（payload 带 recommended_scene）
  │  6. 输出 JSON（含 recommended_scene）
  │
  ▼
A2A 端点接收
  │  set_issue_scene(repo_key, issue_id, recommended_scene)
  │  → issue_state.scene_id = "skill-dev"
  │
  ▼
后续 Issue/PR 事件触发 run_pipeline()
  │  agent_type = issue/coding/qa/sre（阶段矩阵路由）
  │  skill 查找：
  │    scene = get_issue_scene(repo_key, issue_id)  ← "skill-dev"
  │    skill = pipelines.json["skill-dev"].nodes[tag=agent_type].skill
  │  → issue 节点用 skill-issue，coding 用 skill-creator，qa 用 skill-tester，sre 用 skill-publish
  │
  ▼
每个节点按场景对应 skill 执行
```

## 七、边界处理

| 场景 | 处理 |
|------|------|
| VOD 未输出 recommended_scene | issue_state.scene_id 为空 → 回退到 managed_repos.dev_scene → default |
| VOD 输出的 scene_id 不存在 | get_pipeline_config 回退到 default |
| 用户手动指定场景 | Dashboard 新增 Issue 级场景切换接口，set_issue_scene() 覆盖 |
| Issue 无 issue_state | get_issue_scene() 回退到 get_repo_scene() |
| 非 VOD 触发的事件（如直接 push） | 无 issue_state.scene_id → 回退到仓库默认场景 |
| 成员文档不存在 | 降级为仅基于项目知识基线 + 需求特征判定 |

## 八、改动清单

| 文件 | 改动 |
|------|------|
| `src/api/session_store.py` | 新增 `get_issue_scene()` / `set_issue_scene()`；`get_issue_stage()` 返回增加 scene_id |
| `src/api/event_pipeline.py` | `run_pipeline()` skill 查找改用 `get_issue_scene()`；`_execute_agent_inner()` done 事件解析 recommended_scene |
| `src/api/routes/webhook.py` | `a2a_endpoint()` 接收 recommended_scene 并持久化 |
| `src/api/pipeline_config.py` | 无需改动（已支持按 scene_id 查 node） |
| `src/prompt_builder.py` | VOD prompt 注入可用场景列表 |
| `skills/vod/SKILL.md` | 新增 B5.5 场景判定步骤；JSON 产出增加 recommended_scene 字段 |
| `skills/vod/references/requirement-elicit-guide.md` | A2A 调用示例增加 recommended_scene 字段 |
| `src/user/db.py` | `get_issue_stage` SQL 增加 scene_id 列（或 session_store 层处理） |
| DB 迁移 | `issue_state` 表加 `scene_id TEXT DEFAULT ''` 列（_ensure_column 幂等） |

## 九、测试要点

1. **VOD 判定 → 后续节点 skill 正确**：VOD 推荐 skill-dev → issue 节点用 skill-issue 而非 issue-advance
2. **回退链**：无 issue_state → 仓库默认 → default
3. **A2A 传场景 + done JSON 解析双重保障**：A2A 未传但 JSON 有 → 仍生效
4. **手动覆盖**：用户设置场景后不被 VOD 覆盖（设置标志位或时序判断）
5. **多 Issue 独立**：同仓库 Issue A 走 skill-dev，Issue B 走 default，互不干扰
