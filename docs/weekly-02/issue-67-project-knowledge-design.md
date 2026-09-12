# Issue #67：项目知识库体验优化

> 日期：2026-09-09
> 分支：feat/issue-67-project-knowledge（从 weekly-02 拉取）

---

## 一、项目知识使用场景

### 1.1 Agent 自动使用（核心场景）

项目知识的主要消费者是 DevOps Agent——在执行任务时通过 `prompt_builder` 自动注入上下文：

| Agent | 使用场景 | 读取的知识来源 | 目的 |
|-------|---------|--------------|------|
| VOD Agent | 分析 Issue | 项目基线（project-graph.md / project-capabilities.md） | 理解项目架构，判断 Issue 该路由给哪个 Agent |
| Issue Agent | 路由 Issue | 项目基线 + 知识沉淀 | 判断 Issue 类型和发展阶段，决定路由策略 |
| Coding Agent | 写代码 | 项目级知识沉淀 + Agent 级知识 + 成员上传文档 | 拿到项目上下文后写代码，避免重复踩坑 |
| QA Agent | 测试 | 知识沉淀（了解之前的测试结论） | 避免重复测试，参考历史测试经验 |
| SRE Agent | 运维 | 知识沉淀（了解之前的故障处理经验） | 参考历史故障处理方案 |
| Chat Agent | 对话 | 项目基线 + 成员文档 | 回答用户关于项目的问题 |

注入链路：

```
Agent 执行任务 → prompt_builder 构建 prompt
  → _build_project_info_section（基线文件摘要）
  → _build_knowledge_section（三级知识：issue + issue_agent + member_agent）
  → 成员文档摘要
→ Agent 拿到完整上下文执行任务
```

`GET /api/knowledge/context?repo=xxx&agent_type=xxx&issue_num=xxx` 端点返回合并后的注入文本，供远端 worker 使用。

### 1.2 人工使用

| 角色 | 使用场景 | 操作方式 |
|------|---------|---------|
| 开发者 | 查看项目架构 | 在知识库页面查看 project-graph.md 了解项目模块关系 |
| 开发者 | 查看历史经验 | 查看知识沉淀了解之前 Agent 发现了什么问题 |
| 开发者 | 上传设计文档 | 上传 API 设计文档、架构方案，供 Agent 后续执行时参考 |
| 新成员 | 团队知识传承 | 通过知识库了解项目历史决策和经验 |
| 项目负责人 | 审查知识质量 | 查看 Agent 沉淀的知识是否准确，决定是否接受/拒绝 |

### 1.3 知识生命周期

```
产生 → 存储 → 展示 → 注入 → 沉淀
  │       │       │       │       │
  │       │       │       │       └─ 新知识成为后续任务的上下文
  │       │       │       └─ prompt_builder 注入到 Agent prompt
  │       │       └─ 知识库页面展示给用户查看/管理
  │       └─ DB agent_shared_knowledge + OBS / 本地文件
  └─ VOD Agent 生成 / DevOps Agent 写入 / 用户手动上传
```

---

## 二、项目知识来源梳理

### 1.1 知识来源全景

项目知识库的数据来自 5 个来源，分 3 个层级：

| 层级 | 来源 | scope 值 | 产生方式 | 存储位置 | 示例 |
|------|------|---------|---------|---------|------|
| **项目基线** | VOD Agent 分析仓库 | `baseline` | Issue 打开/更新时触发 `maybe_refresh_project_info` | OBS / 本地 `project-info/` | `project-graph.md`、`project-capabilities.md`、`.metadata.json` |
| **项目级知识** | DevOps Agent 执行任务时写入 | `project` | Agent 执行 Issue 后追加到 `project-info/knowledge.md` | DB `agent_shared_knowledge` + OBS / 本地 `project-info/knowledge.md` | `- [seq 42 / agent] 全局编码规范使用 black` |
| **Issue 级知识** | DevOps Agent 执行任务时写入 | `issue` | Agent 执行 Issue 后追加到该 Issue 的 `knowledge.md` | DB `agent_shared_knowledge` + OBS / 本地 `issues/{N}/knowledge.md` | `- [seq 42 / agent] Issue #15 发现日志模块有内存泄漏` |
| **Issue+Agent 级知识** | DevOps Agent 执行任务时写入 | `issue_agent` | Agent 执行 Issue 后追加到该 Issue 的 Agent 目录 | DB `agent_shared_knowledge` + OBS / 本地 `issues/{N}/{type}/knowledge.md` | `- [seq 38 / agent] Issue #15 测试覆盖率不足` |
| **成员上传** | 用户手动上传（项目级） | `member` | 用户在知识库页面点击上传 | DB `agent_shared_knowledge` + OBS / 本地 `project-info/member/{username}/` | `design.md`、`api-spec.json` |
| **成员上传** | 用户手动上传（Agent 级） | `member` + `agent_type` | 用户上传时选择 Agent scope | DB `agent_shared_knowledge` + OBS / 本地 `project-info/agent/{type}/` | `coding-guide.md` |

### 1.2 各来源详细说明

#### 来源 1：项目基线（scope=baseline）

- **产生者**：VOD Agent（Volume of DevOps 分析引擎），底层调用 `build_project_info()`
- **产生时机**：**Issue 打开/更新时触发**（不是托管时自动产生）。`event_pipeline.py` 处理 Issue 事件时在后台线程调 `maybe_refresh_project_info()`，检查是否需要刷新基线：
  - 文件缺失（首次没有 project-graph.md / project-capabilities.md）→ 刷新
  - 远程 commit 变化（代码更新了）→ 刷新
  - 超过 7 天未更新 → 刷新
  - 降级状态且 codegraph 现在可用 → 刷新
  - 如果仓库托管后一直没人提 Issue，就不会产生基线知识
- **产生内容**：
  - `project-graph.md`：项目依赖图、模块关系、架构概览
  - `project-capabilities.md`：项目能力清单（支持什么、不支持什么）
  - `.metadata.json`：项目元信息（语言、框架、分支、最后 commit sha、更新时间）
- **降级模式**：codegraph 不可用时降级生成——仅基于 README + GitCode API 基础信息（meta.degraded=true），codegraph 恢复后下次 Issue 触发时自动升级
- **写入路径**：`{WORKSPACE_DIR}/{owner}/{repo}/project-info/`
- **DB 记录**：`agent_shared_knowledge` 表 `scope='baseline'`，含 `obs_key` + `content_hash`
- **读取方式**：`load_project_info()` 优先查 DB baseline obs_key → OBS 读取，回退本地文件

#### 来源 2：项目级知识沉淀（scope=project）

- **产生者**：DevOps Agent（coding/qa/sre 等）
- **产生时机**：Agent 执行 Issue 任务完成后，调用 `POST /api/events/{seq}/knowledge`，body 指定 `scope=project`
- **产生内容**：Agent 发现的知识点，追加到 `knowledge.md`
- **写入路径**：`{WORKSPACE_DIR}/{owner}/{repo}/project-info/knowledge.md`
- **写入方式**：`write_knowledge(scope='project')` → 追加写入，格式 `- [seq {source_seq} / agent] {content}`
- **注入范围**：全局——所有 Issue 的所有 Agent 执行时都注入
- **DB 记录**：`agent_shared_knowledge` 表 `scope='project'`，`issue_num=0`

#### 来源 3：Issue 级知识沉淀（scope=issue）

- **产生者**：DevOps Agent
- **产生时机**：Agent 执行 Issue 任务完成后，调用 `POST /api/events/{seq}/knowledge`，body 指定 `scope=issue`
- **产生内容**：Agent 发现的知识点，追加到该 Issue 的 `knowledge.md`
- **写入路径**：`{WORKSPACE_DIR}/{owner}/{repo}/issues/{issue_num}/knowledge.md`
- **写入方式**：`write_knowledge(scope='issue')` → 追加写入，格式 `- [seq {source_seq} / agent] {content}`
- **注入范围**：Issue 级——同一个 Issue 的所有 Agent 执行时注入，其他 Issue 不注入
- **DB 记录**：`agent_shared_knowledge` 表 `scope='issue'`，`issue_num` 记录来源 Issue

#### 来源 4：Issue + Agent 级知识沉淀（scope=issue_agent）

- **产生者**：DevOps Agent
- **产生时机**：Agent 执行 Issue 任务完成后，调用 `POST /api/events/{seq}/knowledge`，body 指定 `scope=issue_agent`
- **产生内容**：Agent 自己沉淀的知识，只给自己用
- **写入路径**：`{WORKSPACE_DIR}/{owner}/{repo}/issues/{issue_num}/{agent_type}/knowledge.md`
- **写入方式**：`write_knowledge(scope='issue_agent', agent_type='coding')` → 追加写入
- **注入范围**：Issue + Agent 级——只注入给同一个 Issue 的同一个 Agent 类型
- **DB 记录**：`agent_shared_knowledge` 表 `scope='issue_agent'`，含 `agent_type` + `issue_num`
- **与 scope=issue 的区别**：issue 是同一个 Issue 所有 Agent 共享；issue_agent 是某个 Agent 自己的，不给其他 Agent 看

#### 来源 5：成员上传文档（项目级，scope=member）

- **产生者**：用户手动上传
- **产生时机**：用户在知识库页面点击"上传"按钮
- **产生内容**：用户上传的 md/txt/json 文档（≤10MB）
- **写入路径**：`project-info/member/{username}/`
- **DB 记录**：`agent_shared_knowledge` 表 `scope='member'`，`agent_type` 为空
- **删除权限**：仅上传者本人可删除

#### 来源 6：成员上传文档（Agent 级，scope=member + agent_type）

- **产生者**：用户手动上传
- **产生时机**：用户上传时选择 scope=agent + 指定 agent_type
- **写入路径**：`project-info/agent/{type}/`
- **DB 记录**：`agent_shared_knowledge` 表 `scope='member'`，`agent_type` 非空
- **前端展示**：在 Agent tab 中以 `scope='member_agent'` 标签展示

### 1.3 知识注入链路

知识不是只在页面展示——它会被注入到 Agent 的 prompt 上下文中：

```
Agent 执行任务 → prompt_builder 构建 prompt
  → _build_project_info_section（基线文件摘要）
  → _build_knowledge_section（三级知识：issue + issue_agent + member_agent）
  → 成员文档摘要
→ Agent 拿到完整上下文执行任务
```

`GET /api/knowledge/context` 端点返回合并后的注入文本，供远端 worker 使用。

### 1.4 存储架构

```
agent_shared_knowledge 表
├── scope='baseline'      → VOD 基线文件（project-graph.md 等）→ 全局注入
├── scope='project'       → 项目级知识沉淀 → 全局注入（所有 Issue 所有 Agent）
├── scope='issue'         → Issue 级知识沉淀 → 同 Issue 所有 Agent 注入
├── scope='issue_agent'   → Issue+Agent 级知识沉淀 → 同 Issue 同 Agent 注入
└── scope='member'        → 成员上传
    ├── agent_type=''     → 项目级成员文档 → 全局注入
    └── agent_type!=''    → Agent 级成员文档 → 指定 Agent 注入

文件路径：
├── project-info/project-graph.md          → baseline
├── project-info/project-capabilities.md   → baseline
├── project-info/knowledge.md              → project（全局知识沉淀）
├── issues/{N}/knowledge.md                → issue（Issue 级知识沉淀）
├── issues/{N}/{agent_type}/knowledge.md   → issue_agent（Issue+Agent 级知识沉淀）
├── project-info/member/{username}/        → member（项目级成员文档）
└── project-info/agent/{type}/             → member + agent_type（Agent 级成员文档）

存储层：
├── OBS 模式（生产）：obs_key → storage.read_bytes / storage.save_bytes
└── 本地模式（开发）：file_path → open() 读写本地文件
```

### 1.5 前端展示架构

```
知识库页面（knowledge.js）— 优化后统一列表
├── 统计卡片栏（3 个卡片，点击切换筛选）
│   ├── 系统知识（N 条）→ 点击筛选来源=系统
│   ├── 用户知识（N 条）→ 点击筛选来源=用户
│   └── 未关联（N 条）→ 点击筛选关联状态=未关联
├── 筛选器
│   ├── 来源筛选：全部 / 系统 / Agent / 用户
│   ├── 关联状态筛选：全部 / 已关联 / 未关联
│   └── 搜索框：全文搜索
├── 知识列表（不分 tab，统一展示）
│   └── 每条知识卡片：
│       ├── 来源标签（系统/Agent/用户，不同颜色区分）
│       ├── 关联状态标签（全局/已关联 N 个场景/已关联 Coding Agent/未关联）
│       ├── 创建时间 + 创建者
│       ├── 内容预览（统一取前 2 行）
│       └── 操作按钮（按来源控制权限）：
│           ├── 系统/Agent 生成：查看 + 关联设置
│           └── 用户创建：查看 + 编辑 + 删除 + 关联设置
├── "新增知识"按钮 → 弹出 modal（输入文本 + 选择关联场景/Agent）
├── "上传文档"按钮 → 弹出 modal（上传文件 + 选择关联场景/Agent）
├── 查看全文（Markdown 渲染 / JSON 原文）
└── 关联设置 modal（管理知识的场景/Agent 关联，可开关 enabled）
```

---

## 三、当前问题分析

### 3.1 体验层问题

| 问题 | 描述 | 影响 |
|------|------|------|
| 搜索只搜标题 | `filterKnowledge` 只匹配 `data-name` 和 `textContent`（预览文本），不搜全文 | 用户搜不到内容里有但预览里没有的知识 |
| 预览取最后几行 | project tab 取最后 3 行，agent tab 取最后 2 行，逻辑不统一 | 用户看不到知识开头的重要摘要 |
| 无分页 | 全量渲染所有卡片 | 知识多了页面卡顿 |
| 删除用原生 confirm | `deleteKnowledgeDoc` 用 `confirm()` | 和 Issue #54 的弹出层改造不一致 |
| 上传后全量重载 | `renderKnowledge()` 重新加载整个列表 | 上传一个文件刷新所有数据 |
| Agent 类型硬编码 | 上传弹窗的 Agent 下拉是写死的 6 个 | 实际有知识的 Agent 类型可能不同 |
| 无知识来源时间线 | 知识卡片没有按时间排序的视图 | 用户看不到知识积累的过程 |

### 3.2 架构层问题

| 编号 | 问题 | 描述 | 影响 | 证据 |
|------|------|------|------|------|
| P1 | 知识沉淀是被动触发的 | 只有 `event-dialogue` 一个 skill 能写入知识，且只在用户主动对话提建议时触发。Coding/QA/SRE Agent 执行任务时踩的坑、发现的经验都不会沉淀 | Agent 执行任务的经验大量丢失，必须等用户来对话才写入 | `skills/vod/event-dialogue/SKILL.md` 第 20 行"用户提出建议/意见时" |
| P2 | issue / issue_agent 是暗知识 | 用户在知识库页面只能看到 baseline / project / member 三类。Agent 沉淀的 issue / issue_agent 级知识完全不可见 | 用户不知道 Agent 沉淀了什么，也不能管理 | `knowledge.js` 只渲染 project tab + agent tab，无 issue 级展示 |
| P3 | 知识只增不删 | issue / issue_agent 级知识在 Issue 关闭后变成死知识——没有 Agent 再读，但文件和 DB 记录一直占空间。project 级知识也是只追加不清理 | 存储无限膨胀，DB 记录无限增长 | `write_knowledge` 只追加不清理，无过期/归档机制 |
| P4 | scope 混了两个维度 | `scope` 字段既表示"谁产生的"又影响"注入范围"。`scope=issue` 字面意思是"Issue 级别"，实际含义是"Agent 执行 Issue 后产生的同 Issue 共享知识" | 用户看不懂这些值，新增 source 字段分离来源和注入范围 | `agent_shared_knowledge` 表 scope 字段注释 |
| P5 | 用户知识没有 CRUD | 不能编辑知识内容、不能直接输入文本创建知识、不能删除 Agent 沉淀的错误知识 | 用户对知识库没有完全控制权 | 只有 `POST /api/knowledge`（上传文件）+ `DEL /api/knowledge`（删除成员文档） |
| P6 | 没有场景关联知识 | 场景执行时只注入三级知识，用户不能指定"这个场景的 Coding Agent 额外读取 design.md" | 用户不能为特定场景配置专属知识注入 | `prompt_builder._build_knowledge_section` 只注入 project/issue/issue_agent/member |
| P7 | Agent 不知道该写哪个 scope | event-dialogue skill 虽然写了"按影响范围判定 scope"，但实际 9 次执行全写了 project | project 级知识混着 Issue 专属的东西，prompt 越来越长 | `xiaowu2119/hello-skill` 的 project/knowledge.md 有 9 条记录全是 scope=project |
| P8 | 没有跨 Issue 知识复用 | Issue #15 的经验只给 Issue #15 的 Agent 读。Issue #20 遇到同样的问题，Agent 不知道 Issue #15 已经踩过坑 | 类似新 Issue 的 Agent 重复踩坑 | `read_knowledge` 只读当前 issue_num 的知识 |
| P9 | 知识注入不透明 | Agent 执行任务时注入了哪些知识，用户完全不知道 | Agent 拿了错误的知识执行了错误的操作，用户看不到原因 | `prompt_builder` 注入知识后不记录引用链路 |

---

## 四、优化需求（用户确认）

### 4.0 核心设计：统一知识模型

**设计思路**：所有知识是独立的，通过关联关系控制注入范围，不再按 tab 分类。

**统一模型**：

- 知识本身没有 tab 归属——就是一条知识列表
- 关联场景 → 执行该场景时注入
- 关联 Agent type → 该类型 Agent 执行时注入
- 不关联 → 只在知识库展示，不注入到任何 Agent

**系统产生的知识写入时自动关联**：

| source | 写入时自动关联 | 用户可修改关联 |
|--------|--------------|-------------|
| `system`（baseline） | 全局（所有场景、所有 Agent） | ✅ 可改 |
| `agent`（project/issue/issue_agent） | 按当前 scope 规则自动关联 | ✅ 可改 |
| `manual`（用户创建） | 用户手动选择关联 | ✅ 可改 |

**source 字段只控制权限**：

| source | 内容编辑 | 删除 | 关联管理 |
|--------|---------|------|---------|
| `system` | ❌ 只读 | ❌ | ✅ 可改 |
| `agent` | ❌ 只读 | ❌ | ✅ 可改 |
| `manual` | ✅ 仅创建者 | ✅ 仅创建者 | ✅ 可改 |

系统产生的知识内容不可编辑（改了会和 Agent 下次写入冲突），但关联关系可改——用户可以把系统自动关联"全局"的知识改成只关联 Coding Agent。

**scope 字段向后兼容**：

写入时仍按现有 scope 规则生成关联记录，注入时改为查关联表。scope 字段保留但不再作为注入依据，逐渐废弃。

**前端——统一知识列表**：

- 不分 tab，所有知识在一个列表里
- 每条卡片显示：来源标签 + 关联状态标签 + 创建时间 + 内容预览
- 筛选器：按来源（系统/Agent/用户）、按关联状态（已关联/未关联）过滤
- 搜索框：全文搜索

---

### 4.1 需求 1：知识关联场景/Agent

**目标**：用户在知识库页面管理知识的关联关系——为每条知识设置关联哪些场景和 Agent，场景执行时自动注入关联的知识到 Agent prompt。

**当前问题**：场景执行时 `prompt_builder` 只注入三级知识（project/issue/issue_agent/member），用户不能手动指定"这条知识在执行某些场景时额外注入给 Coding Agent"。

**方案**：

参考 Multica 的 `agent_skill` 多对多关联表设计，新增关联表。

**SQLite DDL**（开发环境）：

```sql
CREATE TABLE IF NOT EXISTS scene_knowledge_ref (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id  INTEGER NOT NULL,                        -- agent_shared_knowledge.id
    scene_id      TEXT NOT NULL DEFAULT '',                 -- 场景UUID，空字符串=全部场景，禁止NULL
    agent_type    TEXT NOT NULL DEFAULT '',                 -- Agent类型，空字符串=全部Agent，禁止NULL
    enabled       INTEGER NOT NULL DEFAULT 1,               -- 1启用，0禁用
    created_by    TEXT NOT NULL DEFAULT '',                 -- 创建人
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),  -- 创建时间
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),  -- 更新时间
    updated_by    TEXT NOT NULL DEFAULT '',                 -- 最后修改人

    -- 三元组唯一约束：防止同一知识重复绑定相同(scene_id, agent_type)
    UNIQUE (knowledge_id, scene_id, agent_type)
);
```

**MySQL DDL**（生产环境，原生写，不走方言翻译层）：

```sql
CREATE TABLE IF NOT EXISTS scene_knowledge_ref (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
    knowledge_id  BIGINT UNSIGNED NOT NULL COMMENT 'agent_shared_knowledge主键',
    scene_id      VARCHAR(64) NOT NULL DEFAULT '' COMMENT '场景UUID，空字符串代表全部场景，禁止NULL',
    agent_type    VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'Agent类型，空字符串代表全部Agent，禁止NULL',
    enabled       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用，0禁用',
    created_by    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '创建人',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间，自动刷新',
    updated_by    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '最后修改人',

    UNIQUE KEY uk_knowledge_scene_agent (knowledge_id, scene_id, agent_type),
    KEY idx_query (enabled, scene_id, agent_type),

    CONSTRAINT fk_scene_knowledge_ref_knowledge_id
        FOREIGN KEY (knowledge_id) REFERENCES agent_shared_knowledge(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库-场景-Agent绑定关系';
```

**为什么 MySQL 版本不走方言翻译层**：

grape 的 `mysql_compat.py` 翻译层把 `INTEGER PRIMARY KEY AUTOINCREMENT` → `INT AUTO_INCREMENT`、`datetime('now')` → `CAST(NOW() AS CHAR)` 存为 TEXT、不保留 COMMENT 和外键。对于这张新建的核心关联表，翻译层产出不够严谨：
- `INT` 不如 `BIGINT UNSIGNED` 安全
- `TEXT DEFAULT (CAST(NOW() AS CHAR))` 不如原生 `DATETIME DEFAULT CURRENT_TIMESTAMP`
- 缺少 `ON UPDATE CURRENT_TIMESTAMP` 自动刷新
- 缺少 COMMENT、外键级联、复合索引

因此 MySQL 版本直接写原生 DDL，绕过翻译层。SQLite 版本保持 grape 现有风格（`INTEGER PRIMARY KEY AUTOINCREMENT` + `datetime('now')`）。

**业务代码必须遵守的规则**：

1. 禁止给 `scene_id`、`agent_type` 传入 NULL，全局统一存空字符串 `''`
2. 新增绑定时捕获唯一约束冲突异常，提示"该知识已绑定此场景/Agent 类型"
3. 查询生效知识时必须 `DISTINCT knowledge_id` 去重——一条知识可能命中多条 ref（精确匹配 + 全局匹配同时命中）
4. SQLite 连接已开启 `PRAGMA foreign_keys=ON`（`session_manager.py:90`），外键约束生效
5. MySQL 外键 `ON DELETE CASCADE`——知识删除时关联记录自动清理
6. `updated_at`：SQLite 版由业务代码更新，MySQL 版由 `ON UPDATE CURRENT_TIMESTAMP` 自动刷新

**标准查询 SQL**（SQLite + MySQL 通用）：

```sql
SELECT DISTINCT knowledge_id
FROM scene_knowledge_ref
WHERE enabled = 1
AND (scene_id = :sceneId OR scene_id = '')
AND (agent_type = :agentType OR agent_type = '');
```

一条知识可以有多条关联记录。`enabled` 字段控制开关——用户可以临时禁用某条关联而不删除。

**系统写入时自动生成关联记录**：

| source + scope | 自动生成的关联记录 |
|---------------|------------------|
| system + baseline | `scene_id='', agent_type='', enabled=1`（全局） |
| agent + project | `scene_id='', agent_type='', enabled=1`（全局） |
| agent + issue | `scene_id='', agent_type='', enabled=1`（全局，同 Issue 内生效由 issue_num 控制） |
| agent + issue_agent | `scene_id='', agent_type='{当前agent}', enabled=1`（指定 Agent） |

用户创建知识时不自动关联——由用户手动选择关联什么。

**管理入口在知识库页面**（不在场景编辑器）：

知识卡片上加"关联设置"按钮 → 弹出 modal：
- 关联场景：从场景列表中勾选（可多选），或选"所有场景"
- 关联 Agent：选择 Agent 类型（全局/指定 Agent，可多选）
- 每个关联可以单独开关 enabled
- 系统产生的知识也可以修改关联（比如把全局改成只关联 Coding Agent）

知识卡片上显示关联状态标签：
- "全局" / "已关联 3 个场景" / "已关联 Coding Agent" / "未关联"
- 未关联的知识不注入到任何 Agent

场景编辑器里只展示，不管理——Agent 节点卡片显示"已关联 N 条知识"，用户可点击查看列表，但增删关联回到知识库页面操作。

**执行时注入**：

`prompt_builder` 构建执行 prompt 时，查 `scene_knowledge_ref` 表：
1. `enabled = 1` AND (`scene_id = 当前场景` OR `scene_id = ''`（全局））
2. AND (`agent_type = 当前 Agent 类型` OR `agent_type = ''`（全局））
3. → 注入到对应 Agent 的 prompt

**影响范围**：
- 新增 `scene_knowledge_ref` 表
- `knowledge.py`：新增关联管理 API（`POST /api/knowledge/{id}/refs` + `GET /api/knowledge/{id}/refs` + `PUT /api/knowledge/ref/{ref_id}` + `DEL /api/knowledge/ref/{ref_id}`）
- `knowledge.js`：知识卡片加"关联设置"按钮 + 关联管理 modal + 统一列表（去掉 tab）
- `prompt_builder`：改为查 `scene_knowledge_ref` 表注入（向后兼容：无关联记录时回退到 scope 逻辑）
- `write_knowledge()`：写入时自动生成关联记录
- `scene-market.js`：Agent 节点卡片展示已关联知识数量（只读）
- `knowledge.js`：知识列表 API 返回 id + 关联状态字段

### 4.2 需求 2：知识编辑/删除/新增

**目标**：用户可以对用户创建的知识进行编辑、删除、新增。

**知识分类**：

新增 `source` 字段表示知识来源，与 `scope`（注入范围）分离：

| source 值 | 含义 | 产生方式 | 权限 |
|-----------|------|---------|------|
| `baseline` | VOD Agent 基线 | VOD Agent 自动分析生成 | 只读，不可编辑/删除 |
| `agent` | Agent 沉淀 | DevOps Agent 执行任务时自动写入 | 只读，不可编辑/删除 |
| `manual` | 用户创建 | 用户手动上传或直接输入文本 | 可编辑/删除，仅创建者本人 |

`source` 和 `scope` 的关系：

| source | scope | 含义 |
|--------|-------|------|
| `baseline` | `baseline` | VOD 基线，全局注入 |
| `agent` | `project` | Agent 沉淀，全局注入 |
| `agent` | `issue` | Agent 沉淀，同 Issue 注入 |
| `agent` | `issue_agent` | Agent 沉淀，同 Issue 同 Agent 注入 |
| `manual` | `member` | 用户创建，全局或指定 Agent 注入 |

用户创建知识时只关心 `source=manual`，选择注入范围（全局/指定 Agent）通过 `scope` + `agent_type` 控制，和现有逻辑一致。

系统生成的知识（source=baseline/agent）不可直接编辑——改了会和 Agent 下次写入冲突。如果要修正，应该重新让 Agent 分析或执行任务覆盖。

**现有数据迁移**：

从现有 `scope` 值推导 `source`：
- `scope=baseline` → `source=baseline`
- `scope=project` 或 `scope=issue` 或 `scope=issue_agent` → `source=agent`
- `scope=member` → `source=manual`

迁移方式：`ALTER TABLE agent_shared_knowledge ADD COLUMN source TEXT DEFAULT '';` 然后用 UPDATE 语句按上述规则回填。

**当前问题**：
- 只能删除成员上传的文档（`deleteKnowledgeDoc`），不能删除手动新增的知识
- 不能编辑知识内容
- 新增知识只能通过上传文件，不能直接输入文本

**方案**：

后端新增/修改 API：

| API | 方法 | 说明 | 权限 |
|-----|------|------|------|
| `POST /api/knowledge/entry` | 新增 | 直接输入文本创建知识条目（source=manual），指定 scope + agent_type | 登录用户 |
| `PUT /api/knowledge/{id}` | 编辑 | 修改知识内容，仅 source=manual 可编辑 | 仅创建者本人 |
| `DEL /api/knowledge/{id}` | 删除 | 删除 source=manual 的知识条目 | 仅创建者本人 |

source=baseline/agent 的编辑/删除请求直接返回 403。

前端：
- source=baseline/agent 的知识卡片：只显示"查看"按钮，无编辑/删除
- source=manual 的知识卡片：显示"查看" + "编辑" + "删除"按钮
- 知识库页面加"新增知识"按钮，弹出 modal 输入文本 + 选择注入范围（全局/指定 Agent）
- 编辑/删除用苹果风格 modal，不用原生 confirm

**影响范围**：
- `agent_shared_knowledge` 表：新增 `source` 字段 + 数据回填
- `knowledge.py`：新增 `POST /entry` + `PUT /{id}` + `DEL /{id}`，加 source 权限校验（baseline/agent 返回 403）
- `knowledge.js`：知识卡片按 source 显示不同按钮 + 新增知识按钮 + 编辑 modal
- `write_knowledge()`：写入时设置 source=agent
- `upload_knowledge()`：上传时设置 source=manual

### 4.3 需求 3：知识注入范围管理（统一到关联关系）

**目标**：用户通过关联关系控制知识的注入范围——关联全局则所有 Agent 注入，关联指定 Agent 则只给该 Agent 注入。不再通过修改 scope 字段控制注入范围。

**当前问题**：知识的 scope 在写入时就固定了，注入范围不可修改。现在统一为关联关系——修改关联即可改变注入范围。

**方案**：

需求 3 已经合并到需求 1 的关联管理中。用户在知识卡片的"关联设置"modal 里：
- 选"所有场景" + "所有 Agent" = 全局注入
- 选"所有场景" + "Coding Agent" = 只给 Coding Agent 注入
- 选特定场景 + 特定 Agent = 只在该场景的该 Agent 执行时注入

不再需要修改 scope 字段或移动文件——注入范围完全由 `scene_knowledge_ref` 表的关联记录控制。

**影响范围**：
- 无额外改动——需求 1 的关联管理 API 和 UI 已覆盖此需求
- `prompt_builder`：注入逻辑统一查 `scene_knowledge_ref` 表

### 4.4 三个需求的关系

```
需求 2（编辑/删除/新增 + source 字段）是基础
  └── 需求 1（关联场景/Agent）是核心——注入范围统一由关联关系控制
       └── 需求 3 已合并到需求 1——通过关联管理改变注入范围
```

**建议实现顺序**：
1. 先做需求 2（知识 CRUD + source 字段 + 数据迁移）——基础能力
2. 再做需求 1（scene_knowledge_ref 表 + 关联管理 + prompt_builder 改查关联表）——核心能力
3. 需求 3 不需要单独实现——已合并到需求 1

### 4.5 待确认点

- [x] 知识编辑后是否需要版本历史？→ **不需要，直接覆盖**
- [x] 删除 Agent 沉淀的知识是否需要确认提示？→ **需要，苹果风格 modal 确认**
- [x] 场景关联知识是否限制数量？→ **需要，每个场景每个 Agent 节点最多 20 条**
- [x] 新增知识时是否支持 Markdown 格式？→ **支持，编辑器用 textarea + Markdown 预览**
- [x] 知识绑定范围修改时，已关联该知识的场景是否需要提示？→ **需要，修改关联时提示受影响的场景数量**

---

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
| `upload_knowledge()` | `src/api/routes/knowledge.py:472` | `source='manual'`（用户上传） |
| `POST /api/knowledge/entry`（新增） | `src/api/routes/knowledge.py`（新增） | `source='manual'`（用户输入） |

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

**POST /api/knowledge/{id}/refs（新增关联）**：

```python
@router.post("/{knowledge_id}/refs")
def add_knowledge_ref(knowledge_id: int, body: KnowledgeRefBody,
                      user: dict = Depends(get_current_user)):
    """为知识新增场景/Agent 关联。"""
    try:
        with db_lock:
            db.cursor().execute(
                "INSERT INTO scene_knowledge_ref "
                "(knowledge_id, scene_id, agent_type, enabled, created_by) "
                "VALUES (?, ?, ?, 1, ?)",
                (knowledge_id, body.scene_id or "", body.agent_type or "",
                 user.get("username", ""))
            )
            db.commit()
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            raise HTTPException(status_code=409, detail={"code": 409, "message": "该知识已绑定此场景/Agent 类型"})
        raise
    return {"ok": True}
```

**PUT /api/knowledge/ref/{ref_id}（修改关联——开关 enabled）**：

```python
@router.put("/ref/{ref_id}")
def update_knowledge_ref(ref_id: int, body: KnowledgeRefUpdateBody,
                         user: dict = Depends(get_current_user)):
    """修改关联的 enabled 开关。"""
    with db_lock:
        db.cursor().execute(
            "UPDATE scene_knowledge_ref SET enabled = ?, updated_by = ? WHERE id = ?",
            (1 if body.enabled else 0, user.get("username", ""), ref_id)
        )
        db.commit()
    return {"ok": True}
```

**DEL /api/knowledge/ref/{ref_id}（删除关联）**：

```python
@router.delete("/ref/{ref_id}")
def delete_knowledge_ref(ref_id: int, user: dict = Depends(get_current_user)):
    """删除知识的某条关联。"""
    with db_lock:
        db.cursor().execute("DELETE FROM scene_knowledge_ref WHERE id = ?", (ref_id,))
        db.commit()
    return {"ok": True}
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

`GET /api/knowledge` 返回数据以 id 为主键，所有后续操作（编辑/查询详情/删除/关联管理）都以 id 为准：

```python
# 每条知识必须返回 id 主键，后续接口都以 id 操作：
{
    "id": 42,                    # 知识主键（必须返回，后续 CRUD 以此为准）
    "source": "manual",          # 新增：来源
    "scope": "member",           # 保留：注入范围（向后兼容）
    "repo_key": "gitcode:xxx/yyy",
    "agent_type": "coding",
    "issue_num": 0,
    "file_path": "project-info/member/user/xxx.md",
    "obs_key": "knowledge/xxx/yyy/member/user/xxx.md",
    "status": "accepted",
    "source_seq": 0,
    "created_by": "user1",
    "created_at": "2026-09-09 10:00:00",
    "updated_at": "2026-09-09 10:00:00",
    "content": "知识正文预览...",  # 前 500 字符
    "archived": 0,               # 新增：归档标记
    "refs": [                    # 新增：关联列表
        {"id": 1, "scene_id": "", "agent_type": "coding", "enabled": 1},
        {"id": 2, "scene_id": "uuid-xxx", "agent_type": "", "enabled": 1},
    ],
    "ref_summary": "全局",       # 新增：关联摘要（前端展示用）
    "issue_tag": "",             # 新增：Issue 标签（issue/issue_agent 级知识专用）
}
```

**所有后续接口都以 id 主键为准**：

| 接口 | 方法 | URL | 说明 |
|------|------|-----|------|
| 查询详情 | GET | `/api/knowledge/{id}` | 按 id 查询单条知识全文 |
| 编辑 | PUT | `/api/knowledge/{id}` | 按 id 编辑知识内容 |
| 删除 | DEL | `/api/knowledge/{id}` | 按 id 删除知识 |
| 查看关联 | GET | `/api/knowledge/{id}/refs` | 按 id 查询关联列表 |
| 新增关联 | POST | `/api/knowledge/{id}/refs` | 按 id 新增关联 |
| 修改关联 | PUT | `/api/knowledge/ref/{ref_id}` | 按 ref_id 修改关联开关 |
| 删除关联 | DEL | `/api/knowledge/ref/{ref_id}` | 按 ref_id 删除关联 |
| 标记归档 | PUT | `/api/knowledge/{id}/archive` | 按 id 标记/取消归档 |
| 读取全文 | GET | `/api/knowledge/content?repo=xxx&obs_key=xxx` | 保留现有（按 obs_key 读全文） |

**`ref_summary` 生成规则**：
- 有 `scene_id='' AND agent_type=''` 的关联 → "全局"
- 有特定 `scene_id` 的关联 → "已关联 N 个场景"
- 有特定 `agent_type` 的关联 → "已关联 {agent_type}"
- 无关联记录 → "未关联"

**`list_knowledge` 端点改动**（`knowledge.py:346`）：

返回数据改为统一列表格式——不再分 `project` / `agents` 两层，改为 `knowledge` 统一数组：

```python
return {
    "repo_key": repo_key,
    "knowledge": all_knowledge,  # 统一列表，每条含 id + source + refs + ref_summary
    "stats": {                   # 新增：统计数（前端统计卡片用）
        "system": sum(1 for k in all_knowledge if k["source"] == "system"),
        "agent": sum(1 for k in all_knowledge if k["source"] == "agent"),
        "manual": sum(1 for k in all_knowledge if k["source"] == "manual"),
        "unlinked": sum(1 for k in all_knowledge if not k.get("refs")),
    },
}
```

前端 `_flattenList` 简化为直接用 `d.knowledge` + `d.stats`。

---

### 6.10 前端 knowledge.js 改动方案——Vue 3 实现

**当前 knowledge.js 结构**（340 行）：纯原生 JS，项目 tab / Agent tab 两 tab 切换 + 搜索过滤 + 查看全文 + 上传文档 + 删除文档。

**改动方案**：知识库页面改用 Vue 3 CDN 方式实现，不影响其他页面。

**引入 Vue 3**：

`index.html` 新增 Vue 3 CDN（全局构建），放在其他 JS 之前：

```html
<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
```

**Vue 应用结构**：

`renderKnowledge()` 不再手动操作 DOM，改为创建 Vue 应用挂载到 `pageContent`：

```javascript
function renderKnowledge() {
  const el = document.getElementById('pageContent');
  if (!el) return;

  const app = Vue.createApp({
    data() {
      return {
        list: [],           // 知识列表（统一，不分 tab）
        loading: true,
        filter: { source: '', refStatus: '', query: '' },
        stats: { system: 0, agent: 0, user: 0, unlinked: 0 },
        showArchived: false,
        // modals
        editModal: null,    // 编辑的知识对象
        refModal: null,     // 关联设置的知识对象
        entryModal: false,  // 新增知识 modal
        uploadModal: false, // 上传 modal
        _vueApp: null,      // Vue 实例引用
      };
    },
    computed: {
      filteredList() {
        let result = this.list;
        // 来源筛选
        if (this.filter.source === 'system') {
          result = result.filter(k => k.source === 'system' || k.source === 'agent');
        } else if (this.filter.source === 'manual') {
          result = result.filter(k => k.source === 'manual');
        }
        // 关联状态筛选
        if (this.filter.refStatus === 'linked') {
          result = result.filter(k => k.refs && k.refs.length > 0);
        } else if (this.filter.refStatus === 'unlinked') {
          result = result.filter(k => !k.refs || k.refs.length === 0);
        }
        // 搜索
        if (this.filter.query) {
          const q = this.filter.query.toLowerCase();
          result = result.filter(k =>
            (k.content || '').toLowerCase().includes(q) ||
            (k.path || '').toLowerCase().includes(q) ||
            (k.created_by || '').toLowerCase().includes(q)
          );
        }
        return result;
      },
    },
    async mounted() {
      await this.loadList();
    },
    methods: {
      t(key, params) {
        return I18N.t('knowledge.' + key, params || {});
      },
      currentRepo() {
        try {
          const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
          return c ? (c.fullName || '') : '';
        } catch { return ''; }
      },
      async loadList() {
        this.loading = true;
        try {
          const repo = this.currentRepo();
          if (!repo) { this.loading = false; return; }
          const d = await API.get('/knowledge?repo=' + encodeURIComponent(repo));
          if (window._currentView !== 'knowledge') return;  // stale guard
          this.list = this._flattenList(d);
          this._updateStats();
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
          if (window._currentView !== 'knowledge') return;
          this.loading = false;
        } finally {
          if (window._currentView === 'knowledge') this.loading = false;
        }
      },
      _flattenList(d) {
        // 把 project + agents + issue 级知识合并成统一列表
        const list = [];
        (d.project.files || []).forEach(f => list.push({...f, source: 'system', scope: 'baseline'}));
        (d.project.knowledge || []).forEach(k => list.push({...k, source: k.source || 'agent'}));
        (d.project.member_docs || []).forEach(doc => list.push({...doc, source: 'manual', scope: 'member'}));
        (d.issue_knowledge || []).forEach(k => list.push({...k, source: k.source || 'agent'}));
        Object.entries(d.agents || {}).forEach(([type, entries]) => {
          entries.forEach(e => list.push({...e, source: e.source || 'agent', agent_type: type}));
        });
        return list;
      },
      _updateStats() {
        this.stats.system = this.list.filter(k => k.source === 'system').length;
        this.stats.agent = this.list.filter(k => k.source === 'agent').length;
        this.stats.user = this.list.filter(k => k.source === 'manual').length;
        this.stats.unlinked = this.list.filter(k => !k.refs || k.refs.length === 0).length;
      },
      // 统计卡片点击筛选
      filterByStats(type) {
        if (type === 'system') this.filter.source = 'system';
        else if (type === 'user') this.filter.source = 'manual';
        else if (type === 'unlinked') this.filter.refStatus = 'unlinked';
      },
      // 查看全文
      viewContent(k) {
        loadKnowledgeContent(k.obs_key || k.path);
      },
      // 编辑
      editKnowledge(k) {
        this.editModal = {...k};
      },
      async saveEdit() {
        try {
          await API.put('/knowledge/' + this.editModal.id, { content: this.editModal.content });
          app.showToast(this.t('knowledge_updated'));
          this.editModal = null;
          await this.loadList();
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },
      // 删除——苹果风格 modal 确认
      async deleteKnowledge(k) {
        if (!await this._confirmModal(this.t('delete_confirm'))) return;
        try {
          await API.del('/knowledge/' + k.id);
          app.showToast(this.t('knowledge_deleted'));
          await this.loadList();
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },
      // 关联设置
      async openRefModal(k) {
        try {
          const d = await API.get('/knowledge/' + k.id + '/refs');
          this.refModal = { knowledge: k, refs: d.refs || [], newScene: '', newAgent: '' };
        } catch (e) {
          app.showToast(e.message);
        }
      },
      async addRef() {
        const sceneId = this.refModal.newScene || '';
        const agentType = this.refModal.newAgent || '';
        try {
          await API.post('/knowledge/' + this.refModal.knowledge.id + '/refs', {
            scene_id: sceneId, agent_type: agentType
          });
          const d = await API.get('/knowledge/' + this.refModal.knowledge.id + '/refs');
          this.refModal.refs = d.refs || [];
          this.refModal.newScene = '';
          this.refModal.newAgent = '';
          app.showToast(this.t('ref_add'));
        } catch (e) {
          if (e.detail && e.detail.message && e.detail.message.includes('已绑定')) {
            app.showToast(this.t('ref_already_bound'));
          } else {
            app.showToast(e.message);
          }
        }
      },
      async toggleRef(ref) {
        try {
          await API.put('/knowledge/ref/' + ref.id, { enabled: !ref.enabled });
          ref.enabled = !ref.enabled;
        } catch (e) {
          app.showToast(e.message);
        }
      },
      async deleteRef(ref) {
        // 提示受影响场景
        const affected = this.refModal.refs.filter(r => r.enabled && r.id !== ref.id).length;
        if (!await this._confirmModal(
          this.t('ref_delete_confirm') + (affected > 0 ? '（会影响 ' + affected + ' 个场景）' : '')
        )) return;
        try {
          await API.del('/knowledge/ref/' + ref.id);
          this.refModal.refs = this.refModal.refs.filter(r => r.id !== ref.id);
          app.showToast(this.t('ref_delete'));
        } catch (e) {
          app.showToast(e.message);
        }
      },
      // 新增知识
      async createEntry() {
        const content = this.entryModal.content || '';
        if (!content.trim()) { app.showToast(this.t('entry_content')); return; }
        try {
          await API.post('/knowledge/entry', {
            repo: this.currentRepo(),
            content: content,
            agent_type: this.entryModal.agentType || ''
          });
          app.showToast(this.t('knowledge_created'));
          this.entryModal = false;
          await this.loadList();
        } catch (e) {
          app.showToast(e.message);
        }
      },
      // 苹果风格确认 modal
      _confirmModal(message) {
        return new Promise((resolve) => {
          // 复用 skills.js 的苹果风格 modal 组件
          _showAppleConfirm(message, () => resolve(true), () => resolve(false));
        });
      },
    },
    template: `
      <div class="knowledge-page">
        <!-- 统计卡片栏 -->
        <div class="stats-bar" v-if="!loading">
          <div class="stat-card" @click="filterByStats('system')">
            <span class="stat-num">{{ stats.system + stats.agent }}</span>
            <span class="stat-label">{{ t('stats_system') }}</span>
          </div>
          <div class="stat-card" @click="filterByStats('user')">
            <span class="stat-num">{{ stats.user }}</span>
            <span class="stat-label">{{ t('stats_user') }}</span>
          </div>
          <div class="stat-card" @click="filterByStats('unlinked')">
            <span class="stat-num">{{ stats.unlinked }}</span>
            <span class="stat-label">{{ t('stats_unlinked') }}</span>
          </div>
        </div>
        <!-- 筛选器 -->
        <div class="filter-bar" v-if="!loading">
          <select v-model="filter.source">
            <option value="">{{ t('filter_source_all') }}</option>
            <option value="system">{{ t('source_system') }}</option>
            <option value="manual">{{ t('source_manual') }}</option>
          </select>
          <select v-model="filter.refStatus">
            <option value="">{{ t('filter_ref_all') }}</option>
            <option value="linked">{{ t('filter_ref_linked') }}</option>
            <option value="unlinked">{{ t('filter_ref_unlinked') }}</option>
          </select>
          <input v-model="filter.query" :placeholder="t('search_placeholder')">
          <button @click="entryModal = { content: '', agentType: '' }">{{ t('entry_create') }}</button>
          <button @click="uploadModal = true">{{ t('upload_btn') }}</button>
        </div>
        <!-- 知识列表 -->
        <div v-if="loading" class="loading">{{ t('loading') }}</div>
        <div v-else-if="filteredList.length === 0" class="empty">{{ t('empty') }}</div>
        <div v-else class="knowledge-list">
          <div v-for="k in filteredList" :key="k.id" class="cd skill-card knowledge-card"
               :class="{ archived: k.archived }">
            <!-- 卡片头部 -->
            <div class="card-header">
              <span class="source-tag" :class="'source-' + k.source">{{ t('source_' + k.source) }}</span>
              <span class="ref-tag" v-if="k.ref_summary">{{ k.ref_summary }}</span>
              <span class="ref-tag unlinked" v-else>{{ t('ref_unlinked') }}</span>
              <span class="issue-tag" v-if="k.issue_tag">{{ k.issue_tag }}</span>
              <span class="archived-tag" v-if="k.archived">{{ t('archived') }}</span>
            </div>
            <!-- 文件过大提示 -->
            <div class="size-warning" v-if="k.scope === 'project' && k.content && k.content.length > 10240">
              ⚠ {{ t('file_too_large', { size: (k.content.length / 1024).toFixed(1) }) }}
            </div>
            <!-- 内容预览 -->
            <div class="card-content">{{ (k.content || '').substring(0, 200) }}</div>
            <!-- 卡片底部 -->
            <div class="card-footer">
              <span class="card-meta">{{ k.created_by }} · {{ (k.created_at || '').substring(0, 16) }}</span>
              <div class="card-actions">
                <button class="bt bt-o bt-s" @click="viewContent(k)">{{ t('view') }}</button>
                <button v-if="k.source === 'manual' && !k.archived" class="bt bt-o bt-s" @click="editKnowledge(k)">{{ t('edit_save') }}</button>
                <button v-if="k.source === 'manual' && !k.archived" class="bt bt-o bt-s danger" @click="deleteKnowledge(k)">{{ t('delete') }}</button>
                <button v-if="!k.archived" class="bt bt-o bt-s" @click="openRefModal(k)">{{ t('ref_settings') }}</button>
              </div>
            </div>
          </div>
        </div>
        <!-- 编辑 modal -->
        <div v-if="editModal" class="modal-overlay" @click.self="editModal = null">
          <div class="modal cd">
            <h3>{{ t('edit_title') }}</h3>
            <textarea v-model="editModal.content" rows="12" class="edit-textarea"></textarea>
            <div class="modal-actions">
              <button @click="editModal = null">{{ t('cancel') }}</button>
              <button class="bt bt-p" @click="saveEdit">{{ t('edit_save') }}</button>
            </div>
          </div>
        </div>
        <!-- 关联设置 modal -->
        <div v-if="refModal" class="modal-overlay" @click.self="refModal = null">
          <div class="modal cd">
            <h3>{{ t('ref_modal_title') }}</h3>
            <!-- 新增关联 -->
            <div class="ref-add-row">
              <input v-model="refModal.newScene" :placeholder="t('ref_scene')" class="ref-input">
              <select v-model="refModal.newAgent" class="ref-input">
                <option value="">{{ t('ref_agent_all') }}</option>
                <option value="vod">VOD</option>
                <option value="coding">Coding</option>
                <option value="qa">QA</option>
                <option value="sre">SRE</option>
                <option value="chat">Chat</option>
              </select>
              <button @click="addRef">{{ t('ref_add') }}</button>
            </div>
            <!-- 已有关联列表 -->
            <div class="ref-list">
              <div v-for="ref in refModal.refs" :key="ref.id" class="ref-item">
                <span>{{ ref.scene_id || t('ref_scene_all') }} · {{ ref.agent_type || t('ref_agent_all') }}</span>
                <label class="switch">
                  <input type="checkbox" :checked="ref.enabled" @change="toggleRef(ref)">
                </label>
                <button @click="deleteRef(ref)" class="danger">{{ t('ref_delete') }}</button>
              </div>
            </div>
            <div class="modal-actions">
              <button @click="refModal = null">{{ t('cancel') }}</button>
            </div>
          </div>
        </div>
        <!-- 新增知识 modal -->
        <div v-if="entryModal" class="modal-overlay" @click.self="entryModal = false">
          <div class="modal cd">
            <h3>{{ t('entry_title') }}</h3>
            <textarea v-model="entryModal.content" rows="10" :placeholder="t('entry_content')" class="edit-textarea"></textarea>
            <select v-model="entryModal.agentType" class="ref-input">
              <option value="">{{ t('ref_agent_all') }}</option>
              <option value="vod">VOD</option>
              <option value="coding">Coding</option>
              <option value="qa">QA</option>
              <option value="sre">SRE</option>
              <option value="chat">Chat</option>
            </select>
            <div class="modal-actions">
              <button @click="entryModal = false">{{ t('cancel') }}</button>
              <button class="bt bt-p" @click="createEntry">{{ t('entry_create') }}</button>
            </div>
          </div>
        </div>
        <!-- 上传 modal（保留现有上传逻辑，加关联选择） -->
        <!-- ... -->
      </div>
    `,
  });

  app.mount(el);
}
```

**Vue 带来的好处**：
- 不用手动操作 DOM——数据变了视图自动更新
- 筛选/搜索响应式——`filter` 变了 `filteredList` computed 自动重算
- 不用 stale guard 检查 DOM——Vue 组件卸载时自动清理（用户切走时 `pageContent.innerHTML` 被替换，Vue 应用自动卸载）
- 组件化——modal 通过 `v-if` 控制显示隐藏，不用手动 `appendChild`/`remove`

**和现有代码的兼容**：
- `renderKnowledge()` 仍然是全局函数，`app.nav('knowledge')` 调用它
- `loadKnowledgeContent()` 保留原生 JS（查看全文页面不需要 Vue）
- `API` 对象继续用现有的 `api.js`
- `I18N` 继续用现有的 `i18n.js`
- `app.showToast` 继续用现有的 `app.js`
- `_showAppleConfirm` 复用 skills.js 的苹果风格 modal 组件

**Vue 应用生命周期管理**：
- `renderKnowledge()` 创建 Vue 应用并 mount 到 `pageContent`
- 用户切走时 `app.nav()` 替换 `pageContent.innerHTML`——Vue 应用自动卸载
- 切回来时重新调 `renderKnowledge()` 重新创建

**CSS 样式**：

新增 `app.css` 里知识库页面的 Vue 组件样式：

```css
/* 统计卡片栏 */
.knowledge-page .stats-bar { display: flex; gap: 12px; margin-bottom: 16px; }
.knowledge-page .stat-card {
  flex: 1; padding: 16px; border-radius: 12px; background: var(--c);
  cursor: pointer; transition: transform 0.2s;
  display: flex; flex-direction: column; align-items: center;
}
.knowledge-page .stat-card:hover { transform: translateY(-2px); }
.knowledge-page .stat-num { font-size: 24px; font-weight: 700; color: var(--t); }
.knowledge-page .stat-label { font-size: 11px; color: var(--t3); margin-top: 4px; }

/* 来源标签颜色 */
.source-tag { font-size: 10px; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
.source-system { background: rgba(0,113,227,0.1); color: #0071e3; }
.source-agent { background: rgba(255,149,0,0.1); color: #ff9500; }
.source-manual { background: rgba(52,199,89,0.1); color: #34c759; }

/* 关联状态标签 */
.ref-tag { font-size: 10px; padding: 2px 8px; border-radius: 4px; background: rgba(0,113,227,0.08); color: var(--t2); }
.ref-tag.unlinked { background: rgba(142,142,147,0.1); color: var(--t3); }

/* 归档标签 */
.archived-tag { font-size: 10px; padding: 2px 8px; border-radius: 4px; background: rgba(142,142,147,0.15); color: var(--t3); }

/* 文件过大提示 */
.size-warning { font-size: 10px; color: var(--or); padding: 4px 0; }

/* modal */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 999; }
.modal { width: 500px; max-width: 90vw; padding: 24px; border-radius: 14px; background: var(--c); }
.edit-textarea { width: 100%; min-height: 200px; padding: 12px; border: 1px solid var(--b); border-radius: 8px; font-size: 13px; font-family: monospace; }
```

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
  "ref_global": "全局",
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
  "edit_save": "保存",
  "entry_title": "新增知识",
  "entry_content": "输入知识内容（支持 Markdown）",
  "entry_create": "创建",
  "knowledge_deleted": "知识已删除",
  "knowledge_updated": "知识已更新",
  "knowledge_created": "知识已创建",
  "delete_confirm": "确认删除此知识？关联关系将一并删除。",
  "system_readonly": "系统生成的知识不可编辑"
}
```

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

### 6.16 全文搜索实现

前端实现——从 `GET /api/knowledge` 返回的列表数据里搜索。

每条知识返回 `content` 字段（前 500 字符的预览文本）。搜索时匹配 `content` + `file_path` + `created_by`：

```javascript
function _filterKnowledge(list, filterState) {
    let result = list;
    // 来源筛选
    if (filterState.source) {
        if (filterState.source === 'system') {
            result = result.filter(k => k.source === 'system' || k.source === 'agent');
        } else if (filterState.source === 'manual') {
            result = result.filter(k => k.source === 'manual');
        }
    }
    // 关联状态筛选
    if (filterState.refStatus === 'linked') {
        result = result.filter(k => k.refs && k.refs.length > 0);
    } else if (filterState.refStatus === 'unlinked') {
        result = result.filter(k => !k.refs || k.refs.length === 0);
    }
    // 搜索
    if (filterState.query) {
        const q = filterState.query.toLowerCase();
        result = result.filter(k =>
            (k.content || '').toLowerCase().includes(q) ||
            (k.path || '').toLowerCase().includes(q) ||
            (k.created_by || '').toLowerCase().includes(q)
        );
    }
    return result;
}
```

后端 `GET /api/knowledge` 的 `_read_entry_content` 已经返回了 content 预览，前端直接搜索即可。不需要后端全文搜索接口——数据量不大（几百条），前端过滤足够。

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

和上传文件一样落在 `project-info/member/{username}/` 目录下。区别是上传文件用原始文件名，手动创建用时间戳命名（因为没有文件名）：

```python
import time, uuid
filename = f"entry_{int(time.time())}_{uuid.uuid4().hex[:6]}.md"
member_dir = os.path.join(base, "project-info", "member", str(user.get("username", "user")))
file_path = os.path.join(member_dir, filename)
```

OBS 模式下 obs_key 格式：`knowledge/{owner}/{repo}/member/{username}/{filename}`

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
- 知识库页面仍可查看，卡片标记"已归档"标签
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

**前端展示**：

归档/过时的知识卡片：
- 显示"已归档"标签（灰色）
- 操作按钮只有"查看"，无编辑/删除/关联设置
- 默认不显示归档知识——筛选器加"显示归档"开关

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

#### 6.23.3 upload_knowledge 不存在重复记录问题

`upload_knowledge`（`knowledge.py:472`）上传文件时已有重名检查（第 514-535 行）——同名文件自动加时间戳后缀（`design-1234567890.md`），每次上传文件名不同，DB 记录的 `file_path` 天然不同，不会产生重复记录。

不需要改动 `upload_knowledge` 的 DB 写入逻辑。

仅需要补充：上传时设置 `source='manual'`（6.1 已写）。

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

    # 查关联
    with db_lock:
        refs = db.cursor().execute(
            "SELECT id, scene_id, agent_type, enabled, created_by, created_at "
            "FROM scene_knowledge_ref WHERE knowledge_id=? ORDER BY id",
            (knowledge_id,)
        ).fetchall()

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
        "refs": [...],
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

  // 卸载旧的 Vue 应用（如果存在）
  if (_knowledgeVueApp) {
    _knowledgeVueApp.unmount();
    _knowledgeVueApp = null;
  }

  _knowledgeVueApp = Vue.createApp({ ... });
  _knowledgeVueApp.mount(el);
}
```

`app.js` 的 `nav()` 函数里，切走 knowledge 时调卸载：

```javascript
// nav() 里 view !== 'knowledge' 时
if (window._knowledgeVueApp) {
  window._knowledgeVueApp.unmount();
  window._knowledgeVueApp = null;
}
```

#### 6.24.2 _get_knowledge_by_id 函数实现

```python
def _get_knowledge_by_id(knowledge_id: int) -> dict | None:
    """按 id 查询单条知识记录。"""
    from src.session_manager import db, db_lock
    with db_lock:
        row = db.cursor().execute(
            "SELECT id, scope, repo_key, agent_type, issue_num, file_path, "
            "status, source_seq, created_by, created_at, obs_key, content_hash, "
            "updated_at, source, archived "
            "FROM agent_shared_knowledge WHERE id = ?",
            (knowledge_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "scope": row[1], "repo_key": row[2],
        "agent_type": row[3] or "", "issue_num": row[4] or 0,
        "file_path": row[5], "status": row[6] or "accepted",
        "source_seq": row[7] or 0, "created_by": row[8] or "",
        "created_at": row[9] or "", "obs_key": row[10] or "",
        "content_hash": row[11] or "", "updated_at": row[12] or "",
        "source": row[13] or "", "archived": row[14] or 0,
    }
```

#### 6.24.3 Pydantic Body 模型定义

在 `knowledge.py` 路由文件顶部定义：

```python
from pydantic import BaseModel

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

FastAPI 路由匹配按声明顺序——先声明的先匹配。把固定路径路由放在 `{id}` 路由之前：

```python
# 固定路径——必须在 /{id} 之前声明
@router.get("", ...)                    # GET /api/knowledge（列表）
@router.post("", ...)                   # POST /api/knowledge（上传）
@router.post("/entry", ...)             # POST /api/knowledge/entry
@router.get("/content", ...)            # GET /api/knowledge/content
@router.get("/context", ...)            # GET /api/knowledge/context
@router.delete("", ...)                 # DEL /api/knowledge?repo=xxx&file=xxx（现有，保留）

# {id} 路由——放在固定路径之后
@router.get("/{knowledge_id}", ...)     # GET /api/knowledge/{id}
@router.put("/{knowledge_id}", ...)     # PUT /api/knowledge/{id}
@router.delete("/{knowledge_id}", ...)  # DEL /api/knowledge/{id}
@router.put("/{knowledge_id}/archive", ...)  # PUT /api/knowledge/{id}/archive
@router.get("/{knowledge_id}/refs", ...)     # GET /api/knowledge/{id}/refs
@router.post("/{knowledge_id}/refs", ...)    # POST /api/knowledge/{id}/refs

# {ref_id} 路由
@router.put("/ref/{ref_id}", ...)       # PUT /api/knowledge/ref/{ref_id}
@router.delete("/ref/{ref_id}", ...)    # DEL /api/knowledge/ref/{ref_id}
```

`content` / `context` / `entry` / `ref` 是固定字符串，不会被当作 `{knowledge_id}` 匹配——因为固定路径在 `{id}` 之前声明，FastAPI 优先匹配固定路径。

现有 `DEL /api/knowledge?repo=xxx&file=xxx` 和新增 `DEL /api/knowledge/{id}` 不冲突——前者没有 path param，后者有 `{id}`。

#### 6.24.5 Vue _flattenList 和 API 返回格式对齐

6.9 改了返回格式为统一列表 `d.knowledge` + `d.stats`。6.10 Vue 代码的 `_flattenList` 简化：

```javascript
_loadData(d) {
    // 6.9 改后 API 返回统一格式
    this.list = d.knowledge || [];
    this.stats = d.stats || { system: 0, agent: 0, manual: 0, unlinked: 0 };
},
```

不再需要 `_flattenList`——后端已经返回统一列表，前端直接用。

#### 6.24.6 苹果风格确认 modal 提取为全局函数

skills.js 里的苹果风格 modal 是 `deleteSkill` 函数内联实现的，没有提取成全局函数。knowledge.js 用不了。

提取到 `app.js` 作为全局函数：

```javascript
// app.js 新增
function _showAppleConfirm(message, onConfirm, onCancel) {
    var modal = document.createElement('div');
    modal.id = 'appleConfirmModal';
    modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center';
    modal.innerHTML = '<div class="cd" style="width:360px;padding:24px;border-radius:14px;text-align:center">' +
        '<p style="font-size:14px;color:var(--t);margin-bottom:20px;line-height:1.5">' + message + '</p>' +
        '<div style="display:flex;gap:10px">' +
        '<button id="appleConfirmCancel" style="flex:1;padding:10px 16px;font-size:13px;border:1px solid #d1d1d6;border-radius:10px;background:transparent;color:#1d1d1f;cursor:pointer">' + (I18N.t('common.cancel') || '取消') + '</button>' +
        '<button id="appleConfirmOk" style="flex:1;padding:10px 16px;font-size:13px;border:none;border-radius:10px;background:#ff3b30;color:#fff;cursor:pointer">' + (I18N.t('common.confirm') || '确认') + '</button>' +
        '</div></div>';
    document.body.appendChild(modal);
    modal.onclick = function(e) { if (e.target === modal) { modal.remove(); if (onCancel) onCancel(); } };
    document.getElementById('appleConfirmOk').onclick = function() { modal.remove(); if (onConfirm) onConfirm(); };
    document.getElementById('appleConfirmCancel').onclick = function() { modal.remove(); if (onCancel) onCancel(); };
}
```

knowledge.js 的 Vue 方法里调用：

```javascript
_confirmModal(message) {
    return new Promise((resolve) => {
        _showAppleConfirm(message, () => resolve(true), () => resolve(false));
    });
},
```

skills.js 的 `deleteSkill` / `convertSkill` 也改为调用 `_showAppleConfirm`，去掉内联 modal 代码。

#### 6.24.7 MySQL 数据迁移去重 SQL

MySQL 不允许直接在子查询里引用要删除的表。用临时表写法：

```sql
-- MySQL 版本：先查要保留的 id 到临时表，再删
CREATE TEMPORARY TABLE _keep_ids AS
SELECT MAX(id) AS keep_id FROM agent_shared_knowledge
GROUP BY repo_key, scope, agent_type, issue_num;

DELETE FROM agent_shared_knowledge
WHERE id NOT IN (SELECT keep_id FROM _keep_ids);

DROP TEMPORARY TABLE _keep_ids;
```

SQLite 版本保持原来的子查询写法（SQLite 支持）。

#### 6.24.8 upload_knowledge 和 build_project_info 设置 source

**upload_knowledge**（`knowledge.py:585`）——INSERT 语句加 source：

```python
# 原来
c.execute(
    "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("member", repo_key, agent_type, 0, rel, obs_key, content_hash, "accepted", 0, ...)
)

# 改为（加 source）
c.execute(
    "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
    ("member", repo_key, agent_type, 0, rel, obs_key, content_hash, "accepted", 0, ..., "manual")
)
```

**build_project_info**（`builder.py`）——写 baseline 到 DB 时设置 source=system。这个写入在 `knowledge.py` 的 `_sync_baseline_to_db` 或类似函数里，找到 INSERT baseline 的地方加 source='system'。

#### 6.24.9 事件详情页知识引用链路前端展示

后端记录了 `injected_ids` 到线程消息（6.19）。前端事件详情页（events.js）展示：

```javascript
// events.js 渲染线程消息时判断
if (msg.content.startsWith('📌 本次注入知识：')) {
    // 解析知识 ID 列表
    var idsStr = msg.content.replace('📌 本次注入知识：', '');
    var ids = idsStr.split(', ').map(function(s) { return parseInt(s); });

    // 渲染为可点击的知识引用标签
    html += '<div class="knowledge-ref-trace">' +
        '<span class="ref-trace-label">📌 本次注入 ' + ids.length + ' 条知识</span>' +
        '<button onclick="showInjectedKnowledge(' + JSON.stringify(ids) + ')">查看详情</button>' +
        '</div>';
}

// 点击查看详情——调 API 逐条查询知识标题
function showInjectedKnowledge(ids) {
    // 调 GET /api/knowledge/{id} 查询每条知识的标题和预览
    // 展示在 modal 里
}
```

i18n 新增：
```json
{
  "injected_knowledge_count": "本次注入 {count} 条知识",
  "view_injected_detail": "查看详情"
}
```

---

## 五、Multica 对比分析

### 5.1 Multica 的知识/记忆体系

Multica 没有传统意义上的"知识库"概念，而是用三种独立机制：

| 机制 | 定位 | 用户可见 | 跨 Agent | 生命周期 |
|------|------|---------|---------|---------|
| Skill 绑定 | Agent 带什么知识执行任务 | ✅ 用户管理 | ❌ 按 Agent 绑定 | 用户控制 |
| Source Context | 跨 Issue 引用上下文快照 | ✅ 用户主动引用 | ❌ 仅目标 Issue | 30 天过期自动清理 |
| Agent Memory | Agent 跨任务记住东西 | ❌ 不可见 | ❌ 按 (agent, profile) 隔离 | 持久存活 |

### 5.2 关键设计差异

| 维度 | grape | Multica |
|------|-------|---------|
| 知识注入方式 | 系统自动从知识库注入三级知识 | Skill 绑定 + 用户主动引用 Source Context |
| 知识来源 | 只有 event-dialogue 对话时写入 | Skill 是用户/平台维护的，Memory 是 Agent 自动写的 |
| 用户控制注入 | 用户不能控制注入哪些知识 | 用户通过 Skill 绑定和 Source Context 精确控制 |
| Agent 自主沉淀 | 只有 event-dialogue 能写 | Agent 通过 Memory 自动记（用户不可见） |
| 跨 Agent 知识传播 | issue 级知识文件（用户不可见） | Squad 对话评论传递（用户可见） |
| 知识生命周期 | 无——只增不删 | Source Context 30 天过期 + 状态机管理 |
| 知识可见性 | issue/issue_agent 级知识用户看不到 | 所有知识对用户可见（Memory 除外） |

### 5.3 给 grape 的启发

**启发 1：知识注入从"系统自动全注入"改为"用户可控注入"**

Multica 用户通过 Skill 绑定 + Source Context 引用精确控制 Agent 带什么知识。grape 是系统自动把 project/issue/issue_agent/member 全注入，用户不能控制。

→ 对应需求 1（场景关联知识）：让用户在场景编辑器里为每个 Agent 节点选择注入哪些知识。三级自动注入保留作为默认，场景关联知识作为用户主动追加。

**启发 2：用户知识需要 CRUD + 可见性**

Multica 的 Skill 用户可以创建、编辑、删除、绑定。grape 的知识用户只能看不能改，issue/issue_agent 级知识连看都看不到。

→ 对应需求 2（知识 CRUD）+ 需求 3（绑定范围）：用户创建的知识支持编辑/删除/新增。issue/issue_agent 级知识在知识库页面展示出来——至少能看到 Agent 沉淀了什么。source 字段分离来源控制权限。

**启发 3：知识需要生命周期管理**

Multica 的 Source Context 有状态机（pending → attached → abandoned）+ 30 天过期自动清理。Agent Memory 按 Agent 隔离不无限膨胀。grape 的知识只增不删，Issue 关闭后变死知识。

→ 新增需求 4（知识生命周期管理）：
- Issue 关闭后 issue/issue_agent 级知识标记为"归档"——不再注入但保留可查
- 加过期清理机制——超过 N 天未被引用的归档知识自动清理
- project 级知识加管理界面——用户能标记过时知识不再注入

### 5.4 核心方向

从"系统自动管理的暗知识库"改为"用户可控、可见、可管理的知识体系"。

