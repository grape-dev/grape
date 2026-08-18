# Knowledge 看板与维护设计：Sidebar「DevOps Skills」→「Knowledge」+ 新增「Project Knowledge」

> 版本：v2.2（已实施 · 已部署 + Agent 级定向注入方案 + 现状诊断）
> 日期：2026-08-17
> 说明：本方案基于现有代码实现梳理（主看板 `src/api/static/`、后端 `src/api/routes/`、`knowledge.py`、`event_archive.py`、`project_info/builder.py`、`prompt_builder.py`、`runtime/service.py` 等）。目标是：将主看板 Sidebar 的「DevOps Skills」分组更名为「Knowledge」，下挂「DevOps Skills（原 Skills Library，保留不动）」与「Project Knowledge（新增）」两个入口，提供按「项目级维度 / DevOps Agent 维度」组织、展示与维护项目级知识的统一看板。**v2.0 起标注为已实施状态**：P0~P3 已落地并部署生产（`new-version2`，commit `a0e0d4f7` / `f7ea2636`）。**v2.1** 新增「§六 Agent 级成员上传文档定向注入」方案。**v2.2** 补充 §6.6 现状诊断：`project-info/member` 知识当前几乎无效（流水线 Agent 拿不到，`{{KNOWLEDGE_DIR}}` 为死占位符）。

---

## 一、背景与目标

### 1.1 现状痛点

系统运行过程中已持续产生三类**项目级知识**，但缺乏统一入口查看与维护：

| 知识类型 | 存储位置（`{WORKSPACE_DIR}/{owner}/{repo}/` 下） | 现状曝光 |
|---|---|---|
| **project-info**（代码图 / 能力基线） | `project-info/project-graph.md`、`project-capabilities.md`、`.metadata.json`（`src/project_info/builder.py` 在 issue 事件时后台生成） | 仅 API `GET /api/repos/{owner}/{repo}/project-info`（`repos.py:563`），**无 UI 页面** |
| **event.json 档案**（Issue 级结构化产出） | `issues/{n}/event.json`（`src/api/event_archive.py` init/append/finalize 维护） | 仅在事件详情页内嵌展示对应 pipeline 段（`events.js:29`） |
| **共享知识 knowledge.md**（Agent 对话沉淀） | `project-info/knowledge.md`（project 级）、`issues/{n}/knowledge.md`（issue 级）、`issues/{n}/{agent}/knowledge.md`（agent 级） | 仅事件对话页「对话产出」侧栏（`events.js:136-149`）；索引在 DB 表 `agent_shared_knowledge` |

**知识产生与消费通道已存在：**
- **产生**：DevOps Agent 通过 `POST /api/events/{seq}/knowledge`（bot token 鉴权，`events.py:637`）→ `write_knowledge()`（`knowledge.py:69`）落文件 + 登记 `agent_shared_knowledge` 索引
- **消费（作为上下文注入）**：
  - 对话类：`prompt_builder._build_knowledge_section()`（`prompt_builder.py:554`）按 project+issue+issue_agent 合并注入
  - 流水线任务类：`project_info_files` 随 `enqueue_task` 打包到远端 worker（`runtime/service.py:188`、`:260`）

### 1.2 目标

1. Sidebar 分组 `DevOps Skills` → **`Knowledge`**，下挂两个入口：
   - **DevOps Skills**（原 Skills Library，技能库逻辑与页面完全不动）
   - **Project Knowledge**（新增，项目知识看板）
2. Project Knowledge 展示与存放**项目级知识**，来源两类：
   - **DevOps Agent 自动生成**：project-info、event.json、知识沉淀 knowledge.md
   - **项目成员主动上传**（新增能力）
3. 知识按两个维度组织：
   - **项目级维度**：project-info 文档 + project 级知识 + 成员上传的项目文档
   - **DevOps Agent 维度**：VOD / Issue / Coding / QA / SRE / Chat 各 Agent 沉淀的知识
4. 这些知识在 Agent 处理任务时作为**最佳上下文输入**（复用/补强现有注入管线）

### 1.3 范围

- **本次范围（主看板）**：`src/api/static/` 侧边栏 + `src/api/routes/` 新增聚合路由 + 新增前端页面 `pages/knowledge.js`
- **可选延伸（Admin 运营后台）**：`admin/frontend`（Vue）+ Java 后端。菜单存 DB（`admin_permission_menus`），现有「Skill 管理」一级菜单（`/skill/manage`）。本方案将 Admin 列为可选后续项，不阻塞主看板落地

---

## 二、设计

### 2.1 Sidebar 与导航改动

**`src/api/static/index.html`（当前 51-52 行）**

```html
<!-- 改前 -->
<div class="grp">DevOps Skills</div>
<a id="nav-skills" onclick="app.nav('skills')"><span class="ic">◈</span>Skills Library</a>

<!-- 改后 -->
<div class="grp">Knowledge</div>
<a id="nav-skills" onclick="app.nav('skills')"><span class="ic">◈</span>DevOps Skills</a>
<a id="nav-knowledge" onclick="app.nav('knowledge')"><span class="ic">❖</span>Project Knowledge</a>
```

**`src/api/static/js/app.js`**

- `VIEWS` 白名单（`app.js:3`）增加 `'knowledge'`
- `nav()` 增加分支（`app.js:230` 附近）：

```js
} else if (view === 'knowledge') {
  title.textContent = 'Project Knowledge';
  renderKnowledge();
}
```

- `index.html` 脚本区新增 `<script src="/static/js/pages/knowledge.js?v=1"></script>`（第 94 行附近）
- 注意 bump 前端缓存版本 `app_cache_ver`（`app.js:332`）与脚本 `?v=` 参数，避免旧缓存

### 2.2 后端：新增路由模块 `src/api/routes/knowledge.py`

新增独立 APIRouter，聚合复用现有能力：

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/knowledge` | GET | 聚合返回两类维度的知识清单（项目级 + Agent 维度） |
| `/api/knowledge/{type}/{...}` | GET | 读取单个知识全文（MD/JSON 渲染，需路径穿越防护） |
| `/api/knowledge` | POST | **成员手动上传**知识文档（md/txt/json，鉴权当前用户） |
| `/api/knowledge/{id}` | DELETE | 删除成员上传的知识（可选） |
| `/api/knowledge/context` | GET | 返回合并后的注入上下文文本（便于前端预览"Agent 实际会用到的上下文"） |

**聚合逻辑要点：**

- **项目级维度**：
  - project-info：`load_project_info(repo_key)`（复用 `repos.py:563` 用法，见 `runtime/service.py:188`），返回 files + meta（commit/branches/updated/degraded）
  - project 级知识：`agent_shared_knowledge` 表按 `repo_key` + `scope='project'` 查询
  - 成员上传：扫描成员上传目录
- **Agent 维度**：
  - `agent_shared_knowledge` 表按 `repo_key` + `scope IN ('issue','issue_agent')` 按 `agent_type` 分组
  - 每 Agent 下按 issue 聚合其 `knowledge.md`，并关联对应 `event.json`（`event_archive.load_event_json`）
- **读取防护**：所有知识读取必须 `os.path.normpath` + `startswith` 校验（复用 `SkillService.SAFE_SKILL_PATH` 白名单模式），防路径穿越；文件读取 UTF-8

### 2.3 前端：新增页面 `src/api/static/js/pages/knowledge.js`

仿 `skills.js` 结构（入口 `renderKnowledge()`，复用 `.cd` / `.skill-card` 样式、`API`、`app.showToast`、`renderMarkdown`、`_AGENT_TYPES`）：

- **维度页签**：`项目级` / `Agent 维度`
  - **项目级**：project-info 卡片（Project Graph、Project Capabilities、metadata、knowledge.md），展示生成时间 / degraded 状态；下方为成员上传文档区，含「＋ 上传知识」按钮（仿 skills.js 模态框 + `API.upload` 模式）
  - **Agent 维度**：按 `_AGENT_TYPES` 分组（VOD / Issue / Coding / QA / SRE / Chat），各一组卡片，展示该 Agent 沉淀的知识条目与来源 Issue；点开看全文（复用 `renderSkillDetail` 详情布局）
- 顶部可选搜索框（复用 `filterSkills` 思路）
- 加载失败兜底 `.error`

### 2.4 成员手动上传

- **前端**：模态框选择文件（仅 `.md/.txt/.json`，≤10MB），填写所属维度（项目级 / 指定 Agent）
- **后端**：`POST /api/knowledge` 鉴权当前用户；落盘（见 §六 目标态）：
  - `scope=project` → `{WORKSPACE_DIR}/{owner}/{repo}/project-info/member/{username}/{name}`（现状）
  - `scope=agent` → **目标态** `project-info/agent/{agent_type}/{name}`（§六；现状仍落 `member/` 且未分流）
  - 登记 `agent_shared_knowledge` 索引（`scope='member'`，`agent_type` 按维度）
- **校验**：文件类型白名单（`md/txt/json`，禁用 `.py/.sh/.exe` 等可执行文件，对齐 `SkillService` 规则）
- ⚠️ **现状缺口（§六）**：上传时选「项目级 / Agent 维度」目前只影响索引的 `agent_type`，落盘/展示/注入均未按 agent 分流——成员上传文档一律当项目级处理。

### 2.5 知识作为最佳上下文（注入管线补强）

现有注入通道已存在，需补齐两处：

1. **对话类 Agent**：`_build_knowledge_section`（`prompt_builder.py:554`）在读取 `project-info/knowledge.md` 时，一并合并成员上传文档摘要，保证对话 Agent 可用最新项目知识
2. **流水线 Agent**：`project_info_files` 打包（`runtime/service.py:260`）把成员上传文档并入（与 project-info 产物同一 `project-info/` 目录，改动最小），保证 Coding/QA/SRE 等 pipeline Agent 拿到最新项目知识

> 注入优先级（范围从大到小）保持现有约定：project → issue → issue_agent。

---

## 三、Project Knowledge 原型图（线框稿）

> 以下线框稿风格对齐 `user-workflow-overview.md`，描述目标态 UI，用于评审与实现参考；**暂不实施**。所有页面沿用现有主看板风格（`.cd` 卡片、`bt` 按钮、页签、模态框）。

### 3.1 Sidebar（Knowledge 分组）

```
┌─ Sidebar（改后） ────────────────────────────────────┐
│  [Grape]（点击返回仓库选择大堂）                        │
│  Multi-Agent DevOps                                 │
│  [team/backend-service ▼]                           │
│                                                     │
│  Workspace                                          │
│    ■ Dashboard                                      │
│    ▶ Pipeline                                       │
│                                                     │
│  DevOps Agents                                      │
│    ● VOD Agent                                      │
│    ● Issue Agent                                    │
│    ● Coding Agent                                   │
│    ● QA Agent                                       │
│    ● SRE Agent                                      │
│                                                     │
│  Knowledge                                          │
│    ◈ DevOps Skills                                  │
│    ❖ Project Knowledge                              │
│                                                     │
│  Account                                            │
│    📋 Task Center                                   │
│    ☎ Chat Agent                                     │
│                                                     │
│  Settings                                           │
│    ...（不变）                                       │
│                                                     │
│    [👤 user] [↩ 登出]                               │
└─────────────────────────────────────────────────────┘
```

- 分组名 `DevOps Skills` → **`Knowledge`**；原 `Skills Library` 更名 **`DevOps Skills`**（保留原导航 `app.nav('skills')`），新增 `Project Knowledge`（`app.nav('knowledge')`）。

### 3.2 Project Knowledge 主页（项目级维度页签）

```
┌─ Top Bar ─────────────────────────────────────────────────┐
│  Project Knowledge · team/backend-service ↗                │
│  [🔍 搜索知识...]  [项目级] [Agent 维度]        [＋ 上传知识] │
├────────────────────────────────────────────────────────────┤
│  📦 项目知识基线（DevOps Agent 自动生成 · project-info）      │
│  ┌─────────────────────┐  ┌─────────────────────┐          │
│  │ 📊 Project Graph    │  │ ⚙ Project Caps      │          │
│  │ 代码依赖与调用链     │  │ 技术栈与能力清单     │          │
│  │ 更新 08-16 10:02    │  │ 更新 08-16 10:02    │          │
│  │ main · a1b2c3d4     │  │ [View]             │          │
│  │ [View]              │  └─────────────────────┘          │
│  └─────────────────────┘  ┌─────────────────────┐          │
│  ┌─────────────────────┐  │ 🗂 metadata         │          │
│  │ 📄 项目知识沉淀      │  │ 生成元信息 (JSON)   │          │
│  │ knowledge.md        │  │ 更新 08-16 10:02    │          │
│  │ 2 条 · Agent 沉淀    │  │ [View]             │          │
│  │ [View]              │  └─────────────────────┘          │
│  └─────────────────────┘                                   │
│                                                            │
│  👥 成员上传文档（项目成员手动维护）                          │
│  ┌─────────────────────┐  ┌─────────────────────┐          │
│  │ 📄 部署规范.md       │  │ 📄 环境说明.json     │          │
│  │ @alice · 08-15 16:40 │  │ @bob · 08-14 09:12  │          │
│  │ [View] [Delete]     │  │ [View] [Delete]     │          │
│  └─────────────────────┘  └─────────────────────┘          │
└────────────────────────────────────────────────────────────┘
```

### 3.3 Project Knowledge 主页（Agent 维度页签）

```
┌─ Top Bar ─────────────────────────────────────────────────┐
│  Project Knowledge · team/backend-service                  │
│  [🔍 搜索知识...]  [项目级] [Agent 维度]        [＋ 上传知识] │
├────────────────────────────────────────────────────────────┤
│  🔍 VOD Agent · 3 条知识                                    │
│  ┌───────────────────────────────────────────────────┐    │
│  │ [agent 级] 验收标准须含性能指标                     │    │
│  │    @vod 沉淀 · Issue #7 · 08-16 09:40   [View]    │    │
│  ├───────────────────────────────────────────────────┤    │
│  │ [agent 级] 原文不删除，先澄清后处理                │    │
│  │    @vod 沉淀 · Issue #3 · 08-12 14:20   [View]    │    │
│  └───────────────────────────────────────────────────┘    │
│  📋 Issue Agent · 1 条知识                                 │
│  ┌───────────────────────────────────────────────────┐    │
│  │ [agent 级] 需求须拆分为可验收子任务                │    │
│  │    @issue 沉淀 · Issue #5 · 08-15 11:02  [View]   │    │
│  └───────────────────────────────────────────────────┘    │
│  💻 Coding Agent · 无沉淀                                  │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 该 Agent 暂无知识沉淀（来自 Issue 对话的 accepted） │    │
│  └───────────────────────────────────────────────────┘    │
│  ... QA / SRE / Chat Agent 同上分组                       │
└────────────────────────────────────────────────────────────┘
```

### 3.4 知识详情页（点击任意 [View]）

```
┌─ Top Bar ─────────────────────────────────────────────────┐
│  Knowledge Detail · team/backend-service                   │
├────────────────────────────────────────────────────────────┤
│  [← 返回 Project Knowledge]                                │
│                                                            │
│  ┌─ 知识信息 ────────────┐  ┌─ 正文（Markdown 渲染）──────┐ │
│  │ 名称   项目知识沉淀     │  │  ## 项目知识沉淀            │ │
│  │ 维度   项目级          │  │  - [seq 23 / agent] 验收…   │ │
│  │ 类型   Agent 自动生成  │  │  - [seq 19 / agent] PR 走… │ │
│  │ 来源   project/know…  │  │                            │ │
│  │ 更新   08-16 10:02    │  └────────────────────────────┘ │
│  │ 仓库   team/backend…  │                                │
│  └────────────────────────┘                                │
└────────────────────────────────────────────────────────────┘
```

### 3.5 上传知识弹窗（＋ 上传知识）

```
┌─ 上传项目知识 ────────────────────────────────────────────┐
│  维度:    ● 项目级   ○ Agent 维度                          │
│  Agent:   [VOD Agent ▾]       （Agent 维度时启用选择）      │
│  文件:    [📁 选择文件]   部署规范.md                       │
│  格式:    .md / .txt / .json，≤10MB                       │
│  ──────────────────────────────────────────────           │
│  上传中...  ▓▓▓▓▓▓░░░░ 45%                                │
│                                      [取消]   [上传]        │
└───────────────────────────────────────────────────────────┘
```

> 交互说明（对齐现有上传模式）：
> - 「Agent 维度」勾选后展示 Agent 下拉（vod/issue/coding/qa/sre/chat），决定索引归类；「项目级」即归入项目级维度。
> - 上传走 `API.upload`（XHR 进度条），成功 toast 后刷新列表；校验失败多行展示错误（对齐 skills 上传）。
> - 详情页与弹窗复用现有 `renderSkillDetail` / `showSkillUpload` 布局与 `.cd` / `bt` 样式。

---

## 四、项目知识文件格式分析

> 基于代码实现（`src/project_info/builder.py`、`src/api/knowledge.py`、`src/api/event_archive.py`）与磁盘实测（`data/workspaces/org/test/project-info/`）。共 4 类文件、5 种格式；决定了聚合接口返回值与前端渲染方式。

### 4.1 `project-graph.md` — Markdown（代码架构图）

- 生成器：`src/project_info/builder.py:19-37` `_GRAPH_TEMPLATE`
- 结构：

```markdown
# Project Graph: {owner}/{repo}

> Last updated: {ISO时间} | Branches: {分支列表} | Commit: {sha}

## Architecture Overview       ← codegraph explore 输出
{内容，每节 ≤600 字符截断}
## Module Dependency Graph
## Core Call Chains
## Branch Structure
## Key Files
```

- 特点：各节 600 字截断（`_build_graph_md` 用 `arch[:600]`）；codegraph 不可用时降级写 `"codegraph 不可用，降级生成"`。
- 渲染：**Markdown**（可直接 `renderMarkdown`）。

### 4.2 `project-capabilities.md` — Markdown（能力/技术栈清单）

- 生成器：`builder.py:39-63` `_CAP_TEMPLATE` + `_build_capabilities_md`
- 结构：

```markdown
# Project Capabilities: {owner}/{repo}

> Last updated: {ISO时间}

## Tech Stack          ← 实际有值
## Supported Features  ← 实际有值
## API Endpoints       ← 实际有值
## External Integrations  ← 固定写死 "N/A"
## Skills & Agents         ← "N/A"
## Data Model              ← "N/A"
## Configuration           ← "N/A"
```

- ⚠️ 注意：`builder.py:199` 中 integrations / skills_agents / data_model / config 四节**硬编码 "N/A"**，模板预留但未填充。看板详情建议隐藏 N/A 小节。
- 渲染：**Markdown**。

### 4.3 `.metadata.json` — JSON（元数据）

- 生成器：`_write_meta`（builder.py:95-102）+ `_read_meta`
- 实际字段（builder.py 写入）：

```json
{
  "last_updated": "2026-08-16T10:02:00+08:00",
  "last_commit_sha": "a1b2c3d4...",
  "degraded": false,
  "note": "",
  "branches_analyzed": ["main", "master"]
}
```

- 文档约定（`codegraph-builder-plan.md`）另有 `graph_version`。
- 读取：`runtime/service.py:187 load_project_info` 读它，并把 `files[".metadata.json"]`（JSON 字符串）一并返回。
- 渲染：**JSON**（详情需美化展示，不走 Markdown）。

### 4.4 `knowledge.md` — 纯文本追加式列表（共享知识）

- 写入：`src/api/knowledge.py:93 write_knowledge`，**追加**（非覆盖）
- 每行格式：`- [seq {source_seq} / {created_by}] {content}`
- 磁盘实测（`data/workspaces/org/test/project-info/knowledge.md`）：

```
- [seq 502 / agent] 知识-project
- [seq 504 / agent] 此仓库 PR 走内部评审
```

- ⚠️ 特点：**只追加不清理**，样例已 46 行且内容重复；`accepted` 才落文件，`rejected` 仅登记 DB 索引。文件本身无时间戳，需以 `agent_shared_knowledge.created_at` 补充展示；看板建议按 DB 索引去重展示。
- 分三级存放：`project-info/knowledge.md`（项目级）、`issues/{n}/knowledge.md`（issue 级）、`issues/{n}/{agent}/knowledge.md`（agent 级）。
- 渲染：**纯文本 / Markdown 列表**。

### 4.5 `issues/{n}/event.json` — JSON（Issue 级结构化档案）

- 写入：`src/api/event_archive.py:140 init_event_json` → `append_event_archive_log` → `finalize_event_archive`
- 结构（`tests/test_event_archive.py` 确认）：

```json
{
  "issue":  { "number": 5, "title": "登录超时", "status": "open", "created_at": "" },
  "status": "completed",
  "created_at": "", "updated_at": "",
  "summary": "Issue #5：登录超时 | ...",
  "pipeline": [
    {
      "seq": 12, "agent": "issue", "event_type": "issue_opened",
      "runtime": "opencode", "skill": "issue",
      "status": "completed", "exit_code": 0, "duration": 12.3,
      "trigger": { "actor": "alice", "branch": "main", "raw_type": "Issue Hook" },
      "input":  { "issue_id": "#5", "summary": "新 Issue" },
      "output": { "actions": ["labeled:bug"], "conclusion": "分类为 bug",
                  "artifacts": ["comment"], "next_step": "A2A coding",
                  "issue_summary": "..." },
      "a2a_to": null,
      "logs":   [ { "time": "", "type": "text", "msg": "..." } ],
      "started_at": "", "completed_at": ""
    }
  ]
}
```

- 渲染：**JSON**（建议结构化渲染 pipeline 段或美化 JSON，不走 Markdown）。

### 4.6 格式 → 看板映射

| 文件 | 格式 | 渲染方式 | 元信息（卡片显示） | 读取入口 |
|---|---|---|---|---|
| `project-graph.md` | Markdown | `renderMarkdown` | `meta.last_updated` / `meta.degraded` / commit | `load_project_info()` |
| `project-capabilities.md` | Markdown | `renderMarkdown`（隐藏 N/A 节） | 同上 | `load_project_info()` |
| `.metadata.json` | JSON | 美化 JSON 展示 | — | `load_project_info()`（`files[".metadata.json"]`） |
| `knowledge.md`（project 级） | 追加列表 | 列表/去重渲染 | `agent_shared_knowledge.created_at` | `read_knowledge(repo_key, scope='project')` |
| `issues/{n}/event.json` | JSON | 结构化/美化 | `issue.number` / `status` | `event_archive.load_event_json()` |

> 聚合路由可直接复用上述三个现成读取入口（`load_project_info` / `read_knowledge` / `load_event_json`），无需重复解析格式。

---

## 五、生产环境项目知识现状与风险（实测）

> 2026-08-17 在 `203.0.113.2` 上实测（工作区路径解析见 `src/config.py:21-40`，生产 systemd 未设 `DEVOPS_WORKSPACE_DIR` → `User=root` → 主工作区 `/opt/grape-workspaces`）。

### 5.1 工作区数据分布（两处）

| 工作区 | 含 project-info 的仓库 | 状态 |
|---|---|---|
| `/opt/grape-workspaces`（主工作区） | 逐步累积（含 `Jason7602/get-together`、`gcw_7pdiBkYp/hello-calculator`、`Wing-Jason/test_script` 等） | `get-together` **degraded=false 完整生成**（19:06:58 触发，19:07:02 regenerated） |
| `/opt/grape/data/workspaces`（历史遗留） | 19 个仓库 | **9 个完整**（graph + capabilities + metadata），其余仅 metadata |

- 主工作区**新数据确实写这里**；旧目录是 2026-08-12 工作区改家目录前的历史数据，**从未迁移**。
- ⚠️ 旧目录里的 9 个完整 project-info 基线（`gitcode-pr-assistant` 等，08-06~08-11 生成）当前**不会**出现在只读 `WORKSPACE_DIR` 的看板/`load_project_info` 里。

### 5.2 degraded 根因（存在 bug）

现象：迁移后 project-info 生成几乎没成功过——日志只有 `project-info refresh triggered`，产物仅 `.metadata.json`（`degraded:true`），无 graph/capabilities。

根因是 `src/project_info/builder.py:239 _ensure_analysis_worktree`：

```python
base = _repo_base_dir(repo_key)          # {WORKSPACE_DIR}/{owner}/{repo}/codebase
if not os.path.isdir(base):
    return None                          # ← 直接放弃 → degraded: "analysis worktree failed"
```

- 它依赖 bare mirror `codebase/` 存在；而 `codebase/` **只由 `repogit.ensure_repo_base` 惰性创建**，该函数**仅在被 coding/qa/sre（git agent）处理 Issue 时**经 `ensure_issue_worktree` 触发。
- 只跑 VOD/Issue 的仓库（如 `hello-calculator`）**永远不建 codebase** → `_ensure_analysis_worktree` 永远返回 None → 每次 refresh 都 `{"ok": False, "degraded": True, "reason": "analysis worktree failed"}`，只写 metadata、不写 md 文件。
- codegraph CLI 已安装（1.5.0），非工具缺失；迁移前旧工作区有 codebase → 能成功（如 `gitcode-pr-assistant` degraded:false），迁移后主工作区为空目录 → 失败。

**结论**：是设计缺陷（`build_project_info` 依赖别的功能副产物创建的 mirror，自己从不主动建），不是环境问题。

**修复方向**（一行级改动）：`_ensure_analysis_worktree` 在 `codebase` 不存在时先调用 `repogit.ensure_repo_base(repo_key, token)` 建立 bare mirror（该函数带 `token`、可 fetch），失败才 return None。

### 5.3 对看板的影响与前置建议

1. 只读主工作区会**看不到 9 个完整基线** → 看板数据不全。
2. 建议实现前先二选一（或都做）：
   - **一次性工作区数据迁移**：`/opt/grape/data/workspaces/*` → `/opt/grape-workspaces/`（注意 bare mirror / issues / project-info 一并迁移，`repo_key` 去前缀规则见 `repo_key.py`）；
   - **聚合层兼容旧目录**：`load_project_info` 主工作区无文件时回退旧目录（涉及改动 `runtime/service.py`，需评估）。
3. 修复 5.2 的 bug，让新仓库能正常生成基线。

### 5.4 首跑时序竞态（已定位，暂未修复）

实测 `Jason7602/get-together`：issue_opened 触发刷新（19:06:58）→ 4 秒后 regenerated（19:07:02，degraded=false，文件齐全），但**同一事件构建的 VOD prompt 仍显示「无项目知识基线」**。

根因：`event_pipeline.py:309` 的 `maybe_refresh_project_info` 是**异步后台线程**（`threading.Thread(...daemon=True)`），与 `run_pipeline` 同步构建 prompt 并发执行；仓库**首次**有 issue 时刷新从零开始（甚至要先 `ensure_repo_base` clone + `codegraph init`），prompt 必然抢在刷新完成前 → 第一次任务的 prompt 无基线，第二次起才有。

> 注意：这是**设计如此**——`skills/vod/SKILL.md` A1/B1 明示「⚠️ 不再自行生成：知识基线由服务端统一维护」，VOD 只消费 prompt 注入的基线，服务端负责生成。问题仅在**首跑时序**，非 VOD 不主动建知识。

### 5.5 已知问题：看板 View 加载失败「文件不存在」（待修复）

现象：`Jason7602/get-together` 基线已生成，点击项目级页签 View 报 `文件不存在: project-graph.md`。

根因：**路径语义在前后端不一致，三类路径三种基准**：

| 来源 | path 语义 | 示例 | content 接口解析结果 |
|---|---|---|---|
| project-info `files[].name` | 相对 `project-info/` | `project-graph.md` | `{ws}/owner/repo/project-graph.md` ❌ |
| 知识沉淀 `file_path` | 相对 `WORKSPACE_DIR`（DB 原文） | `owner/repo/project-info/knowledge.md` | 双重嵌套 ❌ |
| 成员文档 `path` | 相对仓库根 | `project-info/member/alice/x.md` | ✅ |

而后端 `GET /api/knowledge/content` 的 `_read_file` 只认「相对仓库根」。前两类与之错位 → 404。

修复方向（约 15 行内，不影响注入管线）：
1. 聚合接口 `project_files[].name` 改为完整相对路径 `project-info/project-graph.md`（与 member_docs 一致）；
2. 知识沉淀/Agent 条目返回前，把 DB 的 WORKSPACE 相对路径剥掉 `{owner}/{repo}/` 前缀 → 「相对仓库根」；
3. 前端 `knowledge.js` 统一用修正后的路径调用 `loadKnowledgeContent`。

---

## 六、Agent 级成员上传文档定向注入（方案，v2.1）

### 6.1 背景与缺口

现状：成员上传时选「项目级 / Agent 维度」**只影响索引的 `agent_type` 字段**，落盘都在 `project-info/member/{username}/`，展示都在项目级页签，注入时 `member_files()` 把所有 member 文档打包给**所有 Agent**——「Agent 维度」上传实际不产生定向行为。

目标：成员上传的 Agent 级文档**只给对应 Agent** 使用：落盘 `project-info/agent/{agent_type}/`，并在该 Agent 的 prompt 中告知路径供其 read tool 读取。

### 6.2 设计：复用「占位符物化」机制

现有 `runtime/agent.py:200-223` 已具备「服务端 project-info 文件 → 打包 `project_info_files` → worker 物化到 `cwd/.project-info/` → `{{PROJECT_INFO_DIR}}` 占位符替换为本地目录」的能力。本方案直接复用，无需新造物化通道：

```
成员上传（scope=agent, agent_type=vod）
  → 落盘 project-info/agent/vod/部署规范.md
        ↓
coding 任务 enqueue_task(agent_type=coding, issue_id=#5)
  → project_info_files 打包：
       project-graph.md / project-capabilities.md / .metadata.json   （公共基线）
     + agent/coding/*.md|txt|json                                    （仅本 agent，不含 vod）
        ↓ worker 物化
  → cwd/.project-info/agent/coding/部署规范.md
        ↓ prompt 注入
  → "本 Agent 专属知识：{{PROJECT_INFO_DIR}}/agent/coding/，存在则用 read tool 读取并遵循"
```

### 6.3 存储约定（目标态）

| 维度 | 落盘路径（`{WORKSPACE_DIR}/{owner}/{repo}/` 下） |
|---|---|
| 项目级上传 | `project-info/member/{username}/{name}`（现状保留） |
| Agent 级上传 | `project-info/agent/{agent_type}/{name}`（新增） |
| project-info 基线 | `project-info/project-graph.md` / `project-capabilities.md` / `.metadata.json` |
| Agent 对话沉淀 | `issues/{n}/{agent_type}/knowledge.md`（既有） |

### 6.4 改动点

| # | 位置 | 改动 |
|---|---|---|
| 1 | `knowledge.py` `upload_knowledge` | `scope=agent` 时落盘 `project-info/agent/{agent_type}/{name}`（不再落 `member/`）；`scope=project` 维持现状 |
| 2 | `runtime/service.py` `enqueue_task` | `project_info_files` 打包时**只并入当前任务 `agent_type`** 的 `project-info/agent/{agent_type}/` 文件；key 用相对 project-info 目录（`agent/{agent_type}/xxx.md`），修复现有 `member_files` 全量 + WORKSPACE 相对 key 导致的物化嵌套隐患 |
| 3 | `prompt_builder.py` | `_build_project_info_section` 增加 `agent_type` 参数，尾部追加「本 Agent 专属知识：{{PROJECT_INFO_DIR}}/agent/{agent_type}/，存在则用 read tool 读取并遵循」 |
| 4 | `knowledge.py` 聚合 | Agent 维度页签补充读取 `project-info/agent/{agent_type}/` 的成员文档 |
| 5 | `knowledge.py` `delete_knowledge` | 放开 `project-info/agent/` 前缀删除（按 agent_type + 作者校验） |
| 6 | 前端 `knowledge.js` | Agent 维度上传后归入对应 Agent 分组展示 |

### 6.5 注意与决策点

- **`member/` 与 `agent/` 并存**：新上传全走 `agent/`（项目级维持 `member/`），旧数据兼容读取；是否迁移由实施时评估。
- **打包体积**：`load_project_info` 已含 graph/capabilities，再加 Agent 目录后每任务打包体积略增；Agent 文档通常小，可接受。
- **注入优先级**：Agent 专属文档应优先于 project 级沉淀（越具体越优先，符合既有 project→issue→issue_agent 约定）。
- **物化语义修复**：`member_files()` 当前返回 WORKSPACE 相对 key，并入 `_pif` 后会物化成 `.project-info/{owner}/{repo}/...` 双重嵌套——本方案改用相对 project-info 目录 key，一并修复。

### 6.6 现状诊断：`project-info/member` 的知识当前几乎无效（实测分析）

> 结论：成员上传的知识目前**能看不能用**——看板可见、Event 对话可闻，但真正干活的流水线 Agent 拿不到。

**消费链路逐环排查（现状）：**

| 环节 | 现状 | 是否真正消费 |
|---|---|---|
| 看板展示 | `list_knowledge` → `_member_docs()` 扫描 member 目录，项目级页签展示 | ✅ 人能看 |
| 流水线任务打包 | `enqueue_task` → `member_files()` 并入 `project_info_files` + `knowledge_files` | ⚠️ 只打包 |
| 流水线 prompt 指引 | `build_prompt` **不含** `{{KNOWLEDGE_DIR}}`，也不调 `_build_knowledge_section` | ❌ **无指引** |
| 对话 prompt 注入 | `build_conversation_prompt` → `_build_knowledge_section` 有 `[member 级]` 摘要 | ✅ 仅对话场景 |
| worker 物化 | `agent.py:217` 物化到 `.knowledge/`，`{{KNOWLEDGE_DIR}}` replace | ⚠️ 无占位符可替换 |

**关键证据：`{{KNOWLEDGE_DIR}}` 是"死"占位符**
- `src/prompt_builder.py` 全文件**从未生成** `{{KNOWLEDGE_DIR}}` 字符串（grep 无匹配）；
- `src/runtime/agent.py:223` 却执行 `prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)` → prompt 里没有该串，replace 是 **no-op**；
- 流水线 `build_prompt` 只调 `_build_project_info_section`（graph/capabilities），**不调** `_build_knowledge_section`。

→ 成员文档虽被打包进任务、物化到 `.knowledge/`，但 Agent 的 prompt 里**没有任何一句**告知文件存在、位置、是否读取 → Agent 不会去翻 `.knowledge/`，知识等于没用。

**根因归纳：**

| 缺陷 | 位置 |
|---|---|
| `member_files()` 全量打包给所有 Agent（不按 agent_type） | `knowledge.py:121-124`、`service.py:262-267` |
| 打包 key 用 WORKSPACE 相对路径（`owner/repo/...`），物化嵌套 | `knowledge.py` `member_files` |
| 流水线 prompt 无 knowledge/member 指引，`{{KNOWLEDGE_DIR}}` 死占位符 | `prompt_builder.py` `build_prompt` |
| `_build_knowledge_section` 只服务对话类，流水线不调用 | `prompt_builder.py` |

**与 §六 方案的关系**：本方案正好一并修复以上缺陷——① 上传按 agent 分目录实现定向注入；② prompt 追加「本 Agent 专属知识：`{{PROJECT_INFO_DIR}}/agent/{agent_type}/`，用 read tool 读取」让 Agent **知道并读取**；③ 打包 key 改相对 project-info 目录修正物化；④ 复用**活**占位符 `{{PROJECT_INFO_DIR}}`（`build_prompt` 已替换）而非死的 `{{KNOWLEDGE_DIR}}`。

---

## 七、实施步骤（建议顺序）

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | Sidebar 改名 + 新增导航入口（index.html + app.js） | ✅ 已实施（`a0e0d4f7`，`app.js?v=23`） |
| P1 | 后端聚合路由 `src/api/routes/knowledge.py`（项目级 + Agent 维度查询、知识读取、路径防护、context 接口），`main.py` 挂载 | ✅ 已实施 |
| P1 | 前端页面 `knowledge.js`（双维度页签、详情查看、加载失败兜底） | ✅ 已实施 |
| P2 | 成员上传：`POST /api/knowledge` + 前端上传模态框 | ✅ 已实施 |
| P2 | 注入补强：`prompt_builder.py` + `runtime/service.py` 并入成员文档 | ✅ 已实施（`member_files` 并入 `knowledge_files` / `project_info_files`） |
| P3 | 测试：新增 `tests/test_knowledge_board.py`（22 用例）+ builder 修复回归（3 新增） | ✅ 已实施，全量 **1071 passed** |
| 修复 | `builder.py` `_ensure_analysis_worktree` 主动 `ensure_repo_base`（§5.2） | ✅ 已实施 |
| 修复 | §5.5 View 加载失败路径语义统一 | ✅ 已实施（commit `386cc906`，已合入 main） |
| 待办 | §5.4 首跑时序竞态（prompt 构建等待刷新） | ⏳ 待评估 |
| 待办 | §六 Agent 级成员上传文档定向注入（落盘 `project-info/agent/{agent_type}/` + prompt 告知路径） | ⏳ 待实施 |
| 可选 | Admin 后台延伸：admin 菜单加「项目知识」入口 + Java `KnowledgeController`（复用 `SkillController` 模式） | 不阻塞主看板 |

---

## 八、测试用例设计

### 8.1 新增测试文件 `tests/test_knowledge_board.py`

API 级测试，复用 `tests/test_event_conversation.py` 的 TestClient 模式：
- 环境：`DEVOPS_DB_PATH=data/test_sessions.db`、`DEVOPS_DB_BACKEND=sqlite`、`DEVOPS_WORKSPACE_DIR` 隔离（`conftest.py` 已兜底）
- 脚手架：`setup_module`（`init_database`、`create_user`、seed `managed_repos` + `_BOT_TOKEN`、`_headers`）、`_clean`（清 `agent_shared_knowledge` / `events` / `event_thread` / 工作区 project-info 目录）、`_seed_event`、`_bot_headers`
- 独立 `WORKSPACE_DIR`（monkeypatch `src.config.WORKSPACE_DIR` + `src.api.knowledge.WORKSPACE_DIR`），种入 project-info 文件与 knowledge.md，避免污染共享测试库

| 测试类 | 用例 | 覆盖点 / 断言 |
|---|---|---|
| **TestKnowledgeList**（`GET /api/knowledge?repo=`） | `test_list_empty_repo` | 无知识 → `project` 与 `agents` 维度均为空 |
| | `test_list_project_dimension` | 种 project-info 三文件 + project 级 knowledge.md → 项目级维度返回 files（graph/capabilities/meta）+ 知识条目，meta 含 updated/degraded |
| | `test_list_agent_dimension` | 种 `issue_agent`（vod/coding）知识 → 按 `agent_type` 分组返回，每条含来源 issue_num 与 file_path |
| | `test_list_repo_isolation` | repo A 的知识不出现在 repo B 查询结果（`repo_key` 过滤） |
| | `test_list_requires_auth` | 无 token → 401/403 |
| **TestKnowledgeDetail**（`GET /api/knowledge/{type}/{...}`） | `test_read_markdown` | 读 project-graph.md → 200，内容与落盘一致 |
| | `test_read_json_meta` | 读 `.metadata.json` → 200，响应体可 JSON 解析 |
| | `test_path_traversal_blocked` | `..%2F` / 绝对路径 → 404/400，不得读取目录外文件（`normalize`+`startswith`） |
| | `test_not_found` | 不存在路径 → 404 |
| **TestKnowledgeUpload**（`POST /api/knowledge`） | `test_upload_markdown` | 上传 .md → 200，落盘成员目录 + 登记 `agent_shared_knowledge`（`created_by`=当前用户） |
| | `test_upload_agent_dimension` | 指定 agent_type → 索引带 agent_type，可被 Agent 维度查询聚合到 |
| | `test_upload_forbidden_ext` | 上传 `.py` / `.sh` → 400 |
| | `test_upload_too_large` | >10MB → 400 |
| | `test_upload_requires_auth` | 无 token → 401/403 |
| **TestKnowledgeContext**（`GET /api/knowledge/context`） | `test_context_merges_scopes` | project + issue + issue_agent 知识合并，文本含三级内容，顺序按 project→issue→issue_agent |
| | `test_context_includes_member_docs` | 成员上传文档摘要进入 context |
| | `test_context_empty` | 无知识 → 空字符串 / 空结构（不报错） |
| **TestInjection**（注入补强回归，对齐 `test_knowledge_injected_into_prompt` / `test_enqueue_packs_knowledge_files`） | `test_member_doc_injected_into_prompt` | `build_conversation_prompt` 产出的 prompt 包含成员上传内容 |
| | `test_project_info_files_include_member_docs` | `enqueue_task` 后 task 的 `project_info_files` JSON 含成员文档 rel_path |
| **TestAgentScopedUpload**（§六 Agent 级定向注入） | `test_upload_agent_looks_agent_dir` | `scope=agent` + `agent_type=vod` 上传 → 落盘 `project-info/agent/vod/{name}`（不再落 `member/`） |
| | `test_enqueue_only_packs_own_agent_dir` | 种 `project-info/agent/vod/x.md` + `agent/coding/y.md`，`enqueue_task(agent_type=coding)` → `project_info_files` 只含 `agent/coding/`，不含 `agent/vod/` |
| | `test_prompt_tells_agent_dir` | `_build_project_info_section(repo_key, agent_type='coding')` 输出含 `{{PROJECT_INFO_DIR}}/agent/coding/` 指引 |
| | `test_project_info_key_is_pi_relative` | 打包 key 为相对 project-info 目录（`agent/coding/y.md`），无 WORKSPACE 前缀嵌套 |

### 8.2 回归保护（不改现有测试，仅全量跑）

- `tests/test_event_conversation.py`：知识写入、注入、打包、bot token 鉴权全绿（改动只在 `_build_knowledge_section` / `project_info_files` 打包处，须保持原有行为）
- `tests/test_project_info_builder.py`：project-info 生成路径不受影响
- `tests/test_api_routes.py`：skills 等既有路由不回归

### 8.3 UI 冒烟（`tests/ui_test_playwright.py`，或人工冒烟清单）

- Sidebar 显示 `Knowledge` 分组，下挂 `DevOps Skills` + `Project Knowledge` 两个入口，均能点击导航
- `nav('knowledge')`：无 JS 报错；选择仓库后页面渲染两个维度页签
- 项目级页签：project-info 卡片展示与详情查看正常；成员上传模态框可打开并上传成功
- Agent 维度页签：各 Agent 分组卡片渲染、空态提示正常
- 回归：DevOps Skills 原有上传/详情/删除/转仓库仍可用

---

## 九、风险与注意点

- **路径安全**：所有知识读取必须 `normalize` + `startsWith` 校验，防路径穿越
- **鉴权**：知识读取沿用 `get_current_user`（登录用户）；跨仓库隔离按 `repo_key` 过滤；上传仅当前用户，写入成员目录
- **存量数据兼容**：`agent_shared_knowledge` 与磁盘文件以文件为唯一正体（`knowledge.py` 注释明示），聚合接口以磁盘扫描为准、DB 索引为辅，避免历史缺索引行导致漏显
- **前端缓存**：`app.js` 的 `app_cache_ver` 与脚本 `?v=` 参数需 bump，避免旧缓存
- **不回归**：DevOps Skills（原 Skills Library）全部逻辑（upload / convert / delete / sync-official / template）保持不变，仅分组名变更

---

## 十、结论

改动面集中、基础设施基本就绪：知识生成（Agent → `POST /api/events/{seq}/knowledge`）、存储（project-info / event.json / knowledge.md）、注入（prompt_builder / runtime.service）均已存在。**缺的是统一展示层与成员上传入口**。按 P0 → P2 推进，主看板即可获得「Knowledge → DevOps Skills + Project Knowledge」双入口导航与按「项目级 / DevOps Agent」双维度的项目知识看板，知识天然成为 Agent 任务上下文。
