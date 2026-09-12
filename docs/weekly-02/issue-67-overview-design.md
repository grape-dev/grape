# Issue #67：项目知识库优化 — 概要设计

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


---

## 五、交互流程

### 5.1 用户进入知识库
用户点击侧边栏「知识库」→ app.nav('knowledge') → renderKnowledge() 创建 Vue 应用 → API.get('/knowledge?repo=xxx') 获取统一列表+统计数 → 渲染统计卡片栏+筛选器+知识列表

### 5.2 统计卡片筛选
点击「系统知识」→ filter.source='system' → 列表联动。点击「用户知识」→ filter.source='manual'。点击「未关联」→ filter.refStatus='unlinked'。

### 5.3 查看全文
点击「查看」→ loadKnowledgeContent(obs_key) → API.get('/knowledge/content') → 渲染 Markdown/JSON → 返回列表

### 5.4 编辑知识（source=manual）
点击「编辑」→ modal → 修改内容 → 保存 → API.put('/knowledge/{id}') → 校验 manual+创建者 → 覆盖文件+OBS+hash → 刷新

### 5.5 删除知识（source=manual）
点击「删除」→ 苹果确认 modal → 确认 → API.del('/knowledge/{id}') → 校验 manual+创建者 → 删 OBS+文件+DB+关联 → 刷新

### 5.6 新增知识
点击「新增知识」→ modal → 输入内容+选 Agent → 创建 → API.post('/knowledge/entry') → 写文件+DB → 刷新 → 关联设置手动选择

### 5.7 关联设置
点击「关联设置」→ modal → API.get('/knowledge/{id}/refs') → 新增关联（校验≤20+唯一）→ 开关 enabled → 删除关联（提示受影响场景数）

### 5.8 场景执行时注入
场景执行 → prompt_builder._build_knowledge_section(repo_key, agent_type, scene_id) → 查 scene_knowledge_ref（enabled=1 AND scene_id 匹配 AND agent_type 匹配）→ DISTINCT 去重 → 过滤 archived=0 → 基线始终注入 → 注入 prompt+记录 injected_ids → 线程留痕

### 5.9 Issue 关闭归档
Issue 关闭 → event_pipeline → _archive_issue_knowledge(repo_key, issue_num) → archived=1+关联 enabled=0 → 不再注入但可查看

### 5.10 project 级文件过大整理
加载 project 知识 → content.length>10KB → 卡片显示⚠提示 → 点击整理 → 编辑 modal → 删除不需要的行 → 保存覆盖

---

## 六、表设计

### 6.1 agent_shared_knowledge（现有表+新增字段）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 主键 |
| scope | TEXT NOT NULL | baseline/project/issue/issue_agent/member |
| repo_key | TEXT NOT NULL | 仓库标识 |
| agent_type | TEXT DEFAULT '' | Agent 类型 |
| issue_num | INTEGER DEFAULT 0 | Issue 编号 |
| file_path | TEXT NOT NULL | 文件路径 |
| status | TEXT DEFAULT 'accepted' | accepted/rejected |
| source_seq | INTEGER DEFAULT 0 | 事件序号 |
| created_by | TEXT DEFAULT '' | 创建者 |
| created_at | TEXT DEFAULT (datetime('now')) | 创建时间 |
| obs_key | VARCHAR(512) DEFAULT '' | OBS 对象键 |
| content_hash | VARCHAR(64) DEFAULT '' | 内容哈希 |
| updated_at | DATETIME DEFAULT CURRENT_TIMESTAMP | 更新时间 |
| source | TEXT NOT NULL DEFAULT '' | 新增：system/agent/manual |
| archived | INTEGER NOT NULL DEFAULT 0 | 新增：0正常 1归档 |

### 6.2 scene_knowledge_ref（新增表）

SQLite DDL：
```sql
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

MySQL DDL（原生写不走翻译层）：
```sql
CREATE TABLE IF NOT EXISTS scene_knowledge_ref (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
    knowledge_id  BIGINT UNSIGNED NOT NULL COMMENT 'agent_shared_knowledge主键',
    scene_id      VARCHAR(64) NOT NULL DEFAULT '' COMMENT '场景UUID空=全部场景',
    agent_type    VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'Agent类型空=全部Agent',
    enabled       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用0禁用',
    created_by    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '创建人',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    updated_by    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '最后修改人',
    UNIQUE KEY uk_knowledge_scene_agent (knowledge_id, scene_id, agent_type),
    KEY idx_query (enabled, scene_id, agent_type),
    CONSTRAINT fk_scene_knowledge_ref_knowledge_id
        FOREIGN KEY (knowledge_id) REFERENCES agent_shared_knowledge(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库-场景-Agent绑定关系';
```

业务规则：禁止NULL统一空字符串、三元组唯一约束、DISTINCT去重、最多20条、外键级联删除

### 6.3 两表关系
知识的"是什么"在 agent_shared_knowledge，"给谁用"在 scene_knowledge_ref。

---

## 七、Multica 对比分析

| 机制 | 定位 | 用户可见 | 跨 Agent | 生命周期 |
|------|------|---------|---------|---------|
| Skill 绑定 | Agent 带什么知识执行 | ✅ 用户管理 | ❌ 按 Agent 绑定 | 用户控制 |
| Source Context | 跨 Issue 引用上下文快照 | ✅ 用户主动引用 | ❌ 仅目标 Issue | 30 天过期 |
| Agent Memory | Agent 跨任务记住东西 | ❌ 不可见 | ❌ 按 Agent 隔离 | 持久存活 |

核心方向：从"系统自动管理的暗知识库"改为"用户可控、可见、可管理的知识体系"。
