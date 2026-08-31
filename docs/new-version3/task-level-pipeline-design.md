# 任务级流水线方案分析

## 一、现状分析

### 1.1 当前架构关键点

| 维度 | 现状 | 问题 |
|------|------|------|
| 场景级别 | **仓库级**（`managed_repos.dev_scene`） | 同一仓库所有 Issue 共用一个场景，无法按任务类型区分 |
| 流水线阶段 | **固定 5 阶段**（VOD→Issue→Coding→QA→SRE） | `AGENT_ORDER` 硬编码，所有场景走同一条路径 |
| VOD 职责边界 | 澄清需求 → A2A 交办 Issue Agent | VOD 不感知场景，无法根据需求选择流水线 |
| A2A 机制 | `POST /api/a2a {"agent":"issue", "context":"..."}` | context 是纯文本摘要，不携带结构化场景信息 |
| Skill 映射 | `devops_agent_session`（per repo+agent_type） | 会话级绑定，同一仓库的 Issue Agent 恒用同一 skill |
| issue_state | vod→issue→coding→qa→sre→done | 阶段机也是固定 5 段，不支持自定义流程 |

### 1.2 数据流梳理（当前）

```
Issue 创建 → webhook → run_pipeline
  → issue_opened 恒路由给 VOD
  → set_issue_stage(vod)
  → VOD 执行（澄清需求，≤3 轮）
  → VOD 调 A2A → POST /api/a2a {"agent":"issue"}
    → webhook.py: 查 devops_agent_session 获取 skill
    → fallback: 读 managed_repos.dev_scene → agent-skill-mapping.json
    → set_issue_stage(issue)
    → 创建 event + 分派引擎执行
  → Issue Agent 执行（spec/标签/分配）
  → Issue Agent 调 A2A → coding
  → Coding Agent 执行（编码/PR）
  → Coding Agent 调 A2A → qa
  → QA Agent 执行（审查/测试）
  → QA Agent 调 A2A → sre
  → SRE Agent 执行（部署/发布）
```

关键瓶颈：**VOD 澄清完需求后，无条件 A2A 给 Issue Agent**。后续全链路用的 skill 由 `devops_agent_session`（仓库级场景）决定，与本次任务的实际需求类型无关。

### 1.3 场景配置现状（agent-skill-mapping.json）

```
default       → vod + issue-advance + implementation + pr + cloud-deploy
cloud-native  → 同 default
iac           → iac-vod + iac-issue + iac-coding + iac-qa + iac-sre
agent-dev     → 同 default
skill-dev     → vod + skill-issue + skill-creator + skill-tester + skill-publish
api-skill-dev → vod + skill-issue + rest-api-skill-creator + rest-api-skill-tester + skill-publish
blog-publish  → vod + issue-advance + ai-blog-generation + pr + blog-publish
```

7 个场景，但选择权在 Dashboard 手动切换（仓库级），VOD 无法根据 Issue 内容自动选择。

---

## 二、目标方案

### 2.1 核心思路

**VOD 澄清需求后，根据需求特征选择对应场景的流水线，后续 Agent 按该场景的 skill 映射执行。**

```
Issue → VOD 澄清 → 判定场景（如 iac / skill-dev / blog-publish / default）
  → A2A 交办时携带 scene_id
  → 后续每个 Agent 用该 scene_id 对应的 skill 执行
  → Pipeline 页面展示任务级场景标签
```

### 2.2 方案分层

#### 层 1：数据模型 — issue 级场景绑定

**新增字段**：`issue_state.scene_id`

```
issue_state 表新增列：
  scene_id TEXT DEFAULT ''   -- VOD 判定的场景 ID（空则回退仓库级 dev_scene）
```

- VOD 在 A2A 交办时写入 `issue_state.scene_id`
- 后续所有 Agent 的 skill 查找优先读 `issue_state.scene_id`，空则回退 `managed_repos.dev_scene`

#### 层 2：A2A 协议扩展 — 携带场景信息

**扩展 A2A payload**：

```json
{
  "agent": "issue",
  "payload": {
    "repository": "songspace/hello-blog",
    "issue_number": "18",
    "context": "VOD 已完成需求收敛...",
    "scene_id": "iac"           ← 新增：VOD 判定的场景
  }
}
```

- `webhook.py` 的 `a2a_endpoint` 读取 `scene_id`，写入 `issue_state`
- 后续 Agent 分派时，skill 查找改为 `issue_state.scene_id → agent-skill-mapping.json`

#### 层 3：VOD 能力增强 — 场景判定

VOD SKILL.md 新增「场景判定」步骤（B5 之后、B6 之前）：

```
#### B5.5 场景判定（澄清后、A2A 交办前）

根据需求特征判定开发场景：

| 需求特征 | 场景 | scene_id |
|----------|------|----------|
| 基础设施/资源管理/terraform | IaC开发 | iac |
| 创建/修改 DevOps Skill | Skill开发 | skill-dev |
| 创建 REST API Skill | API Skill开发 | api-skill-dev |
| 博客/文章生成与发布 | 博客发布 | blog-publish |
| 云原生/容器/K8s | 云原生开发 | cloud-native |
| 通用开发（Web/后端/脚本等） | 通用开发 | default |

判定依据：
- Issue title/body 中的关键词
- 项目知识基线（技术栈、已有 skills）
- 澄清过程中确认的需求方向

A2A 交办时携带 scene_id：
  POST /api/a2a {"agent":"issue", "payload": {..., "scene_id": "iac"}}
```

#### 层 4：Skill 查找链路改造

**当前**（3 处调用点）：
```
devops_agent_session.skill → fallback: managed_repos.dev_scene → agent-skill-mapping.json
```

**改造后**：
```
devops_agent_session.skill → fallback: issue_state.scene_id → managed_repos.dev_scene → agent-skill-mapping.json
```

3 个调用点：
1. `webhook.py` a2a_endpoint 的 skill fallback（~L164）
2. `event_pipeline.py` 的 pipeline skill fallback（~L656）
3. `event_pipeline.py` 的 subscription dispatch skill fallback（~L409）

改造方式：在读取 scene_id 时，优先从 `issue_state.scene_id` 获取：
```python
def resolve_issue_scene(repo_key: str, issue_id: str) -> str:
    """优先读 issue 级场景，回退仓库级"""
    from src.api.session_store import get_issue_stage
    st = get_issue_stage(repo_key, issue_id)
    if st and st.get("scene_id"):
        return st["scene_id"]
    from src.user.db import get_repo_scene
    return get_repo_scene(repo_key) or "default"
```

#### 层 5：Pipeline 展示增强

Pipeline 页面每个 Issue 卡片新增场景标签：

```
Active: Issue #18  ·  场景: IaC开发  ·  责任人: ...
[VOD] → [Issue] → [Coding] → [QA] → [SRE]
```

后端 `pipeline.py` 的 `_get_issue_pipeline` 读取 `issue_state.scene_id` 返回给前端。

---

## 三、改动清单

### 3.1 后端（Python）

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/api/session_store.py` | `issue_state` 表加 `scene_id` 列；新增 `get_issue_scene()` / `set_issue_scene()` | DB 迁移 + 读写方法 |
| `src/api/routes/webhook.py` | a2a_endpoint 读取 `payload.scene_id`，写入 `issue_state`；skill fallback 改用 `resolve_issue_scene()` | A2A 协议扩展 |
| `src/api/event_pipeline.py` | 3 处 skill fallback 改用 `resolve_issue_scene()`；`set_issue_stage` 时同步写入 scene_id | 链路改造 |
| `src/api/routes/pipeline.py` | `_get_issue_pipeline` 返回 `scene_id` 字段 | 展示增强 |
| `src/config.py` 或 `src/api/session_store.py` | 新增 `resolve_issue_scene()` 公共方法 | 统一场景查找 |

### 3.2 Skill（VOD）

| 文件 | 改动 | 说明 |
|------|------|------|
| `skills/vod/SKILL.md` | 新增 B5.5 场景判定步骤；A2A 交办格式加 `scene_id` | VOD 能力增强 |
| `skills/vod/references/requirement-elicit-guide.md` | §五 A2A 格式加 `scene_id` 字段；新增场景判定规则表 | 配套文档 |

### 3.3 前端（JS）

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/api/static/js/pages/pipeline.js` | Issue 卡片展示场景标签 | UI 增强 |
| `src/api/static/index.html` | bump ?v=N | 缓存刷新 |

### 3.4 DB 迁移

```sql
-- issue_state 表加 scene_id 列（兼容 SQLite/MySQL）
ALTER TABLE issue_state ADD COLUMN scene_id TEXT DEFAULT '';
```

走 `db_setup` 的 `_ensure_column` 迁移机制（与现有模式一致）。

---

## 四、关键设计决策

### 4.1 场景判定权在 VOD，不在 Issue Agent

**理由**：VOD 是第一个接触 Issue 的 Agent，在做需求澄清时已经理解了需求意图。让 VOD 判定场景最自然，避免 Issue Agent 二次判定导致的语义漂移。

**Issue Agent 仍然信任 VOD 的判定**：Issue Agent 的职责是 spec/标签/分配，不做场景切换。如果 Issue Agent 发现场景不对，可在评论中建议人工调整（兜底）。

### 4.2 scene_id 写入 issue_state，不改 devops_agent_session

**理由**：
- `devops_agent_session` 是仓库级会话配置（引擎绑定、enable、订阅），场景只影响 skill 选择
- `issue_state` 已经是 per-issue 级别，天然适合存储任务级场景
- 不影响仓库级场景配置（Dashboard 切换场景仍然作为 fallback）

### 4.3 不改 AGENT_ORDER（仍 5 阶段）

**理由**：
- 所有场景的 mapping 都包含 vod/issue/coding/qa/sre 5 个角色，只是 skill 不同
- 阶段流程不变，变得是每个阶段用的 skill（如 iac 场景的 coding 用 iac-coding 而非 implementation）
- 未来如果某个场景需要跳过某阶段（如 blog-publish 不需要 QA），可通过 Agent enable 控制，不需要改 AGENT_ORDER

### 4.4 向后兼容

- `scene_id` 为空时，回退到 `managed_repos.dev_scene`（现有行为不变）
- 旧的 A2A 调用不携带 `scene_id` 也能正常工作
- 仓库级场景配置仍然有效（作为 fallback）

---

## 五、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| VOD 场景判定不准 | 后续 Agent 用错 skill | Issue Agent 可在 spec 中标注建议场景；Pipeline 页面支持手动切换场景（重新触发） |
| issue_state 表迁移 | 旧数据 scene_id 为空 | 空值回退仓库级场景，行为不变 |
| VOD SKILL 改动 | 需重新部署 VOD worker | 走 skill 更新流程（skill_zip 分发） |
| A2A payload 兼容 | 旧 worker 不发 scene_id | webhook.py 对 scene_id 做容错（空值回退） |

---

## 六、实施路径建议

```
Phase 1: 数据模型 + 后端链路（1-2 天）
  ├── DB: issue_state 加 scene_id 列
  ├── session_store: 新增 get/set_issue_scene + resolve_issue_scene
  ├── webhook.py: A2A 读取 scene_id + skill 查找改造
  ├── event_pipeline.py: 3 处 skill fallback 改造
  └── pipeline.py: 返回 scene_id

Phase 2: VOD Skill 增强（0.5 天）
  ├── SKILL.md: 新增 B5.5 场景判定
  └── requirement-elicit-guide.md: A2A 格式 + 判定规则

Phase 3: 前端展示（0.5 天）
  ├── pipeline.js: 场景标签
  └── index.html: bump version

Phase 4: 验证测试
  ├── 创建不同类型 Issue（通用/iac/blog）
  ├── 验证 VOD 判定场景 → A2A 携带 scene_id
  ├── 验证后续 Agent 使用正确 skill
  └── Pipeline 页面展示场景标签
```

---

## 七、与现有「仓库级场景」的关系

```
优先级链路（高 → 低）：
  1. issue_state.scene_id          ← VOD 按任务判定（本方案新增）
  2. managed_repos.dev_scene       ← Dashboard 手动设置（现有）
  3. "default"                     ← 兜底默认

语义：
  - 仓库级场景 = 该仓库的「默认开发模式」（大多数 Issue 的典型场景）
  - 任务级场景 = 特定 Issue 的「实际需求场景」（VOD 判定，覆盖仓库级）
  - 典型用法：仓库设为 default，VOD 按需把 iac 类 Issue 切到 iac 场景
```

这样做既保留了仓库级场景的批量配置能力，又实现了任务级的灵活切换。
