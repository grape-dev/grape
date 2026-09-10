# Issue #67：项目知识库优化 — 详细设计

## 六、技术实现方案

### 6.1 source 字段 DDL

**SQLite**（通过 `_ensure_column` 幂等添加）：

```sql
ALTER TABLE agent_shared_knowledge ADD COLUMN source TEXT NOT NULL DEFAULT '';
```

**MySQL**（原生 DDL）：

```sql
ALTER TABLE agent_shared_knowledge
  ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT '' COMMENT '知识来源：system/agent/manual';
```

**数据回填**（SQLite + MySQL 通用）：

```sql
-- baseline → system
UPDATE agent_shared_knowledge SET source = 'system' WHERE scope = 'baseline' AND (source IS NULL OR source = '');
-- project/issue/issue_agent → agent
UPDATE agent_shared_knowledge SET source = 'agent' WHERE scope IN ('project', 'issue', 'issue_agent') AND (source IS NULL OR source = '');
-- member → manual
UPDATE agent_shared_knowledge SET source = 'manual' WHERE scope = 'member' AND (source IS NULL OR source = '');
```

**写入时设置 source**：

| 函数 | 文件位置 | 设置值 |
|------|---------|--------|
| `write_knowledge()` | `src/api/knowledge.py:184` | `source='agent'`（Agent 沉淀） |
| `build_project_info()` | `src/project_info/builder.py:244` | `source='system'`（VOD 基线，写 DB 时设） |
| `POST /api/knowledge/entry`（新增） | `src/api/routes/knowledge.py` | `source='manual'`（用户输入/文件导入） |

### 6.1.5 description 字段

**DDL**（建表 + 迁移）：

```sql
-- 建表 DDL（db_setup.py）
description TEXT DEFAULT ''  -- 知识描述：system/agent 自动生成，manual 用户填写

-- 迁移（幂等 ALTER）
ALTER TABLE agent_shared_knowledge ADD COLUMN description TEXT DEFAULT '';
```

**自动生成规则**（`write_knowledge` 里按 scope + agent_type 自动生成）：

| scope | agent_type | 自动描述 |
|-------|-----------|---------|
| baseline | — | VOD 基线分析：项目代码结构图谱 / 项目能力清单 / 项目元数据（按文件名区分） |
| project | vod | VOD Agent：需求分析、拆解 Issue、规划开发路径 沉淀的项目级经验 |
| project | coding | Coding Agent：代码实现、修复缺陷、创建 PR 沉淀的项目级经验 |
| project | qa | QA Agent：代码审查、测试验证、质量把关 沉淀的项目级经验 |
| project | sre | SRE Agent：部署发布、监控告警、运维排障 沉淀的项目级经验 |
| project | chat | Chat Agent：对话交互、知识沉淀、流程控制 沉淀的项目级经验 |
| issue | — | Issue #N 对话沉淀的经验 |
| issue_agent | vod/coding/qa/sre/chat | Issue #N {Agent职责} 沉淀的经验 |
| member | — | 空（用户手动填写） |

**baseline INSERT**（`sync_baselines_to_obs`）按文件名生成描述：
- `project-graph.md` → "VOD 基线分析：项目代码结构图谱（模块/依赖/技术栈）"
- `project-capabilities.md` → "VOD 基线分析：项目能力清单（功能特性/支持能力）"
- `.metadata.json` → "VOD 基线分析：项目元数据（分析时间/分支/提交哈希）"

**用户知识**（`POST /api/knowledge/entry`）：用户在新增 modal 填写描述，没填显示"当前用户没有填写描述"。

**列表 API**（`list_knowledge`）：不返回 content 字段（提高性能），只返回 description。

**详情 API**（`GET /api/knowledge/{id}`）：返回 description 字段，详情页面头部展示。

**数据回填**（已有数据）：
```sql
UPDATE agent_shared_knowledge SET description='VOD 基线分析：项目能力清单' WHERE scope='baseline' AND (description IS NULL OR description='');
UPDATE agent_shared_knowledge SET description='Agent 沉淀的项目级经验' WHERE scope='project' AND (description IS NULL OR description='');
UPDATE agent_shared_knowledge SET description=CONCAT('Issue #', issue_num, ' 对话沉淀的经验') WHERE scope='issue' AND (description IS NULL OR description='');
UPDATE agent_shared_knowledge SET description=CONCAT('Issue #', issue_num, ' ', agent_type, ' Agent 沉淀的经验') WHERE scope='issue_agent' AND (description IS NULL OR description='');
```

### 6.1.6 name 字段

**DDL**（建表 + 迁移）：

```sql
-- 建表 DDL（db_setup.py）
name VARCHAR(256) DEFAULT ''  -- 知识名称：system/agent 自动生成，manual 用户必填

-- 迁移（幂等 ALTER）
ALTER TABLE agent_shared_knowledge ADD COLUMN name VARCHAR(256) DEFAULT '';
```

**生成规则**：

| source | 规则 | 示例 |
|--------|------|------|
| system (baseline) | 固定名称 "项目基线分析" | 项目基线分析 |
| agent (project) | "项目知识 - {Agent职责}" | 项目知识 - Coding |
| agent (issue) | "Issue #{N} 知识" | Issue #12 知识 |
| agent (issue_agent) | "Issue #{N} - {Agent职责} 知识" | Issue #12 - Coding 知识 |
| manual | 用户必填（前端校验 + 后端校验），最长 256 字符 | 自定义名称 |

> 注意：system/agent 的 file_path 固定为 `knowledge.md`，不能从文件名截取 name（会全部变成 "knowledge" 重名），必须按 scope + agent_type + issue_num 组合生成。

**write_knowledge**：加 `name` 参数，system/agent 按上述规则自动生成，manual 传入。

**KnowledgeEntryBody**：加 `name: str`（必填）。

**KnowledgeUpdateBody**：加 `name: str`（编辑时可修改）。

**list_knowledge**：SELECT 加 name，返回。

**详情 API**（`GET /api/knowledge/{id}`）：返回 name 字段，详情页面头部标题展示。

**前端**：
- 卡片标题：用 `k.name` 代替从 file_path 截取
- 详情页头部：用 `detail.name` 展示
- 新增页面：加 name 输入框（必填）
- 编辑页面：加 name 输入框

**数据回填**（已有数据）：按 scope + agent_type + issue_num 组合生成 name。

```sql
-- 回填 name：按 scope 生成
UPDATE agent_shared_knowledge SET name='项目基线分析' WHERE scope='baseline' AND (name IS NULL OR name='');
UPDATE agent_shared_knowledge SET name=CONCAT('项目知识 - ', agent_type) WHERE scope='project' AND (name IS NULL OR name='');
UPDATE agent_shared_knowledge SET name=CONCAT('Issue #', issue_num, ' 知识') WHERE scope='issue' AND (name IS NULL OR name='');
UPDATE agent_shared_knowledge SET name=CONCAT('Issue #', issue_num, ' - ', agent_type, ' 知识') WHERE scope='issue_agent' AND (name IS NULL OR name='');
-- manual：从 file_path 文件名截取（去扩展名，`-_` 替换空格）
UPDATE agent_shared_knowledge SET name=REPLACE(REPLACE(REPLACE(
    SUBSTR(file_path, INSTR(file_path, '/') + 1), '_', ' '), '-', ' '), '.md', '')
WHERE scope='member' AND (name IS NULL OR name='');
```

### 6.2 prompt_builder 注入逻辑改动

**当前逻辑**（`src/prompt_builder.py:907-934`）：

`_build_knowledge_section()` 按 scope 三级注入：
1. `read_knowledge(repo_key, agent_type, issue_num)` 返回 `{scope: content}`
2. 遍历 `("project", "issue", "issue_agent")` 注入
3. 追加成员文档摘要

**改动后逻辑**：

```python
def _build_knowledge_section(repo_key: str, agent_type: str = "", issue_num: int = 0, scene_id: str = "") -> str:
    """共享知识段落：查 scene_knowledge_ref 表注入关联知识 + 基线文件。

    向后兼容：无 scene_knowledge_ref 表或无关联记录时，回退到 scope 三级注入。
    """
    lines = ["### 用户反馈沉淀的共享知识（仅 enabled）"]
    found = False

    # 1. 基线知识始终注入（project-graph.md / project-capabilities.md 摘要）
    try:
        from src.runtime.service import load_project_info
        files, meta = load_project_info(repo_key)
        if files:
            found = True
            lines.append("[baseline 级] 项目基线文件：")
            for fname in ("project-graph.md", "project-capabilities.md"):
                if fname in files:
                    body = files[fname].strip()
                    lines.append(f"- {fname}: {body[:200]}{'…' if len(body) > 200 else ''}")
    except Exception:
        pass

    # 2. 查 scene_knowledge_ref 表注入关联知识
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            rows = db.cursor().execute(
                "SELECT DISTINCT k.id, k.content, k.scope, k.source, k.obs_key, k.file_path "
                "FROM scene_knowledge_ref r "
                "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
                "WHERE r.enabled = 1 "
                "AND (r.scene_id = ? OR r.scene_id = '') "
                "AND (r.agent_type = ? OR r.agent_type = '') "
                "AND k.repo_key = ? "
                "AND k.status = 'accepted'",
                (scene_id, agent_type, repo_key)
            ).fetchall()

        for kid, content, scope, source, obs_key, file_path in rows:
            # obs_key 非空 → 从 OBS 读取（已缓存）；否则用 content
            text = content or ""
            if not text and obs_key:
                text = _read_obs_content(obs_key)
            if not text and file_path:
                text = _read_local_file(repo_key, file_path)
            if text:
                found = True
                lines.append(f"[{source} 级] {text[:500]}")
    except Exception:
        # 向后兼容：scene_knowledge_ref 表不存在时回退到 scope 三级注入
        try:
            from src.api.knowledge import read_knowledge
            k = read_knowledge(repo_key, agent_type=agent_type, issue_num=issue_num)
            for scope in ("project", "issue", "issue_agent"):
                content = k.get(scope)
                if content:
                    found = True
                    lines.append(f"[{scope} 级] {content}")
        except Exception:
            pass

    # 3. 成员文档摘要（保持现有逻辑）
    try:
        from src.api.knowledge import member_files
        mf = member_files(repo_key)
        if mf:
            found = True
            lines.append("[member 级] 项目成员上传文档：")
            for rel in sorted(mf):
                body = mf[rel].strip()
                lines.append(f"- {rel.split('/')[-1]}: {body[:200]}{'…' if len(body) > 200 else ''}")
    except Exception:
        pass

    if not found:
        return ""
    return "\n".join(lines)
```

**改动点**：
- 新增 `scene_id` 参数——调用方传入当前执行的场景 ID
- 注入逻辑改为查 `scene_knowledge_ref` 表（`DISTINCT knowledge_id` 去重）
- 向后兼容：表不存在或无关联记录时回退到 `read_knowledge` scope 三级注入
- 基线知识单独注入（不走关联表——基线始终全局注入）
- 成员文档保持现有 `member_files()` 逻辑

**调用方改动**：

| 调用方 | 文件位置 | 改动 |
|--------|---------|------|
| `build_event_chat_prompt()` | `prompt_builder.py:939` | 传入 `scene_id`（从事件上下文取） |
| `build_general_chat_prompt()` | `prompt_builder.py` | 传入 `scene_id`（从会话上下文取） |
| `GET /api/knowledge/context` | `knowledge.py:442` | 传入 `scene_id`（query param 新增） |

### 6.3 GET /api/knowledge/context 端点改动

新增 `scene_id` query 参数：

```python
@router.get("/context", operation_id="knowledge_context")
def knowledge_context(repo: str = Query(""),
                      agent_type: str = "",
                      issue_num: int = 0,
                      scene_id: str = Query(""),  # 新增
                      user: dict = Depends(get_current_user_or_api_key)):
    ...
    knowledge = _build_knowledge_section(repo_key, agent_type=agent_type,
                                         issue_num=issue_num, scene_id=scene_id)
    ...
```

远端 worker 调用时传入 `scene_id`，后端查关联表注入。

### 6.4 write_knowledge() 改动

写入知识后自动生成 `scene_knowledge_ref` 关联记录：

```python
def write_knowledge(*, repo_key, scope, content, status="accepted",
                    agent_type="", issue_num=0, source_seq=0, created_by=""):
    # ... 现有写入逻辑不变 ...

    # 新增：写入后自动生成关联记录
    if status == "accepted":
        _auto_generate_knowledge_ref(knowledge_id, scope, agent_type, issue_num)

    return file_path


def _auto_generate_knowledge_ref(knowledge_id, scope, agent_type, issue_num):
    """系统写入知识时按 scope 规则自动生成关联记录。"""
    from src.session_manager import db, db_lock
    ref_map = {
        "baseline":     {"scene_id": "", "agent_type": ""},           # 全局
        "project":      {"scene_id": "", "agent_type": ""},           # 全局
        "issue":        {"scene_id": "", "agent_type": ""},           # 全局（issue_num 控制同 Issue 生效）
        "issue_agent":  {"scene_id": "", "agent_type": agent_type},   # 指定 Agent
        "member":       {"scene_id": "", "agent_type": agent_type},   # 用户上传时指定的 Agent
    }
    ref = ref_map.get(scope)
    if not ref:
        return
    try:
        with db_lock:
            db.cursor().execute(
                "INSERT OR IGNORE INTO scene_knowledge_ref "
                "(knowledge_id, scene_id, agent_type, enabled, created_by) "
                "VALUES (?, ?, ?, 1, ?)",
                (knowledge_id, ref["scene_id"], ref["agent_type"], "system")
            )
            db.commit()
    except Exception as e:
        log.warning(f"[knowledge] auto generate ref failed: {e}")
```

**注意**：`INSERT OR IGNORE`（SQLite）/ `INSERT IGNORE`（MySQL）避免唯一约束冲突——如果用户已手动创建了相同关联，不覆盖。

### 6.5 数据迁移方案

**步骤 1：创建 scene_knowledge_ref 表**

在 `db_setup.py` 的建表流程中新增 `scene_knowledge_ref` 表 DDL（SQLite + MySQL 两套）。

**步骤 2：回填 source 字段**

执行 6.1 的回填 SQL。

**步骤 3：为现有知识生成关联记录**

```sql
-- baseline 知识 → 全局关联
INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by)
SELECT id, '', '', 1, 'system' FROM agent_shared_knowledge WHERE scope = 'baseline';

-- project 知识 → 全局关联
INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by)
SELECT id, '', '', 1, 'system' FROM agent_shared_knowledge WHERE scope = 'project';

-- issue 知识 → 全局关联（issue_num 控制同 Issue 生效）
INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by)
SELECT id, '', '', 1, 'system' FROM agent_shared_knowledge WHERE scope = 'issue';

-- issue_agent 知识 → 指定 Agent 关联
INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by)
SELECT id, '', agent_type, 1, 'system' FROM agent_shared_knowledge WHERE scope = 'issue_agent' AND agent_type != '';

-- member 知识（agent_type 非空）→ 指定 Agent 关联
INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by)
SELECT id, '', agent_type, 1, 'system' FROM agent_shared_knowledge WHERE scope = 'member' AND agent_type != '';

-- member 知识（agent_type 空）→ 全局关联
INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by)
SELECT id, '', '', 1, 'system' FROM agent_shared_knowledge WHERE scope = 'member' AND (agent_type = '' OR agent_type IS NULL);
```

MySQL 版本把 `INSERT OR IGNORE` 改为 `INSERT IGNORE`。

**步骤 4：验证迁移**

```sql
-- 检查关联记录数 = 知识记录数（每条知识至少一条关联）
SELECT
    (SELECT COUNT(*) FROM agent_shared_knowledge) AS knowledge_count,
    (SELECT COUNT(DISTINCT knowledge_id) FROM scene_knowledge_ref) AS ref_distinct_count;
-- 两个数应该相等
```

### 6.6 知识 CRUD API 详细设计

**POST /api/knowledge/entry（新增知识）**：

```python
@router.post("/entry")
async def create_knowledge_entry(body: KnowledgeEntryBody, user: dict = Depends(get_current_user)):
    """用户直接输入文本创建知识条目（source=manual）。"""
    if not body.content.strip():
        raise HTTPException(status_code=400, ...)
    if not body.repo:
        raise HTTPException(status_code=400, ...)

    repo_key = to_repo_key(body.repo)
    # 写入文件 + DB
    fp = write_knowledge(
        repo_key=repo_key, scope="member", content=body.content,
        status="accepted", agent_type=body.agent_type or "",
        issue_num=0, source_seq=0, created_by=user.get("username", ""),
    )
    # write_knowledge 内部会自动生成关联记录（6.4）
    # 但 manual 知识不自动关联——用户手动选择关联
    # 所以 write_knowledge 对 scope=member 不自动生成关联
    # 用户创建后通过 POST /api/knowledge/{id}/refs 手动关联

    return {"ok": True, "message": "知识已创建"}
```

**PUT /api/knowledge/{id}（编辑知识）**：

```python
@router.put("/{knowledge_id}")
async def update_knowledge(knowledge_id: int, body: KnowledgeUpdateBody,
                           user: dict = Depends(get_current_user)):
    """编辑知识内容，仅 source=manual 可编辑。"""
    # 查知识记录
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, ...)

    # 权限校验：source=manual 且创建者是本人
    if row["source"] != "manual":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "系统生成的知识不可编辑"})
    if row["created_by"] != user.get("username"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅创建者可编辑"})

    # 更新内容：写文件 + OBS + DB content_hash + updated_at
    _update_knowledge_content(knowledge_id, row, body.content)

    return {"ok": True, "message": "知识已更新"}
```

**DEL /api/knowledge/{id}（删除知识）**：

```python
@router.delete("/{knowledge_id}")
async def delete_knowledge(knowledge_id: int, user: dict = Depends(get_current_user)):
    """删除 source=manual 的知识条目。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, ...)

    if row["source"] != "manual":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "系统生成的知识不可删除"})
    if row["created_by"] != user.get("username"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅创建者可删除"})

    # 删除 OBS 对象 + 本地文件 + DB 记录 + 关联记录
    _delete_knowledge_full(knowledge_id, row)

    return {"ok": True, "message": "知识已删除"}
```

### 6.7 关联管理 API 详细设计

**GET /api/knowledge/{id}/refs（查看关联列表）**：

```python
@router.get("/{knowledge_id}/refs")
def get_knowledge_refs(knowledge_id: int, user: dict = Depends(get_current_user)):
    """获取知识的所有关联关系。"""
    with db_lock:
        rows = db.cursor().execute(
            "SELECT id, scene_id, agent_type, enabled, created_by, created_at, updated_at "
            "FROM scene_knowledge_ref WHERE knowledge_id = ? ORDER BY id",
            (knowledge_id,)
        ).fetchall()
    return {"refs": [...]}
```

**POST /api/knowledge/{id}/refs（新增关联——已废弃，保留兼容）**：

> 已被批量接口替代。前端统一使用 `POST /{id}/refs/batch`。

**POST /api/knowledge/{id}/refs/batch（批量新增关联——幂等）**：

```python
class KnowledgeRefBatchBody(BaseModel):
    scene_ids: list[str] = []
    agent_types: list[str] = []

@router.post("/{knowledge_id}/refs/batch")
def add_knowledge_refs_batch(knowledge_id: int, body: KnowledgeRefBatchBody,
                              user: dict = Depends(get_current_user)):
    """批量新增场景/Agent 关联（幂等，跳过已存在的）。"""
    _check_ref_permission(knowledge_id, user)
    # 先查有无已存在记录（含硬删除后重新添加），有就跳过，没有才 INSERT
    # 返回 {ok: True, added: N}
```

> 入参分离：`scene_ids` 只含场景 ID，`agent_types` 只含 Agent 类型。不再混传 `agent_type: ''` 或 `scene_id: ''`。

**PUT /api/knowledge/ref/{ref_id}（修改关联——保留兼容）**：

```python
@router.put("/ref/{ref_id}")
def update_knowledge_ref(ref_id: int, body: KnowledgeRefUpdateBody, ...):
    """修改关联的 enabled 开关。"""
```

> 前端不再使用此接口做软删除——改用 DELETE 硬删除。

**DEL /api/knowledge/ref/{ref_id}（删除关联——硬删除）**：

```python
@router.delete("/ref/{ref_id}")
def delete_knowledge_ref(ref_id: int, ...):
    """硬删除知识的某条关联（DELETE FROM，不软删除）。"""
```

> 前端 `removeAgentRef` / `removeSceneRef` / `toggleGlobalRef` 都用 `DELETE /ref/{id}` 硬删除。
```

### 6.8 write_knowledge() 对 scope=member 的特殊处理

系统写入时自动生成关联记录（6.4），但用户创建的知识（scope=member）不自动关联——由用户手动选择：

```python
def _auto_generate_knowledge_ref(knowledge_id, scope, agent_type, issue_num):
    ref_map = {
        "baseline":     {"scene_id": "", "agent_type": ""},
        "project":      {"scene_id": "", "agent_type": ""},
        "issue":        {"scene_id": "", "agent_type": ""},
        "issue_agent":  {"scene_id": "", "agent_type": agent_type},
        # member 不自动关联——用户手动选择
    }
    ref = ref_map.get(scope)
    if not ref:
        return  # member 跳过
    ...
```

### 6.9 知识列表 API 改动

`GET /api/knowledge` 返回数据以 id 为主键，所有后续操作都以 id 为准。

**权限校验**：`_check_repo_member(user, repo_key)` —— 只有当前仓库的成员（托管者或已加入）才能查看。新增知识同样校验。

**Tab 设计**：只有 3 个 tab（系统知识/Agent 知识/用户知识），无"全部"tab。每个 tab 按 source 筛选后端查询。

**性能优化**：列表不返回 content 字段，不调 `_read_entry_content` 读文件——只返回 description。

**统计接口独立**：`GET /api/knowledge/stats?repo=xxx` 只返回 `{system, agent, manual, unlinked}`，不与列表接口混合。tab 切换只调列表接口（局部刷新），统计只在首次加载和增删后刷新。

**分页**：`page` + `page_size`（默认 20），返回 `total`。前端始终显示分页器。

```python
# 每条知识返回的字段（不含 content，提高性能）：
{
    "id": 42,
    "source": "manual",
    "scope": "member",
    "repo_key": "gitcode:xxx/yyy",
    "agent_type": "coding",
    "issue_num": 0,
    "file_path": "project-info/member/user/xxx.md",
    "obs_key": "knowledge/xxx/yyy/member/user/xxx.md",
    "status": "accepted",
    "source_seq": 0,
    "created_by": "user1",
    "created_at": "2026-09-09 10:00:00",
    "description": "用户填写的知识描述",
    "name": "用户填写的知识名称",
    "archived": 0,
    "refs": [...],
    "ref_summary": "全局",
    "issue_tag": "",
}
```

### 6.10 前端 knowledge.js 改动方案——Vue 3 实现

**引入 Vue 3**：下载到本地 vendor（`/static/js/vendor/vue.global.prod.min.js`），不影响其他页面。

**页面结构**：
- 列表视图 + 详情视图 + 编辑视图 + 新增视图（`view: 'list' | 'detail' | 'create'`，`editMode: bool`）
- 统计卡片在 apple-card 外面（点击联动切换 tab）
- Tab 栏在 apple-card 外面（药丸式 3 tab）
- apple-card 包裹：筛选器 + 卡片网格 + 分页器
- 详情视图：返回按钮 → 头部卡片（source 标签 + name + description + 元数据 + 操作按钮靠右）→ 内容卡片（固定高度滚动）→ Agent 关联卡片（标签式）→ 场景关联卡片（横向滚动）
- 详情头部不显示 scope（baseline/member/project）和 file_path——内部标识对用户无价值
- 编辑视图：返回按钮 → name 输入框（必填，红色 `*`，maxlength=50）→ description 输入框（maxlength=200）→ content textarea（必填，红色 `*`）+ 还原按钮（条件渲染）+ 导入文件按钮 → 取消/确认
- 新增视图：返回按钮 → name 输入框（必填，红色 `*`，maxlength=50）→ description 输入框（maxlength=200）→ content textarea（必填，红色 `*`）+ 导入文件按钮 → Agent 标签（带描述）→ 场景卡片（弹出层选择）→ 取消/确认

**统计卡片**：点击联动切换 tab（`switchTab(source)`），选中态高亮。

**Tab 栏**：药丸式（pill）风格，3 个 tab（系统/Agent/用户），无"全部"。切换时只调 `loadList()` 局部刷新。

**局部刷新**：
- `firstLoad` 标记：首次加载时隐藏内容显示 loading，之后 loading 时只显示 spinner overlay
- `v-show` 代替 `v-if` 控制列表显隐（保留 DOM，不重新创建）
- `loadList` 只更新 `this.list` 和 `this.total`，Vue 响应式更新
- `loadStats` 独立调用 `/api/knowledge/stats`，不影响列表
- loading 时：spinner overlay + grid `min-height:320px`（不消失，不闪烁）

**卡片样式**（参考场景中心 scene-market）：
- `.scn-card` 结构：色条 + 图标 + 标题(name) + 描述(description) + 关联 pill + 底部按钮区
- `border-radius:18px` + `box-shadow:var(--sh)` + hover `translateY(-4px)` + 加重阴影
- 底部按钮区：`border-top:1px solid var(--b)` 横线隔离 + `justify-content:flex-end` 靠右
- 卡片标题用 `k.name`（为空时回退到 `knowledgeTitle(k)` 从 file_path 截取）
- 描述：`white-space:pre-line` + `word-break:break-word` + `min-height:38px` + `line-clamp:2` + `title` 属性 tooltip
- 关联 pill：`.kb-ref-pill`（场景 N + Agent N），原生 `title` 属性显示名称列表（hover 浏览器自带 tooltip）
- 点击卡片进入详情页面
- **高度统一**：head `min-height:32px`(标题 nowrap) + desc `min/max-height:38px` + pill 行 `min-height:17px` → 所有卡片 foot 对齐
- **CSS 隔离**：知识页面样式用 `.knowledge-page` 前缀覆盖，不影响场景市场全局样式

**详情页面按 source 控制权限**：
| source | Agent 关联卡片 | 场景关联卡片 | 操作 |
|--------|---------------|-------------|------|
| system | 不显示 | 不显示 | 显示"自动加载"提示 |
| agent | 不显示 | 不显示 | 只查看 |
| manual | 标签式（点击切换选中/取消） | 卡片 + "+添加场景"弹出层 | 可编辑/删除 |

**Agent 关联交互**（标签式，同新增页面）：
- 6 个 Agent 类型标签（VOD/Issue/Coding/QA/SRE/Chat），每个带职责描述
- 点击切换选中/取消（`toggleAgentRef` → 批量 API 添加 / DELETE 删除）
- 选中态：蓝色边框 + 背景高亮 + 右上角蓝色实心圆（白色 ✓）
- 未选中态：灰色空心圆（无符号）
- iOS 风格 toggle 视觉

> 全局关联（scene_id='' + agent_type=''）已移除——概念多余，system/agent 知识自动注入，manual 通过 Agent 标签 + 场景关联覆盖。

**场景关联交互**（卡片 + 弹出层）：
- 已选场景：`.scn-card` 卡片横向滚动（`scn-scroll-wrap` + `scn-scroll-track`）+ 左右箭头按钮
- 卡片展示：色条 + 图标 + 标题(scene_display_name) + 描述 + 技能链路(pipeline)
- 每个卡片有 X 按钮删除（硬删除 `DELETE /ref/{id}`）
- "+添加场景"打开 modal 弹出层：仓库场景列表（紧凑行列表 + 搜索 + 选中圆圈 + 确认）

**场景选择弹出层**（modal overlay + Teleport to body）：
- 只显示当前仓库的场景（`GET /api/scenes?repo=xxx`，不显示场景市场）
- 紧凑行列表（`.kb-scene-row`）+ 搜索框
- 点击选中/取消（空心圆 → 蓝色实心圆 ✓，同 Agent 标签风格）
- 固定高度 480px，列表超出滚动
- 底部：已选数量 + 取消/确认按钮
- 点击遮罩关闭
- 新增页面用临时选中（`createSceneTempSelected`），确认后才写入 `createForm.sceneSelected`

**分页器**：始终显示，`page / totalPages (total)` + 20/50 每页切换。

**关联管理 API**：
- 批量新增：`POST /{id}/refs/batch` → `{scene_ids: [], agent_types: []}`（幂等，跳过已存在的）
- 单个新增：同批量接口，传单个元素
- 删除：`DELETE /ref/{id}`（硬删除，不软删除）
- `loadRefs` 调详情接口（`GET /{id}`）刷新完整 refs（含 scene_name/pipeline 等）

**新增/编辑页面**（非 modal）：
- 新增：`view='create'`，name 必填 + description + content + Agent 标签 + 场景选择
- 编辑：`editMode=true`，name 必填 + description + content
- 创建后批量关联 Agent + 场景：`POST /{id}/refs/batch`

**文件导入**（前端 FileReader，不走后端接口）：
- 支持格式：`.md`、`.json`、`.txt`
- 大小限制：10KB（`file.size > 10240` 拒绝）
- 行为：FileReader 读取文件内容覆盖 textarea（用户可预览编辑后再提交）
- 导入后自动填充 name（如果 name 为空，从文件名截取）
- 新增和编辑页面都支持

**还原按钮**（编辑页面）：
- 语义：还原（Revert）——一步回到编辑打开时的快照，不是撤销（Undo）栈式多步回退
- 条件渲染：`v-if editForm.content !== editFormSnapshot.content`（内容变化才显示）
- 位置：content textarea 上方，和导入文件按钮并排（内容区局部操作，不放底部按钮栏）
- tooltip：放弃本次所有修改，恢复到进入编辑时的内容

**表单校验**（前端 + 后端双重）：
- name：必填，≤50 字符（maxlength + 后端校验）
- content：必填，≤10KB（UTF-8 字节数，`new Blob([text]).size` + 后端 `len(content.encode("utf-8"))`）
- description：≤200 字符（maxlength + 后端校验）
- name 不强制唯一——展示名，用 id 做唯一标识
- 必填标记：红色 `*`（`.kb-required` class，`color:var(--r)`）

**Vue 生命周期管理**：
- `nav('knowledge')` 调 `renderKnowledge()` 创建 Vue 应用
- `nav()` 切走时先 `unmount()` 再 `innerHTML = ''`
- 已在 knowledge 页面时 `nav('knowledge')` 直接 return（防止重复 mount）
- `renderKnowledge()` 里 `el.innerHTML = ''` 清空容器后再 mount

### 6.11 i18n 改动

新增 i18n key（zh + en `src/api/static/locales/{zh,en}/knowledge.json`）：

```json
{
  "stats_system": "系统知识",
  "stats_agent": "Agent 知识",
  "stats_user": "用户知识",
  "stats_unlinked": "未关联",
  "filter_source": "来源",
  "filter_source_all": "全部",
  "filter_ref_status": "关联状态",
  "filter_ref_all": "全部",
  "filter_ref_linked": "已关联",
  "filter_ref_unlinked": "未关联",
  "source_system": "系统",
  "source_agent": "Agent",
  "source_manual": "用户",
  "ref_linked_scenes": "已关联 {count} 个场景",
  "ref_linked_agent": "已关联 {agent}",
  "ref_unlinked": "未关联",
  "ref_settings": "关联设置",
  "ref_modal_title": "管理知识关联",
  "ref_scene": "关联场景",
  "ref_scene_all": "所有场景",
  "ref_agent": "关联 Agent",
  "ref_agent_all": "所有 Agent",
  "ref_enabled": "启用",
  "ref_disabled": "禁用",
  "ref_add": "新增关联",
  "ref_delete": "删除关联",
  "ref_delete_confirm": "确认删除此关联？",
  "ref_already_bound": "该知识已绑定此场景/Agent 类型",
  "edit_title": "编辑知识",
  "edit_content": "内容",
  "edit_save": "编辑",
  "entry_title": "新增知识",
  "entry_content": "输入知识内容（支持 Markdown）",
  "entry_create": "创建",
  "entry_description": "知识描述（简述这条知识的用途）",
  "entry_name": "知识名称",
  "entry_name_ph": "请输入知识名称（必填）",
  "name_required": "知识名称必填",
  "import_file": "导入文件",
  "file_too_large": "文件超过 10KB 限制",
  "file_imported": "文件已导入",
  "knowledge_deleted": "知识已删除",
  "knowledge_updated": "知识已更新",
  "knowledge_created": "知识已创建",
  "delete_confirm": "确认删除此知识？关联关系将一并删除。",
  "system_readonly": "系统生成的知识不可编辑"
}
```

> 当前实际 i18n 共 **121 keys**（zh/en 对称）。

### 6.12 统计卡片数据接口

统计数据前端计算——不单独加后端接口。

`GET /api/knowledge` 返回的统一列表里每条知识有 `source` + `refs` 字段，前端计算统计数：

```javascript
const stats = {
    system: list.filter(k => k.source === 'system' || k.source === 'agent').length,
    user: list.filter(k => k.source === 'manual').length,
    unlinked: list.filter(k => !k.refs || k.refs.length === 0).length,
};
```

注意：统计卡片"系统知识"包含 system + agent 两类——因为它们都是系统/Agent 产生的，用户视角是一样的（只读）。如果用户要分来源看，用筛选器按来源筛选。

### 6.13 issue/issue_agent 级知识在统一列表的展示

当前 `GET /api/knowledge` 只返回 project tab 和 agent tab 的知识，不返回 issue/issue_agent 级。

改动 `list_knowledge` 端点（`knowledge.py:346`）——新增返回 issue 级知识：

```python
# 在返回数据中新增 issue 级知识
issue_knowledge = []
try:
    with db_lock:
        rows = db.cursor().execute(
            "SELECT id, scope, agent_type, issue_num, file_path, obs_key, "
            "source_seq, created_by, created_at, content_hash, source "
            "FROM agent_shared_knowledge "
            "WHERE repo_key = ? AND scope IN ('issue', 'issue_agent') "
            "ORDER BY issue_num DESC, id DESC",
            (repo_key,)
        ).fetchall()
    for r in rows:
        content = _read_entry_content(repo_key, r[5] or "", r[4] or "")
        issue_knowledge.append({
            "id": r[0], "scope": r[1], "agent_type": r[2] or "",
            "issue_num": r[3] or 0, "path": r[4], "obs_key": r[5] or "",
            "source_seq": r[6] or 0, "created_by": r[7] or "",
            "created_at": normalize_event_time(r[8] or ""),
            "content": content, "source": r[10] or "agent",
            "issue_tag": f"Issue #{r[3]}" if r[3] else "",
        })
except Exception:
    pass
```

统一列表里 issue 级知识卡片显示 `Issue #N` 标签——用户能区分这条知识是哪个 Issue 产生的。Agent 级的显示 `Issue #N · {agent_type}`。

### 6.14 _update_knowledge_content 实现细节

```python
def _update_knowledge_content(knowledge_id: int, row: dict, new_content: str):
    """更新知识内容：写文件 + OBS + DB content_hash + updated_at。"""
    from src.repo_key import to_repo_key
    repo_key = row["repo_key"]
    file_path = row["file_path"]

    # 1. 写本地文件（覆盖，不是追加）
    base = _repo_base(repo_key)
    full = os.path.normpath(os.path.join(base, file_path))
    # 安全检查
    if full != base and not full.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法路径"})
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(new_content)

    # 2. 写 OBS（启用时）+ 更新 content_hash
    obs_key = row.get("obs_key", "")
    content_hash = ""
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None and obs_key:
            with open(full, "rb") as f:
                file_bytes = f.read()
            storage.save_bytes(obs_key, file_bytes)
            import hashlib
            content_hash = hashlib.sha256(file_bytes).hexdigest()
    except Exception as e:
        log.warning(f"[knowledge] OBS update failed for {obs_key}: {e}")

    # 3. 更新 DB
    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_shared_knowledge "
            "SET content_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (content_hash, knowledge_id)
        )
        db.commit()
```

### 6.15 _delete_knowledge_full 实现细节

```python
def _delete_knowledge_full(knowledge_id: int, row: dict):
    """删除知识：OBS 对象 + 本地文件 + DB 记录 + 关联记录。"""
    from src.session_manager import db, db_lock

    # 1. 删除 OBS 对象
    obs_key = row.get("obs_key", "")
    if obs_key:
        try:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                storage.delete_object(obs_key)
        except Exception as e:
            log.warning(f"[knowledge] OBS delete failed for {obs_key}: {e}")

    # 2. 删除本地文件
    file_path = row.get("file_path", "")
    if file_path:
        base = _repo_base(row["repo_key"])
        full = os.path.normpath(os.path.join(base, file_path))
        if os.path.isfile(full):
            try:
                os.remove(full)
            except Exception:
                pass

    # 3. 删除 DB 记录 + 关联记录（MySQL 外键 CASCADE 自动删 ref，SQLite 手动删）
    with db_lock:
        db.cursor().execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id = ?", (knowledge_id,))
        db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE id = ?", (knowledge_id,))
        db.commit()
```

### 6.16 搜索实现

后端实现——`GET /api/knowledge` 接收 `q` 参数，WHERE 条件 `name LIKE %q% OR description LIKE %q%`。

前端传 `q` 参数给后端，后端 SQL 过滤后返回匹配结果。不做前端过滤——数据量可能大，后端分页 + LIKE 查询更高效。

**最新排序**：`sort=latest` 参数 → `ORDER BY updated_at DESC`。前端"最新"按钮 toggle 切换，选中态蓝色填充。

**筛选器布局**（全部靠左对齐）：`[关联状态下拉] [搜索框] [查询] [最新] [新增知识]`

### 6.17 关联管理 API 权限校验

关联管理 API 的权限规则：

| 操作 | 系统/Agent 知识 | 用户知识（本人创建） | 用户知识（他人创建） |
|------|----------------|-------------------|-------------------|
| 查看关联（GET refs） | ✅ 所有登录用户 | ✅ 所有登录用户 | ✅ 所有登录用户 |
| 新增关联（POST refs） | ✅ 仓库托管者 | ✅ 创建者本人 | ❌ 403 |
| 修改关联 enabled（PUT ref） | ✅ 仓库托管者 | ✅ 创建者本人 | ❌ 403 |
| 删除关联（DEL ref） | ✅ 仓库托管者 | ✅ 创建者本人 | ❌ 403 |

在关联管理 API 里加权限校验：

```python
def _check_ref_permission(knowledge_id: int, user: dict):
    """校验用户是否有权限管理该知识的关联。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})

    if row["source"] == "manual":
        # 用户创建的知识——仅创建者本人可管理关联
        if row["created_by"] != user.get("username"):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅创建者可管理关联"})
    # system/agent 知识——仓库托管者可管理关联
    # TODO: 检查 user 是否是该仓库的托管者（复用现有 is_admin 逻辑）
```

### 6.18 用户手动创建知识的存储路径

通过 `POST /api/knowledge/entry` 创建的知识，scope=member，写入路径：

```
{WORKSPACE_DIR}/{owner}/{repo}/project-info/member/{username}/{timestamp}_{random}.md
```

手动创建用时间戳命名（因为没有文件名）：

```python
import time, uuid
filename = f"entry_{int(time.time())}_{uuid.uuid4().hex[:6]}.md"
member_dir = os.path.join(base, "project-info", "member", str(user.get("username", "user")))
file_path = os.path.join(member_dir, filename)
```

OBS 模式下 obs_key 格式：`knowledge/{owner}/{repo}/member/{username}/{filename}`

**文件导入**（前端 FileReader，不走后端接口）：

新增/编辑页面支持通过文件导入内容：
- 支持格式：`.md`、`.json`、`.txt`
- 大小限制：10KB（`file.size > 10240` 拒绝）
- 行为：FileReader 读取文件内容覆盖 textarea（用户可预览编辑后再提交）
- 导入后自动填充 name（如果 name 为空，从文件名截取）
- 纯前端操作，不调后端 upload 接口

> 原 `POST /api/knowledge`（upload_knowledge）接口已删除——前端文件导入完全替代，不需要后端文件上传接口。

### 6.19 事件详情页知识引用链路

**P9 问题**：Agent 执行任务时注入了哪些知识，用户完全不知道。

**方案**：Agent 执行任务后，记录注入的知识 ID 列表到事件线程。

`prompt_builder` 注入知识时记录引用的知识 ID：

```python
def _build_knowledge_section(repo_key, agent_type, issue_num, scene_id):
    injected_ids = []  # 记录注入的知识 ID

    # ... 查 scene_knowledge_ref 表注入 ...
    for kid, content, ... in rows:
        injected_ids.append(kid)
        ...

    # 返回注入的知识 ID 列表（调用方可记录到事件线程）
    return "\n".join(lines), injected_ids
```

`build_event_chat_prompt` 调用时拿到 `injected_ids`，记录到事件线程消息：

```python
# 在 _execute_conv_task 里
prompt, injected_ids = build_event_chat_prompt(...)
# 执行完成后记录
_add_thread_message(seq, repo_key, issue_id, agent_type, "system",
                    f"📌 本次注入知识：{', '.join(str(i) for i in injected_ids)}")
```

前端事件详情页展示——在对话线程里显示"本次注入了 N 条知识"，点击展开看知识列表。

### 6.20 测试方案

**后端测试**（`tests/test_knowledge_ref.py` 新增）：

| 测试类 | 测试用例 | 说明 |
|--------|---------|------|
| TestKnowledgeRefTable | test_table_creation | scene_knowledge_ref 表创建成功 |
| | test_unique_constraint | 三元组唯一约束生效——重复插入报错 |
| | test_cascade_delete | 知识删除时关联记录自动删除（MySQL）/手动删除（SQLite） |
| TestKnowledgeCRUD | test_create_entry | POST /entry 创建知识成功 |
| | test_create_entry_no_content | 空内容返回 400 |
| | test_update_manual | PUT /{id} 编辑 manual 知识成功 |
| | test_update_system_forbidden | PUT /{id} 编辑 system 知识返回 403 |
| | test_update_other_user_forbidden | PUT /{id} 编辑他人知识返回 403 |
| | test_delete_manual | DEL /{id} 删除 manual 知识成功 |
| | test_delete_system_forbidden | DEL /{id} 删除 system 知识返回 403 |
| TestKnowledgeRef | test_add_ref | POST /{id}/refs 新增关联成功 |
| | test_add_ref_duplicate | 重复关联返回 409 |
| | test_update_ref_enabled | PUT /ref/{id} 开关 enabled 成功 |
| | test_delete_ref | DEL /ref/{id} 删除关联成功 |
| | test_get_refs | GET /{id}/refs 返回关联列表 |
| TestPromptBuilder | test_inject_by_ref | 有关联记录时按 ref 注入 |
| | test_inject_fallback | 无 scene_knowledge_ref 表时回退 scope 注入 |
| | test_inject_distinct | 多条 ref 命中同一知识时去重 |
| TestDataMigration | test_source_backfill | source 字段回填正确 |
| | test_ref_auto_generate | 现有知识自动生成关联记录 |

**前端测试**（Playwright）：

| 测试项 | 说明 |
|--------|------|
| 统计卡片显示 | 3 个卡片数量正确 |
| 点击统计卡片筛选 | 点击后列表联动筛选 |
| 来源筛选 | 按系统/Agent/用户筛选正确 |
| 关联状态筛选 | 按已关联/未关联筛选正确 |
| 全文搜索 | 搜索内容匹配 |
| 知识卡片来源标签 | 颜色和文字正确 |
| 知识卡片操作按钮权限 | system 只有查看+关联，manual 有编辑+删除+关联 |
| 编辑 modal | 编辑后内容更新 |
| 关联设置 modal | 新增/删除/开关关联 |
| 新增知识 modal | 创建后出现在列表 |
| issue 级知识展示 | 显示 Issue #N 标签 |

### 6.21 知识过期处理逻辑

**设计原则**：不同来源的知识有不同的生命周期策略。

| source | scope | 过期策略 | 理由 |
|--------|-------|---------|------|
| system | baseline | 不过期 | 项目基线，Issue 打开时自动刷新覆盖 |
| agent | project | 不过期，但用户可标记"过时" | 全局知识，用户判断是否还有效 |
| agent | issue | Issue 关闭后归档，90 天后清理 | Issue 级知识，关闭后无 Agent 再读 |
| agent | issue_agent | Issue 关闭后归档，90 天后清理 | 同上 |
| manual | member | 不过期 | 用户创建，用户自己管理删除 |

**归档机制**：

`agent_shared_knowledge` 表新增 `archived` 字段：

```sql
-- SQLite
ALTER TABLE agent_shared_knowledge ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;

-- MySQL
ALTER TABLE agent_shared_knowledge ADD COLUMN archived TINYINT NOT NULL DEFAULT 0 COMMENT '0正常 1归档';
```

归档后的知识：
- 不再注入到 Agent prompt（`prompt_builder` 查关联表时过滤 `archived=0`）
- 知识库页面默认不显示（列表查询 `WHERE archived=0`）
- 后端保留 `PUT /{id}/archive` 接口 + `show_archived` 查询参数，前端入口暂未开放
- 关联记录保留但 `enabled` 自动设为 0

**Issue 关闭触发归档**：

在 Issue 关闭时（event_pipeline 处理 Issue closed 事件），把该 Issue 的 issue/issue_agent 级知识标记为归档：

```python
def _archive_issue_knowledge(repo_key: str, issue_num: int):
    """Issue 关闭时归档该 Issue 的知识。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # 归档知识记录
        c.execute(
            "UPDATE agent_shared_knowledge SET archived = 1 "
            "WHERE repo_key = ? AND scope IN ('issue', 'issue_agent') "
            "AND issue_num = ?",
            (repo_key, issue_num)
        )
        # 关联记录 enabled 设为 0
        c.execute(
            "UPDATE scene_knowledge_ref SET enabled = 0, updated_by = 'system' "
            "WHERE knowledge_id IN ("
            "  SELECT id FROM agent_shared_knowledge "
            "  WHERE repo_key = ? AND scope IN ('issue', 'issue_agent') AND issue_num = ?"
            ")",
            (repo_key, issue_num)
        )
        db.commit()
```

**调用方**：在 `event_pipeline.py` 处理 `issue_closed` 事件时调用 `_archive_issue_knowledge`。

**过期清理**：

定时任务清理超过 90 天的归档知识（和 `skill_sync_scheduler` 类似，加一个定时器）：

```python
def cleanup_archived_knowledge():
    """清理超过 90 天的归档知识。"""
    from src.session_manager import db, db_lock
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()

    with db_lock:
        c = db.cursor()
        # 查过期归档知识
        rows = c.execute(
            "SELECT id, obs_key, file_path FROM agent_shared_knowledge "
            "WHERE archived = 1 AND updated_at < ?",
            (cutoff,)
        ).fetchall()

        for kid, obs_key, file_path in rows:
            # 删 OBS 对象
            if obs_key:
                try:
                    from src.storage import get_storage
                    storage = get_storage()
                    if storage is not None:
                        storage.delete_object(obs_key)
                except Exception:
                    pass
            # 删本地文件
            if file_path:
                # ... 删本地文件逻辑 ...
                pass
            # 删关联记录 + DB 记录
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id = ?", (kid,))
            c.execute("DELETE FROM agent_shared_knowledge WHERE id = ?", (kid,))

        db.commit()
        log.info(f"[knowledge] cleaned up {len(rows)} archived knowledge entries")
```

**定时触发**：在 `main.py` 的 `start_skill_sync_scheduler` 旁边加一个 `start_knowledge_cleanup_scheduler`，每天执行一次。

**project 级知识文件过大处理**：

project 级知识一直在追加，不会自动过期。文件越来越大时，提示用户整理：

- 知识库页面读取 project 级知识文件大小，超过阈值（10KB）时卡片上显示"文件较大（15KB），建议整理"提示
- 用户点击"整理"→ 打开编辑 modal，看到全文内容，手动删除不需要的行，保存后覆盖写回文件 + OBS
- 用户主动清理，不是系统自动瘦身——保留有用的，去掉过时的
- 知识卡片同时显示"标记过时"按钮——如果用户觉得整个 project 级知识都没用了，标记后 `archived=1` 不再注入

**前端展示**（当前状态）：

归档功能前端入口已移除——后端接口保留，前端暂不展示归档知识。后续需要时加回：
- 列表默认查询 `WHERE archived=0`
- 后端 `show_archived=true` 参数 + `ORDER BY archived ASC, id DESC` 已就绪
- `PUT /{id}/archive` 接口保留

**文件大小提示前端实现**：

```javascript
// _renderKnowledgeCard 里判断文件大小
const sizeBytes = k.content ? k.content.length : 0;
const SIZE_THRESHOLD = 10 * 1024;  // 10KB
if (k.scope === 'project' && sizeBytes > SIZE_THRESHOLD) {
    const sizeKB = (sizeBytes / 1024).toFixed(1);
    html += `<div class="tr" style="font-size:10px;color:var(--or);margin-top:4px">⚠ ${I18N.t('knowledge.file_too_large', {size: sizeKB})}</div>`;
    // "整理"按钮和"编辑"按钮共用同一个编辑 modal
}
```

i18n 新增 key：
```json
{
  "file_too_large": "文件较大（{size}KB），建议整理去除不需要的知识"
}
```

**影响范围**：
- `agent_shared_knowledge` 表：新增 `archived` 字段
- `event_pipeline.py`：Issue closed 事件处理时调 `_archive_issue_knowledge`
- `main.py`：新增 `start_knowledge_cleanup_scheduler` 定时任务
- `knowledge.py`：`list_knowledge` 返回 `archived` 字段 + 归档知识默认不返回（加 `archived=0` 过滤）
- `prompt_builder.py`：注入时过滤 `archived=0`
- `knowledge.js`：归档知识卡片样式 + "标记过时"按钮 + "显示归档"筛选开关

### 6.22 共用文件多条记录问题处理

**问题**：`write_knowledge` 每次追加一行到同一个文件，但每次都插入一条新 DB 记录。导致 9 条 DB 记录指向同一个文件（同一个 obs_key + 同一个 file_path），但 OBS 只有一份（最后一次写入覆盖了前面所有版本）。前 8 条的 content_hash 和实际文件内容不匹配。

**处理方案**：写入时先查 DB 有无同 scope + repo_key + agent_type + issue_num 的记录：

- **有记录** → 追加内容到文件 + 更新 OBS + 更新已有记录的 content_hash + updated_at（不插入新记录）
- **没记录** → 追加内容到文件 + 写 OBS + 插入新记录

这样一个 scope 一个文件一条 DB 记录。多次写入只追加文件内容，DB 记录只更新不新增。

**三个 scope 都适用**——`write_knowledge` 是统一入口，不管 scope 是 project / issue / issue_agent 都走同一段逻辑：

| scope | agent_type | issue_num | 文件路径 | DB 查询条件 | 效果 |
|-------|------------|-----------|---------|------------|------|
| project | '' | 0 | `project-info/knowledge.md` | scope + repo_key | 一个项目一条记录 |
| issue | '' | 15 | `issues/15/knowledge.md` | scope + repo_key + issue_num | 一个 Issue 一条记录 |
| issue_agent | coding | 15 | `issues/15/coding/knowledge.md` | scope + repo_key + agent_type + issue_num | 一个 Issue 的一个 Agent 一条记录 |

三个 scope 的文件路径本来就不同（`knowledge_path` 返回不同路径），现在 DB 记录也对齐了——一个文件一条记录，不再重复。

**改动函数**：`write_knowledge()`（`src/api/knowledge.py:184`）

```python
def write_knowledge(*, repo_key, scope, content, status="accepted",
                    agent_type="", issue_num=0, source_seq=0, created_by=""):
    # ... 现有写入逻辑不变（追加到文件 + 写 OBS）...

    # 改动：DB 写入逻辑——先查有无已存在记录
    with db_lock:
        c = db.cursor()
        existing = c.execute(
            "SELECT id FROM agent_shared_knowledge "
            "WHERE repo_key=? AND scope=? "
            "AND (agent_type=? OR (agent_type='' AND ?='')) "
            "AND issue_num=?",
            (repo_key, scope, agent_type, agent_type, issue_num)
        ).fetchone()

        if existing:
            # 有记录 → 更新 content_hash + updated_at，不插入新记录
            c.execute(
                "UPDATE agent_shared_knowledge "
                "SET content_hash=?, obs_key=?, updated_at=datetime('now') "
                "WHERE id=?",
                (content_hash, obs_key, existing[0])
            )
            knowledge_id = existing[0]
        else:
            # 无记录 → 插入新记录
            c.execute(
                "INSERT INTO agent_shared_knowledge "
                "(scope, repo_key, agent_type, issue_num, file_path, obs_key, "
                " content_hash, status, source_seq, created_by, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (scope, repo_key, agent_type, issue_num, rel_path, obs_key,
                 content_hash, status, source_seq, created_by, source or "agent")
            )
            knowledge_id = c.lastrowid
        db.commit()

    # 自动生成关联记录（6.4）
    if status == "accepted":
        _auto_generate_knowledge_ref(knowledge_id, scope, agent_type, issue_num)

    return file_path
```

**影响**：
- DB 记录数大幅减少——一个 scope 一条记录，不再每次写入都新增
- content_hash 始终和文件内容一致——每次写入后更新
- OBS 对象数不增加——同一个 scope 始终是同一个 obs_key
- 关联记录只生成一次——已有记录时不重复生成

**对 CRUD 的影响**：
- 编辑/删除：操作的是整个文件（整个 scope 的知识），不是单条。用户标记"过时"时整个 scope 标记，不删单行
- 归档：Issue 关闭时按 issue_num 归档，一个 issue_num 一条记录，归档操作干净
- 知识库页面：一个 scope 一张卡片，不再显示 9 张指向同一文件的卡片

**现有数据迁移**：

```sql
-- 删除重复记录，每个 scope + repo_key + agent_type + issue_num 只保留最新一条
DELETE FROM agent_shared_knowledge
WHERE id NOT IN (
    SELECT MAX(id) FROM agent_shared_knowledge
    GROUP BY repo_key, scope, agent_type, issue_num
);
```

MySQL 版本相同。

---

### 6.23 遗漏点补充

#### 6.23.1 source_seq 在 UPDATE 时更新为最新值

6.22 的 UPDATE 语句没更新 source_seq。一个 scope 一条记录后，文件里有多行不同 seq，DB 里 source_seq 存最后一次写入的 seq——表示这个知识文件最近一次被哪个事件更新。虽然不能代表全部行，但记录最新更新事件有追溯价值。

```python
if existing:
    c.execute(
        "UPDATE agent_shared_knowledge "
        "SET content_hash=?, obs_key=?, source_seq=?, updated_at=datetime('now') "
        "WHERE id=?",
        (content_hash, obs_key, source_seq, existing[0])
    )
```

前端统一列表里不单独展示 seq（一条记录对应多行，展示单个 seq 误导用户），但在详情页可以展示"最近更新事件 seq"。

#### 6.23.2 status=rejected 的处理

`write_knowledge` 收到 `status=rejected` 时，不追加文件、不写 OBS。DB 逻辑：

- **有已存在记录**（不管 accepted 还是 rejected）→ 直接跳过，什么都不做
- **无已存在记录** → 插入一条 rejected 记录（登记备查，不注入）

```python
if status == "rejected":
    # rejected 只登记索引，不落文件
    if existing:
        return ""  # 已有记录，跳过——不覆盖已有 accepted
    # 无记录 → 插入 rejected 记录
    c.execute("INSERT INTO agent_shared_knowledge (...) VALUES (...)", (...))
    db.commit()
    return ""  # 不写文件
```

永远只有一条记录。rejected 来了如果已有记录就忽略——已有的要么是 accepted（已有知识，rejected 不影响），要么是 rejected（之前已登记过）。

rejected 的使用场景：event-dialogue skill 里用户提了建议，Agent 判断"不值得沉淀"，用 rejected 登记留痕"考虑过但不采纳"。实际使用频率很低。

#### 6.23.3 upload_knowledge 已删除

原 `upload_knowledge` 接口（`POST /api/knowledge`）已删除。前端文件导入（FileReader）完全替代——用户在新增/编辑页面导入文件内容到 textarea，通过 `POST /api/knowledge/entry` 提交。不再需要后端文件上传接口。

#### 6.23.4 建表 DDL 写进 db_setup.py

新部署实例需要在建表 DDL 里直接包含 `source` + `archived` 字段，不等 ALTER TABLE。

`agent_shared_knowledge` 建表 DDL 更新（`db_setup.py:420`）：

```sql
CREATE TABLE IF NOT EXISTS agent_shared_knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    repo_key    TEXT NOT NULL,
    agent_type  TEXT DEFAULT '',
    issue_num   INTEGER DEFAULT 0,
    file_path   TEXT NOT NULL,
    status      TEXT DEFAULT 'accepted',
    source_seq  INTEGER DEFAULT 0,
    created_by  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    obs_key      VARCHAR(512) DEFAULT '',
    content_hash VARCHAR(64) DEFAULT '',
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    source       TEXT NOT NULL DEFAULT '',          -- 新增：system/agent/manual
    archived     INTEGER NOT NULL DEFAULT 0          -- 新增：0正常 1归档
);
```

`scene_knowledge_ref` 建表 DDL（新增，放在 `agent_shared_knowledge` 之后）：

```sql
-- SQLite 版
CREATE TABLE IF NOT EXISTS scene_knowledge_ref (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id  INTEGER NOT NULL,
    scene_id      TEXT NOT NULL DEFAULT '',
    agent_type    TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_by    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by    TEXT NOT NULL DEFAULT '',
    UNIQUE (knowledge_id, scene_id, agent_type)
);
```

对已有部署，通过 `_ensure_column` 幂等添加 source + archived 字段（6.1 + 6.21 已写）。

#### 6.23.5 MySQL 原生 DDL 执行机制

grape 没有绕过翻译层执行原生 MySQL DDL 的机制。在 `db_setup.py` 新增 `_execute_raw_mysql_ddl` 函数：

```python
def _execute_raw_mysql_ddl(ddl: str):
    """执行原生 MySQL DDL，绕过 mysql_compat 翻译层。仅在 MySQL 后端时执行。"""
    from src.mysql_compat import get_db_backend
    if get_db_backend() != "mysql":
        return  # SQLite 模式跳过
    from src.mysql_compat import mysql_config
    import pymysql
    cfg = mysql_config()
    conn = pymysql.connect(
        host=cfg.get("host", ""), port=cfg.get("port", 3306),
        user=cfg.get("user", ""), password=cfg.get("password", ""),
        database=cfg.get("database", ""), charset="utf8mb4",
        autocommit=True
    )
    try:
        conn.execute(ddl)
    finally:
        conn.close()
```

在 `db_setup.py` 建表流程中，`scene_knowledge_ref` 表用两套 DDL：
- SQLite：走翻译层（现有机制）
- MySQL：调 `_execute_raw_mysql_ddl` 执行 6.1 里的原生 MySQL DDL

#### 6.23.6 prompt_builder 返回值统一

6.2 和 6.19 的返回值不一致。统一为返回 `(str, list)`：

```python
def _build_knowledge_section(repo_key, agent_type="", issue_num=0, scene_id=""):
    """返回 (知识段落文本, 注入的知识 ID 列表)。"""
    lines = [...]
    injected_ids = []
    # ... 注入逻辑 ...
    for kid, ... in rows:
        injected_ids.append(kid)
        ...
    return "\n".join(lines), injected_ids
```

调用方改动：
- `build_event_chat_prompt`：`knowledge, injected_ids = _build_knowledge_section(...)`
- `GET /api/knowledge/context`：`knowledge, _ = _build_knowledge_section(...)`

如果不需要 injected_ids 的调用方用 `_` 忽略。

#### 6.23.7 关联管理权限校验完善

system/agent 知识的关联管理权限——仓库托管者可管理。复用 `host_repo` 的 `is_admin` 逻辑：

```python
def _check_ref_permission(knowledge_id: int, user: dict):
    """校验用户是否有权限管理该知识的关联。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})

    if row["source"] == "manual":
        # 用户创建的知识——仅创建者本人可管理关联
        if row["created_by"] != user.get("username"):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅创建者可管理关联"})
    else:
        # system/agent 知识——仓库托管者可管理关联
        from src.user.db import is_repo_admin
        if not is_repo_admin(user["id"], row["repo_key"]):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅仓库托管者可管理系统知识的关联"})
```

`is_repo_admin` 复用 `user_db.mark_user_repo_join` 里已有的 `is_admin` 字段。

#### 6.23.8 _project_knowledge_entries() 和 _agent_knowledge_entries() 改动

6.22 改了记录结构后（一个 scope 一条记录），这两个聚合函数简化：

`_project_knowledge_entries`（`knowledge.py:263`）——查 DB 从 `GROUP BY` 改为直接查唯一记录：

```python
def _project_knowledge_entries(repo_key: str) -> list:
    """项目级知识沉淀——一个 scope 一条记录。"""
    with db_lock:
        rows = db.cursor().execute(
            "SELECT id, scope, agent_type, issue_num, file_path, status, "
            "source_seq, created_by, created_at, obs_key, source, archived "
            "FROM agent_shared_knowledge "
            "WHERE repo_key=? AND scope='project' AND archived=0 "
            "ORDER BY id DESC",
            (repo_key,)
        ).fetchall()
    # 每条返回 id + source + archived + content + refs + ref_summary
    ...
```

`_agent_knowledge_entries`（`knowledge.py:279`）——同理简化，不再有重复记录：

```python
def _agent_knowledge_entries(repo_key: str) -> dict[str, list]:
    """Agent 级知识——按 agent_type 分组，每组一个 scope 一条记录。"""
    with db_lock:
        rows = db.cursor().execute(
            "SELECT id, scope, agent_type, issue_num, file_path, status, "
            "source_seq, created_by, created_at, obs_key, source, archived "
            "FROM agent_shared_knowledge "
            "WHERE repo_key=? AND scope IN ('issue','issue_agent') AND archived=0 "
            "ORDER BY agent_type, id DESC",
            (repo_key,)
        ).fetchall()
    ...
```

#### 6.23.9 knowledge_files() 给远端 worker 交付知识

`knowledge_files()`（`knowledge.py:252`）调 `read_knowledge()` 读取知识内容。6.22 改了记录结构但不影响 `read_knowledge`——`read_knowledge` 按文件路径读取，文件还是同一个文件，内容是追加的全文。不受影响。

#### 6.23.10 obs.py 知识同步逻辑

`obs.py:202` 按 scope 循环查 `_query_knowledge_obs` 获取 obs_key。6.22 改了记录结构后，一个 scope 只有一条记录，`_query_knowledge_obs` 查到的 obs_key 唯一——不受影响。反而更干净，不会再查到多条同 obs_key 的记录。

#### 6.23.11 Vue 引入方式——本地 vendor

下载 Vue 3 prod min JS 到 `src/api/static/js/vendor/` 目录，和其他 vendor 文件（jQuery、marked、select2）一起托管：

```html
<script src="/static/js/vendor/vue.global.prod.min.js?v=1"></script>
```

不依赖外部 CDN，不受网络影响，公网/内网都能用。只需要一行 `<script>` 标签，不需要链式加载逻辑。

版本：Vue 3.4.30（写死版本号，不用 @latest）

升级时：下载新版 vendor 文件 + 升 `?v=` 版本号。

#### 6.23.12 关联数量限制 20 条后端校验

`POST /api/knowledge/{id}/refs` 新增关联时校验数量：

```python
@router.post("/{knowledge_id}/refs")
def add_knowledge_ref(knowledge_id: int, body: KnowledgeRefBody, user: dict = Depends(get_current_user)):
    _check_ref_permission(knowledge_id, user)

    # 校验关联数量——同一 scene_id + agent_type 最多 20 条
    with db_lock:
        count = db.cursor().execute(
            "SELECT COUNT(*) FROM scene_knowledge_ref "
            "WHERE knowledge_id=? AND scene_id=? AND agent_type=? AND enabled=1",
            (knowledge_id, body.scene_id or "", body.agent_type or "")
        ).fetchone()[0]

    if count >= 20:
        raise HTTPException(status_code=400,
            detail={"code": 400, "message": "每个场景每个 Agent 节点最多关联 20 条知识"})

    # ... 新增关联逻辑 ...
```

#### 6.23.13 标记归档接口实现

```python
@router.put("/{knowledge_id}/archive")
def archive_knowledge(knowledge_id: int, body: ArchiveBody, user: dict = Depends(get_current_user)):
    """标记/取消归档。body.archived=true 标记归档，false 取消归档。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})

    # 权限：仓库托管者可操作
    from src.user.db import is_repo_admin
    if not is_repo_admin(user["id"], row["repo_key"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅仓库托管者可操作"})

    archived = 1 if body.archived else 0
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE agent_shared_knowledge SET archived=? WHERE id=?",
            (archived, knowledge_id)
        )
        # 归档时关联记录 enabled 设为 0，取消归档时恢复
        c.execute(
            "UPDATE scene_knowledge_ref SET enabled=? WHERE knowledge_id=?",
            (0 if archived else 1, knowledge_id)
        )
        db.commit()
    return {"ok": True, "archived": archived}
```

#### 6.23.14 GET /api/knowledge/{id} 查询详情接口实现

```python
@router.get("/{knowledge_id}", operation_id="knowledge_detail")
def get_knowledge_detail(knowledge_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """按 id 查询单条知识全文。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})

    # 读取全文
    content = _read_entry_content(row["repo_key"], row.get("obs_key", ""), row.get("file_path", ""))

    # 查关联——LEFT JOIN devops_pipeline_scene 获取场景名称 + 描述 + pipeline
    with db_lock:
        refs = db.cursor().execute(
            "SELECT r.id, r.scene_id, r.agent_type, r.enabled, r.created_by, r.created_at, "
            "s.name, s.description, s.display_name, s.scene_id "
            "FROM scene_knowledge_ref r "
            "LEFT JOIN devops_pipeline_scene s ON r.scene_id = s.scene_id AND s.repo_key = ? "
            "WHERE r.knowledge_id = ? ORDER BY r.id",
            (row["repo_key"], knowledge_id)
        ).fetchall()

    # 为每个场景关联补充 pipeline 技能链路
    # ref_list 包含：scene_name, scene_display_name, scene_description, scene_pipeline

    return {
        "id": knowledge_id,
        "source": row["source"],
        "scope": row["scope"],
        "repo_key": row["repo_key"],
        "agent_type": row["agent_type"],
        "issue_num": row["issue_num"],
        "file_path": row["file_path"],
        "obs_key": row["obs_key"],
        "content": content,
        "archived": row["archived"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "description": row.get("description", ""),
        "name": row.get("name", ""),
        "refs": ref_list,  # 含 scene_name/scene_display_name/scene_description/scene_pipeline
    }
```

#### 6.23.15 events.py add_knowledge 调用不受影响

`events.py:1286` 调 `write_knowledge()` 传 `scope` + `content` + `status` + `agent_type` + `issue_num` + `source_seq` + `created_by`。

6.22 改的是 `write_knowledge` 内部的 DB 写入逻辑（先查再 INSERT/UPDATE），调用方式不变——`add_knowledge` 不需要改。

`write_knowledge` 新增 `source` 参数，默认值 `"agent"`——`add_knowledge` 不传 source 时自动用 `"agent"`，行为正确。

---

### 6.24 补充遗漏点

#### 6.24.1 Vue 应用卸载

`Vue.createApp().mount()` 返回的实例需要手动调 `unmount()` 清理响应式系统和事件监听。innerHTML 替换只移除 DOM，Vue 内部的 watcher / computed 不会被清理——内存泄漏。

**方案**：`renderKnowledge()` 保存 Vue 实例引用，`nav()` 切走时调 unmount：

```javascript
// knowledge.js 全局变量
var _knowledgeVueApp = null;

function renderKnowledge() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  if (_knowledgeVueApp) { _knowledgeVueApp.unmount(); _knowledgeVueApp = null; }
  _knowledgeVueApp = Vue.createApp({ ... });
  _knowledgeVueApp.mount(el);
}
```

`app.js` 的 `nav()` 里切走 knowledge 时：`if (window._knowledgeVueApp) { window._knowledgeVueApp.unmount(); window._knowledgeVueApp = null; }`

#### 6.24.2 _get_knowledge_by_id 函数实现

```python
def _get_knowledge_by_id(knowledge_id: int) -> dict | None:
    from src.session_manager import db, db_lock
    with db_lock:
        row = db.cursor().execute(
            "SELECT id, scope, repo_key, agent_type, issue_num, file_path, "
            "status, source_seq, created_by, created_at, obs_key, content_hash, "
            "updated_at, source, archived FROM agent_shared_knowledge WHERE id = ?",
            (knowledge_id,)).fetchone()
    if not row: return None
    return {"id": row[0], "scope": row[1], "repo_key": row[2], ...}
```

#### 6.24.3 Pydantic Body 模型定义

```python
class KnowledgeEntryBody(BaseModel):
    repo: str = ""
    content: str = ""
    agent_type: str = ""

class KnowledgeUpdateBody(BaseModel):
    content: str = ""

class KnowledgeRefBody(BaseModel):
    scene_id: str = ""
    agent_type: str = ""

class KnowledgeRefUpdateBody(BaseModel):
    enabled: bool = True

class ArchiveBody(BaseModel):
    archived: bool = True
```

#### 6.24.4 路由冲突解决

FastAPI 按声明顺序匹配。固定路径路由（`/entry`、`/content`、`/context`、`/ref/{id}`）放在 `{id}` 路由之前声明。现有 `DEL /api/knowledge?repo=xxx&file=xxx` 和新增 `DEL /api/knowledge/{id}` 不冲突——前者无 path param。

#### 6.24.5 Vue _loadData 和 API 返回格式对齐

6.9 改后 API 返回 `d.knowledge` + `d.stats`，Vue 直接用：

```javascript
_loadData(d) {
    this.list = d.knowledge || [];
    this.stats = d.stats || { system: 0, agent: 0, manual: 0, unlinked: 0 };
},
```

不再需要 `_flattenList`。

#### 6.24.6 苹果风格确认 modal 提取为全局函数

skills.js 里内联实现的 modal 提取到 `app.js` 作为全局 `_showAppleConfirm(message, onConfirm, onCancel)`。knowledge.js 和 skills.js 都调用这个全局函数。

#### 6.24.7 MySQL 数据迁移去重 SQL

MySQL 不允许子查询引用要删除的表，用临时表：

```sql
CREATE TEMPORARY TABLE _keep_ids AS
SELECT MAX(id) AS keep_id FROM agent_shared_knowledge
GROUP BY repo_key, scope, agent_type, issue_num;
DELETE FROM agent_shared_knowledge WHERE id NOT IN (SELECT keep_id FROM _keep_ids);
DROP TEMPORARY TABLE _keep_ids;
```

#### 6.24.8 build_project_info 和 entry 设置 source

- `POST /api/knowledge/entry`：INSERT 语句含 `source='manual'`（含文件导入场景）
- `build_project_info`：写 baseline 到 DB 时加 `source='system'`
- ~~`upload_knowledge`：已删除~~

#### 6.24.9 事件详情页知识引用链路前端展示

events.js 渲染线程消息时，判断 `msg.content.startsWith('📌 本次注入知识：')`，解析知识 ID 列表，渲染为"📌 本次注入 N 条知识"+ 查看详情按钮。点击调 `GET /api/knowledge/{id}` 查询知识标题展示在 modal 里。

i18n 新增：`injected_knowledge_count`、`view_injected_detail`

### 6.25 性能优化

#### 6.25.1 详情接口 pipeline 批量查询

**问题**：`GET /api/knowledge/{id}` 在循环里为每个场景关联调 `get_pipeline_config`——每个场景 1 次 db_lock + PRAGMA + SELECT。8 个场景 = 8 次串行 DB 往返，MySQL 后端下导致请求卡住。

**修复**：循环前预计算 `pipeline_map`：
```python
scene_ids = [r[9] for r in refs if r[9] and row["repo_key"]]
pipeline_map = {}
for sid in scene_ids:
    pc = get_pipeline_config(sid, repo_key=row["repo_key"])
    if pc and pc.get("nodes"):
        pipeline_map[sid] = " → ".join(...)
# 循环里只做 dict 查找
pipeline_chain = pipeline_map.get(r[9], "")
```

#### 6.25.2 死代码清理

删除从未被调用的函数：
- `_project_knowledge_entries`：循环读 OBS，0 调用
- `_agent_knowledge_entries`：循环读 OBS + agent_files，0 调用
- `_repo_root_rel`：路径转换，0 调用
- `_build_ref_summary` 里的 `has_global` 检查：全局关联已移除

#### 6.25.3 编辑/新增保存路径不一致修复

**问题**：`_update_knowledge_content` 写文件用 `_repo_base(repo_key)` = `WORKSPACE_DIR/owner/repo/rel`，但 `_read_ws_file` 读文件用 `WORKSPACE_DIR/rel`（缺少 owner/repo 前缀）。`create_knowledge_entry` 存 DB 的 `rel` 用 `os.path.relpath(file_path, WORKSPACE_DIR)` 含 owner/repo 前缀，读时又拼一次——路径重复。

**修复**：
- `_read_ws_file`：本地 fallback 用 `_repo_base(repo_key)` + `rel`（和写文件一致）
- `create_knowledge_entry`：`rel = os.path.relpath(file_path, base)`（相对仓库根，不含 owner/repo）

#### 6.25.4 新增页面场景卡片补全

新增页面的已选场景卡片之前只有标题，补全为和详情页一致的结构：
- 色条 + 图标 + 标题 + 描述 + pipeline 链路（`nodes[].tag` 用 `→` 连接）
- `loadRepoScenes`：从场景 API 的 `nodes` 字段提取 pipeline
- `getSceneDesc` / `getScenePipeline` 方法

#### 6.25.5 详情页内容英文换行

`.kb-detail-content` 加 `word-break:break-word` + `white-space:pre-wrap`——长英文单词正常换行。

#### 6.25.6 知识注入逻辑——member_files 全量扫描移除 + issue_num 精确匹配

**问题 1**：`_build_knowledge_section` 有两条注入路径：
1. `scene_knowledge_ref` 表精确注入（Issue #67 新逻辑）
2. `member_files` 全量扫描 `project-info/member/` 目录注入（旧逻辑遗留）

同一条 manual 知识被注入两次——一次通过 ref 表 `[manual 级]`，一次通过 member_files `[member 级]`。

**修复 1**：移除 `member_files` 全量扫描注入，完全走 `scene_knowledge_ref` 表。

**问题 2**：`scene_knowledge_ref` 表没有 `issue_num` 字段——Issue #1 和 Issue #2 的知识会同时注入到任意 Issue。老逻辑用 `_query_knowledge_obs` 按 `WHERE issue_num=N` 精确查询，新逻辑丢失了这个维度。

**修复 2**：`scene_knowledge_ref` 表加 `issue_num` 字段（INTEGER NOT NULL DEFAULT 0）。

**新老逻辑比对**（含文件路径）：

写入路径（`write_knowledge` / `create_knowledge_entry`，Issue #67 未改写入逻辑）：

| scope | 本地文件路径 | OBS key |
|-------|------------|---------|
| baseline | `{repo_base}/project-info/baseline.md` | `knowledge/{owner}/{repo}/baseline/baseline.md` |
| project | `{repo_base}/project-info/knowledge.md` | `knowledge/{owner}/{repo}/project/knowledge.md` |
| issue | `{repo_base}/issues/{N}/knowledge.md` | `knowledge/{owner}/{repo}/issues/{N}/knowledge.md` |
| issue_agent | `{repo_base}/issues/{N}/{agent}/knowledge.md` | `knowledge/{owner}/{repo}/issues/{N}/{agent}/knowledge.md` |
| member | `{repo_base}/project-info/member/{user}/entry_{ts}_{rand}.md` | `knowledge/{owner}/{repo}/member/{user}/entry_{ts}_{rand}.md` |

auto_ref 规则（`_auto_generate_knowledge_ref`，写入后自动生成）：

| scope | scene_id | agent_type | issue_num | 注入范围 |
|-------|----------|------------|-----------|---------|
| baseline | '' | '' | 0 | 全局（所有场景所有 Agent） |
| project | '' | 写入Agent | 0 | 该 Agent 所有场景 |
| issue | '' | '' | 实际Issue号 | 该 Issue 所有 Agent |
| issue_agent | '' | 写入Agent | 实际Issue号 | 该 Issue 该 Agent |
| member | 用户选择 | 用户选择 | 0 | 用户手动控制 |

注入 SQL（`_build_knowledge_section`）：

```sql
SELECT DISTINCT k.id, k.scope, k.source, k.obs_key, k.file_path, k.content_hash
FROM scene_knowledge_ref r
JOIN agent_shared_knowledge k ON r.knowledge_id = k.id
WHERE r.enabled = 1
  AND (r.scene_id = ? OR r.scene_id = '')        -- 匹配当前场景 OR 全局
  AND (r.agent_type = ? OR r.agent_type = '')     -- 匹配当前Agent OR 全局
  AND (r.issue_num = ? OR r.issue_num = 0)        -- 匹配当前Issue OR 全局
  AND k.repo_key=***
  AND k.status = 'accepted'
  AND k.archived = 0
```

**issue_num 条件安全性分析**：

- `issue_num=0` 语义：全局（不限 Issue），与 `scene_id=''`、`agent_type=''` 一致
- 正常场景：Coding Agent 处理 Issue #5（issue_num=5）→ 匹配 issue_num=5（当前 Issue）OR issue_num=0（全局），不匹配 issue_num=3（其他 Issue）
- issue_num 传 None：SQL `r.issue_num = NULL`（FALSE）+ `r.issue_num = 0`（匹配全局）→ Issue 级知识不注入，全局知识注入（安全 fallback）
- issue_num 传 0（对话场景无 Issue）：只匹配全局，Issue 级知识不注入（正确）
- DB 字段 `INTEGER NOT NULL DEFAULT 0`：不可能为 NULL，迁移已有数据填充 0
- MySQL/SQLite 兼容：INTEGER 类型参数绑定无问题

读取路径（`_read_entry_content`）：

| 模式 | 路径 |
|------|------|
| OBS（obs_key 非空 + content_hash 非空） | `read_with_cache(obs_key, content_hash)` — hash 缓存 LRU |
| OBS（obs_key 非空 + content_hash 空） | `storage.read_bytes(obs_key)` — 直接读 OBS |
| 非OBS / OBS miss | `_read_ws_file(repo_key, file_path)` → `open({repo_base}/{file_path})` |

**结论**：
- baseline：无影响（独立注入，不走 ref 表）
- project：无影响（issue_num=0 全局，agent_type 精确匹配写入 Agent）
- issue：**恢复精确匹配**——老逻辑按 `WHERE issue_num=N` 查，新逻辑 `(issue_num=N OR issue_num=0)` 精确匹配 OR 全局
- issue_agent：**恢复精确匹配**——同 issue + agent_type 精确
- manual：从"全部注入"改为"关联了才注入"，issue_num=0（用户不选 Issue）
- 向后兼容 fallback（ref 表不存在时回退 `read_knowledge` 三级 scope）：只对 system/agent 知识生效
- `member_files` 仍在 `_build_vod_scenes_section` 中用于加载 `pipelines-user.json`，与知识注入无关

**OBS hash 缓存**：`_read_entry_content` 优先走 `read_with_cache(obs_key, content_hash)`——和老逻辑一致，避免每次注入都读 OBS。`_build_knowledge_section` SQL 查询带 `content_hash` 字段。

**空关联防护**：`POST /{id}/refs/batch` 接口跳过 `scene_id='' + agent_type=''` 的空关联（`continue`）——用户不选场景不选 Agent 是合法操作，但不生成全局注入记录。
