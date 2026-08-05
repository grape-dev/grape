# 仓库选择页 Sidebar + 项目模板一键创建 设计方案

> 目标：为「选择要加入的仓库」页面（`renderRepoSelection` / `renderRepos`）增加内嵌 Sidebar，提供「我的项目」与「项目模板」两个入口；支持从配置化的项目模板一键创建新 GitCode 仓库，并自动加入项目托管、应用对应开发场景。
> 关联文档：`user-workflow-overview.md`（2.1.1 选择仓库页面）、`managed-repos-normalization.md`、`webhook-event-flow.md`

---

## 一、背景与目标

### 1.1 背景

当前首次登录或点击「＋ 新增仓库托管」时，页面以全屏卡片网格展示用户可操作的 GitCode 仓库列表（`repos.js` 的 `renderRepoSelection` / `renderRepos`），页面本身没有任何导航结构。用户无法在「我的仓库」与「系统提供的新项目起点」之间快速切换，也无法从零开始创建符合 DevOps Bot 托管规范的初始项目。

### 1.2 目标

1. 仓库选择/管理页增加内嵌 **Sidebar**，包含两个入口：
   - **我的项目**：展示当前仓库卡片列表（现有页面，逻辑不变）。
   - **项目模板**：按大类展示系统预置的项目模板，提供「一键创建」。
2. 「一键创建」在 **GitCode 个人用户下**创建一个与模板同构的项目，并**自动加入项目托管**。
3. 创建成功后可查看该仓库并进入其 Dashboard，仓库自动应用模板对应的**开发场景**（Agent-Skill 映射）。

### 1.3 非目标

- 不改造已托管状态下的主 Sidebar（Workspace / DevOps Agents 等导航）。
- 不引入 GitCode Fork API 依赖（创建方式为 clone + push，见 5.2）。
- 不实现模板市场 / 用户自定义模板上传（本期仅配置化预置）。
- 模板内容来源支持两种：`local`（后端内置目录）与 `git`（GitCode 源仓库），二者配置化共存（见 4.1）。

---

## 二、现状分析

### 2.1 页面现状

| 项 | 说明 | 位置 |
|----|------|------|
| 首次登录/无托管仓库 | 隐藏主 Sidebar，全屏渲染 `renderRepoSelection` | `src/api/static/js/pages/repos.js:2` |
| 仓库管理页 | 侧边栏「＋ 新增仓库托管」→ `renderRepos` | `src/api/static/js/pages/repos.js:40` |
| 卡片操作 | `joinAndSelect`（加入）/ `hostAndSelect`（加入并托管） | `repos.js:109` 起 |

### 2.2 托管链路（可复用）

`POST /api/repos/host`（`src/api/routes/repos.py:97`）已实现完整托管流程：

```
验证用户身份 & 管理员权限
  → 全局 Bot 作为维护者加入仓库（add_collaborator）
  → 接受邀请（accept_invite_for_repo）
  → 校验 bot 已是维护者（access_level >= 40）
  → 创建/复用 Webhook（setup_webhook / find_existing_webhook）
  → 写入 managed_repos
  → ensure_default_agent_sessions(repo) 生成默认 Agent 会话
```

### 2.3 GitCode API 现状

| 能力 | 现状 | 位置 |
|------|------|------|
| 拉取仓库列表 | ✅ `GET /api/v5/user/repos?type=all` | `src/user/repo_manager.py:217` |
| 通用请求封装 | ✅ `_req(method, url, token, body)` | `repo_manager.py:8` |
| **创建仓库** | ❌ 无 | 需新增 `POST /api/v5/user/repos` |
| **向仓库推送代码** | ❌ 无 | 需新增服务端 clone + push |

### 2.4 场景体系

`configs/agent-skill-mapping.json` 已提供 5 个开发场景（`default` / `cloud-native` / `iac` / `agent-dev` / `skill-dev`），通过 `POST /api/scenes/apply/{owner}/{repo}` 写入 `devops_agent_session` 映射。项目模板的 `scene_id` 直接复用该体系。

---

## 三、功能设计

### 3.1 页面布局（仓库选择页内嵌 Sidebar）

```
┌─ Sidebar ─────────────┐  ┌─ 内容区 ────────────────────────────────┐
│  [Logo] Grape          │  │  我的项目 / 项目模板 的内容               │
│  Multi-Agent DevOps    │  │                                         │
│                        │  │  （我的项目 = 现有仓库卡片列表）          │
│  ▸ 我的项目    ● 激活    │  │                                         │
│  ▸ 项目模板             │  │                                         │
│                        │  │                                         │
│  ↩ Logout              │  │                                         │
└────────────────────────┘  └─────────────────────────────────────────┘
```

- Sidebar 仅出现在仓库选择/管理场景（`renderRepoSelection` 与 `renderRepos`）。
- 默认选中「我的项目」，展示现有内容。
- 点击「项目模板」切换内容区为模板页；再次点「我的项目」返回。
- 选择项以 `localStorage`（`sidebarTab`）记忆，重新进入页面时恢复。

### 3.2 项目模板页

```
┌─ 项目模板 ──────────────────────────────────────────────────┐
│  [Skills 项目模板] [AI office 项目模板] [云原生项目模板] [IaC] │  ← 分类 Tab
├────────────────────────────────────────────────────────────┤
│  Skills 项目模板 · 4 个模板                                   │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │
│  │ 🧩 Hello Skill│ │ 🧩 Tool Kit  │ │ 🧩 ...       │        │
│  │ 描述...       │ │ 描述...      │ │              │        │
│  │ [skill-dev]   │ │ [skill-dev]  │ │              │        │
│  │ [一键创建]    │ │ [一键创建]   │ │              │        │
│  └──────────────┘ └──────────────┘ └──────────────┘        │
└────────────────────────────────────────────────────────────┘
```

- **分类 Tab**：Skills / AI office / 云原生 / IaC 等，数据来自 `configs/project-templates.json`。
- **模板卡片**：图标 + 名称 + 描述 + 场景标签 + `一键创建` 按钮。
- **一键创建交互**：
  1. 弹出确认弹窗，预填仓库名（可修改），展示「将创建到你的 GitCode 个人账号下并自动托管」。
  2. 点击确认 → 按钮进入 loading（`创建中...`）。
  3. 成功 → toast + 跳转该仓库 Dashboard（走 `hostAndSelect` 相同的入库+选择逻辑）。
  4. 失败 → toast 展示错误信息（如 409 同名冲突）。

---

## 四、配置设计

新增 `configs/project-templates.json`：

```json
{
  "categories": [
    {
      "id": "skills",
      "name": "Skills 项目模板",
      "icon": "🧩",
      "description": "面向 Skill 开发与维护的项目起点",
      "scene_id": "skill-dev",
      "templates": [
        {
          "id": "hello-skill",
          "name": "Hello Skill",
          "description": "一个最小的 Skill 项目模板：SKILL.md + 示例资源",
          "icon": "🧩",
          "source": "local",
          "default_repo_name": "hello-skill",
          "tags": ["skill", "starter"],
          "scene_id": "skill-dev"
        }
      ]
    },
    {
      "id": "ai-office",
      "name": "AI office 项目模板",
      "icon": "🤖",
      "description": "AI 办公场景应用模板",
      "scene_id": "default",
      "templates": [
        {
          "id": "ai-assistant",
          "name": "AI Assistant",
          "description": "基于大模型的办公助手项目骨架",
          "icon": "🤖",
          "source": "git",
          "source_repo": "devops-bot/ai-office-assistant",
          "default_repo_name": "ai-assistant",
          "tags": ["ai", "office"],
          "scene_id": "default"
        }
      ]
    },
    {
      "id": "cloud-native",
      "name": "云原生项目模板",
      "icon": "☁️",
      "description": "容器化 / 微服务项目起点",
      "scene_id": "cloud-native",
      "templates": [
        {
          "id": "micro-svc",
          "name": "Micro Service",
          "description": "Go + Docker + Kubernetes 微服务骨架",
          "icon": "☁️",
          "source": "git",
          "source_repo": "devops-bot/cloud-native-microsvc",
          "default_repo_name": "micro-svc",
          "tags": ["golang", "k8s", "docker"],
          "scene_id": "cloud-native"
        }
      ]
    },
    {
      "id": "iac",
      "name": "IaC 项目模板",
      "icon": "🏗️",
      "description": "基础设施即代码项目起点",
      "scene_id": "iac",
      "templates": [
        {
          "id": "tf-infra",
          "name": "Terraform Infra",
          "description": "Terraform 基础设施管理骨架",
          "icon": "🏗️",
          "source": "git",
          "source_repo": "devops-bot/terraform-infra",
          "default_repo_name": "tf-infra",
          "tags": ["terraform", "iac"],
          "scene_id": "iac"
        }
      ]
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| `categories[].id` | 分类唯一标识 |
| `categories[].name` | 分类展示名 |
| `categories[].icon` | 分类图标（emoji） |
| `categories[].description` | 分类简介 |
| `categories[].scene_id` | 该分类默认场景（模板可覆盖） |
| `templates[].id` | 模板唯一标识 |
| `templates[].name` | 模板展示名 |
| `templates[].description` | 模板简介 |
| `templates[].icon` | 模板图标 |
| `templates[].source` | **模板内容来源**：`local`（后端内置目录 `configs/project-templates/{template_id}/`）或 `git`（GitCode 源仓库 clone） |
| `templates[].source_repo` | **模板源仓库** `owner/repo`（仅 `source="git"` 时必填，GitCode 上已存在，服务端 clone 用） |
| `templates[].default_repo_name` | 一键创建时的默认仓库名（用户可在弹窗中修改） |
| `templates[].tags` | 标签，用于展示与搜索 |
| `templates[].scene_id` | 模板开发场景，缺省继承分类 `scene_id` |

> `source="git"` 时创建流程**不使用用户 token 克隆模板**（避免用户 token 越权访问），服务端以**全局 Bot token / 只读令牌**拉取模板仓库（见 5.4）。
> `source="local"` 时模板文件随项目代码提交，随版本管理，无需额外建仓库。

---

## 四·一、模板数据来源与预置流程

### 模板数据分两层

| 层 | 内容 | 来源 | 维护者 |
|----|------|------|--------|
| **元数据层** | 卡片展示 + 创建行为（id/名称/图标/描述/`source`/`source_repo`/`scene_id` 等） | `configs/project-templates.json` | 项目组（代码仓库内维护） |
| **内容层** | 模板实际代码 | 二选一：本地内置目录 / GitCode 源仓库 | 项目组预置 |

### 内容层两种来源

| `source` | 模板文件位置 | 适用场景 | 预置方式 |
|----------|-------------|---------|---------|
| `local` | `configs/project-templates/{template_id}/`（随仓库代码提交） | 起步阶段、模板小而稳定、无需跨仓库维护 | 直接把样例项目目录放入仓库，改配置即可 |
| `git` | GitCode 模板源仓库（`source_repo`） | 模板较大、需独立演进、由运维单独维护 | 先建好模板仓库，再在配置中登记 `source_repo` |

### 预置流程（上线的操作步骤）

```
local 模板（本期可立即启用）
  1. 在代码仓库创建 configs/project-templates/{template_id}/
  2. 放入模板内容（SKILL.md、README、样例代码等）
  3. configs/project-templates.json 登记模板，source="local"
  4. 提交代码即生效

git 模板（源仓库就绪后启用）
  1. 运维在 GitCode 建好模板仓库（如 devops-bot/cloud-native-microsvc）
  2. 确认全局 Bot token 有该仓库只读权限
  3. configs/project-templates.json 登记 source_repo，source="git"
  4. 一键创建时服务端 clone + push
```

> 起步建议：本期至少预置 1 个 `local` 模板（如 hello-skill）保证端到端可跑通；`git` 模板待源仓库建好后逐步补齐。

---

## 五、后端设计

### 5.1 新增 `src/api/routes/templates.py`

```python
router = APIRouter(prefix="/api/project-templates", tags=["project-templates"])

@router.get("")
async def list_templates(user: dict = Depends(get_current_user)):
    """返回配置化的模板分类与模板列表"""
    # 读取 configs/project-templates.json，透出 id/name/icon/description/templates

class CreateTemplateRequest(BaseModel):
    template_id: str
    repo_name: str = ""          # 可选，缺省用 default_repo_name

@router.post("/create")
async def create_from_template(body, user: dict = Depends(get_current_user)):
    """一键创建：建仓库 → 推模板代码 → 自动托管 → 应用场景"""
```

### 5.2 一键创建流程（`POST /api/project-templates/create`）

```
1. 校验
   ├── template_id 在配置中存在，否则 404
   ├── 用户已配置 GitCode Token，否则 400
   └── repo_name 合法（^[a-zA-Z0-9_\-]+$），否则 400

2. 建空仓库（用户个人名下）
   ├── POST /api/v5/user/repos
   │     body: { name, description, private: false, auto_init: false }
   ├── 用「用户个人 token」调用（仓库归个人所有）
   └── 失败 → 502（GitCode 错误信息透传）
       └── 409 同名冲突 → 明确返回「同名仓库已存在，请更换仓库名」

3. 写入模板代码（服务端执行，按 source 分支）
   ├── source="local" → 复制 configs/project-templates/{template_id}/ 到临时目录
   ├── source="git"   → 临时目录 git clone source_repo（全局 Bot token / 只读令牌）
   ├── 设置 remote 指向新仓库（https://{个人token}@gitcode.com/{user}/{repo}.git）
   ├── git push -u origin HEAD:main
   └── 清理临时目录（finally）

4. 自动加入托管（复用 host_repo 全链路）
   ├── 等价调用 POST /api/repos/host { full_name: "{user}/{repo_name}" }
   └── 失败 → 返回「仓库已创建但托管失败」+ 错误信息（仓库不删除，用户可手动托管）

5. 应用开发场景
   ├── POST /api/scenes/apply/{user}/{repo_name} { scene_id }
   └── 使用模板的 scene_id（缺省继承分类）

6. 返回
   {"full_name": "{user}/{repo_name}", "webhook_id": n, "scene_id": "..."}
```

### 5.3 `src/user/repo_manager.py` 新增函数

| 函数 | 说明 |
|------|------|
| `create_repo(api_token, name, description="", private=False) -> dict` | 调 `POST /api/v5/user/repos` 建空仓库，返回仓库信息 |
| `push_template_to_repo(template: dict, target_repo: str, git_token: str) -> bool` | 按模板 `source` 分支取内容（local 复制目录 / git clone），push 到新仓库；内部 `subprocess` 调用 `git`，失败抛 `RuntimeError` |
| `get_user_gitcode_login(api_token) -> str` | 获取当前登录用户名（用于组装新仓库 full_name，复用 `_req("GET", ".../user")`） |

> 依赖 `git` CLI：`source="git"` 时 git clone/push 失败返回 502 并提示服务器未安装 git 或模板仓库不可访问；`source="local"` 时无需联网，仅需本地文件复制与 git push。

### 5.4 安全与权限

- **创建仓库**：用**用户个人 token**（仓库归属用户）。
- **拉取模板**：`source="git"` 时用**全局 Bot token**（`configs/global-bot-token.json` 的 `gitcode_api_token`）或专用只读令牌，**绝不使用用户 token 拉模板**，避免用户 token 越权读取模板仓库；`source="local"` 时读本地配置目录，无外部依赖。
- **推送代码**：目标仓库为刚创建的个人仓库，使用个人 token 完成 push（临时 URL，用完即弃，不落日志）。
- **模板信任**：仅允许 `configs/project-templates.json` 中配置的模板（`source="local"` 限本地目录、`source="git"` 限登记的 `source_repo`），不接收任意用户输入源。
- **幂等**：同名仓库冲突直接返回 409 错误提示，不自动加后缀、不覆盖已有仓库。

### 5.5 接口注册

- `src/api/main.py` 的 `include_router` 追加 `templates.router`。
- 不暴露到 8080 webhook 服务（仅主 API 80 端口）。

---

## 六、前端设计

### 6.1 `src/api/static/js/pages/repos.js` 改造

1. 抽出页面壳：`renderSelectionShell(tab)` 渲染「内嵌 Sidebar + 内容区」容器。
2. `renderRepoSelection(repos)` / `renderRepos()` 渲染为壳内的「我的项目」内容。
3. 新增 `switchSidebarTab(tab)`：`'projects'` ↔ `'templates'`，写 `localStorage.sidebarTab`。
4. `renderRepoSelection` 与 `renderRepos` 的容器类名从 `pageContent` 内部改为壳内子容器（保证现有 `.rgrid`/`.rc` 样式复用）。

### 6.2 新增 `src/api/static/js/pages/templates.js`

```javascript
function renderTemplates() {
  // 1. GET /api/project-templates → 分类 Tab + 模板卡片网格
  // 2. 分类 Tab 切换（默认第一个分类）
  // 3. 每张卡片：图标 / 名称 / 描述 / 场景标签 / [一键创建]
}

function createFromTemplate(templateId, defaultName) {
  // 确认弹窗（可改仓库名）→ 显示“创建中...” → 
  // POST /api/project-templates/create
  //   成功 → toast + 走 hostAndSelect 的入库与跳转逻辑进入新仓库
  //   失败 → toast 错误信息（409 提示更换仓库名）
}
```

- 弹窗样式复用现有 `showModal`/CSS 变量体系。
- 创建成功后复用 `hostAndSelect` 末尾的 `selectRepo`/`localStorage.setItem('currentRepo')` 逻辑跳转新仓库 Dashboard。

### 6.3 `index.html`

- 引入 `<script src="/static/js/pages/templates.js?v=1"></script>`。

---

## 七、幂等与安全

| 场景 | 处理 |
|------|------|
| 同名仓库已存在（GitCode 返回 409） | 返回 409 错误提示，不自动加后缀、不覆盖 |
| 用户未配置 GitCode Token | 400 提示先通过 OAuth 登录或配置个人 Token |
| 仓库名非法 | 400 提示（`^[a-zA-Z0-9_\-]+$`） |
| 模板仓库不可访问 / git 未安装 | 502 提示，创建失败不产生半成品 |
| 仓库已创建但托管失败 | 返回「已创建但托管失败」，仓库保留，用户可手动托管 |
| 场景应用失败 | 不影响创建与托管，仅记录 warning（仓库仍可用） |
| token 泄露 | push URL 中的 token 只用于本次请求，日志脱敏；push 完成后清理临时目录 |

---

## 八、测试计划

### 8.1 单元测试（`tests/test_project_templates.py`）

| 用例 | 断言 |
|------|------|
| `GET /api/project-templates` 返回配置中的分类与模板 | 200，分类数、字段齐全 |
| `configs/project-templates.json` 合法性（id 唯一、`source` 合法、`source="git"` 时 `source_repo` 格式正确、`scene_id` 存在） | 校验通过 |
| `source="local"` 的模板本地目录存在且非空 | 校验通过 |
| 创建：template_id 不存在 | 404 |
| 创建：repo_name 非法字符 | 400 |
| 创建：同名冲突（mock GitCode 409） | 409 错误提示 |
| 创建：成功路径（local 模板，mock 建仓库/push/host/scene） | 200，返回 full_name |
| 创建：成功路径（git 模板，mock clone/push） | 200，返回 full_name |
| 创建：托管失败 | 200（或 502）并给出「已创建但托管失败」提示 |
| 创建：无 token | 400 |

### 8.2 集成/手工测试

- 首次登录进入仓库选择页：确认 Sidebar 两个入口正常切换。
- 已托管用户点「＋ 新增仓库托管」：确认 `renderRepos` 同样带 Sidebar。
- 一键创建真实仓库（local 模板）：个人账号下出现新仓库、Webhook 生效、Dashboard 可进入、Agent 会话场景正确应用。
- `git` 模板：源仓库就绪后验证 clone + push 链路。| 8 | 手工验证端到端流程 |

---

## 九、实施步骤

| 步骤 | 内容 |
|------|------|
| 1 | 新增 `configs/project-templates.json`（预置 Skills / AI office / 云原生 / IaC 四类，含示例模板） |
| 2 | 预置至少 1 个 `local` 模板目录（如 `configs/project-templates/hello-skill/`），保证端到端可跑通 |
| 3 | `repo_manager.py` 新增 `create_repo` / `push_template_to_repo` / `get_user_gitcode_login` |
| 4 | 新增 `src/api/routes/templates.py`（list + create，按 `source` 分支取模板内容） |
| 5 | `main.py` 注册 `templates.router` |
| 6 | `repos.js` 改造：内嵌 Sidebar 壳 + 分类/模板切换 |
| 7 | 新增 `static/js/pages/templates.js` + `index.html` 引入 |
| 8 | 新增 `tests/test_project_templates.py` 并跑通 |
| 9 | 手工验证端到端流程（local 模板） |
| 10 | `git` 模板源仓库就绪后，验证 clone + push 链路 |
