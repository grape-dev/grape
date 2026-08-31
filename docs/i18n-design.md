# 前端 UI 国际化设计方案（最终版）

> **范围声明：当前阶段只做前端页面国际化（JS + HTML + JSON 语言包 + 后端 /en 路由）。**
> **后端 API 返回的中文（错误消息 / DB skill display_name/description / 场景名 / 引擎名 / Issue 标题 / configs 配置数据等）属于二期后端国际化，当前阶段不处理、不修改后端 Python 代码、不修改测试断言。**
>
> **两个特例（前端语言包优先于后端值）：**
> 1. templates 分类名 — 前端 _tmplCatName() 查语言包 cat 翻译，无 key fallback 原名
> 2. dashboard Agent 描述 — descs[at] 语言包优先于 meta.description
>
> **场景下拉框不做前端翻译** — 场景名是后端配置数据，可能新增/改名，前端硬编码翻译会不同步。已回退（git revert），直接用后端 ${s.name}。

---

## 一、设计目标

- 支持中英双语切换，中文默认（不带 URL 前缀），英文加 `/en` 前缀
- setLang 双写 localStorage + cookie（grapeLang=en; max-age=31536000）
- 回退机制：en 缺失 key 回退 zh 包
- 变量插值：`{{name}}` 模板变量
- 代码注释不改，只改用户可见文案
- 界面风格默认 Liquid Glass（apple）
- 语言按钮去掉国旗 emoji：`中文` / `English`

## 二、语言包结构

15 个模块 × 2 语言 = 30 个 JSON 文件，存放在 `src/api/static/locales/{zh,en}/`：

```
common.json      — 通用文案（loading/load_failed/save/cancel/status 枚举等）
app.json         — 导航/侧边栏/登录/仓库选择/搜索
settings.json    — 设置页（tab/credentials/notifications/appearance/pipeline/language）
chat.json        — Chat Agent 页面
agents.json      — Agent 配置/事件历史
dashboard.json   — 仪表盘统计/工作流
events.json      — 事件详情
knowledge.json   — 知识库
skills.json      — 技能管理
runtimes.json    — Agent 运行时
observability.json — 系统监控
pipeline.json    — 流水线
tasks.json       — 任务中心
repos.json       — 仓库选择
templates.json   — 项目模板
```

### JSON 格式

```json
{
  "nav": {
    "dashboard": "仪表盘",
    "pipeline": "流水线"
  },
  "login": {
    "failed": "登录失败：{{msg}}"
  }
}
```

## 三、i18n 核心模块

### 3.1 i18n.js（348 行）

IIFE 模式，暴露 `window.I18N = { t, setLang, getLang, init, detectLang, applyI18nToDOM, SUPPORTED, DEFAULT_LANG }`

**核心函数：**

| 函数 | 说明 |
|------|------|
| `detectLang()` | URL pathname `/en` → en，其他 → zh；URL 无前缀看 localStorage |
| `loadPacks(lang)` | 异步 fetch 15 个 JSON 语言包 |
| `t(key, vars)` | 按点分模块查找 → 回退到默认语言 → `{{name}}` 插值 |
| `setLang(lang)` | 双写 localStorage + cookie → `_applyUrlLang` → reload |
| `_applyUrlLang(lang)` | zh 去掉 /en，en 加 /en |
| `init(cacheVer)` | 用 `_INLINE_PACKS` 同步初始化 → `applyI18nToDOM` → 异步加载完整包 |
| `applyI18nToDOM()` | 设置 `<html lang>` + 替换 `[data-i18n]` / `[data-i18n-title]` / `[data-i18n-placeholder]` |

**内联包 `_INLINE_PACKS`：**

i18n.js 内联了 zh/en 的 common + app.login + app.repo 关键 key，保证同步初始化不闪烁（登录页在 i18n.js 异步加载完整包前就能显示正确语言）。

### 3.2 index.html 引入

```html
<!-- i18n.js 在 app.js 之前同步加载 -->
<script src="/static/js/i18n.js?v=1"></script>
<script src="/static/js/app.js?v=34"></script>
```

28 个 `data-i18n` 属性 + `data-i18n-title` + `data-i18n-placeholder`。

HTML fallback 用中文（默认语言），`applyI18nToDOM` 加载后替换。

### 3.3 代码中使用 t()

```javascript
// 模板字面量里必须用 ${I18N.t()} —— 不能用 ' + I18N.t() + '
const html = `<h2>${I18N.t('dashboard.title')}</h2>`;

// 单引号字符串拼接（合法）
el.innerHTML = '<div>' + I18N.t('common.loading') + '</div>';

// 变量插值
I18N.t('app.login.failed', {msg: e.message});

// 枚举值翻译（有则翻译，无则 fallback 原值）
I18N.t('common.status.' + t.status) !== ('common.status.' + t.status)
  ? I18N.t('common.status.' + t.status) : t.status

// 后端字段优先级：语言包优先于后端返回值
const sceneLabel = _sceneLabels[_sceneId] || p.scene_name || _sceneId;
```

## 四、后端路由调整

### 4.1 /en 前缀路由（main.py）

```python
@app.get("/en")
@app.get("/en/{path:path}")
async def serve_spa_en(path: str = ""):
    """英文国际化路由——返回同一个 index.html，前端 i18n.js 从 URL /en 检测语言。"""
    if path.startswith("api/") or path.startswith("static/"):
        raise HTTPException(status_code=404)
    return _spa_response()
```

### 4.2 OAuth 回调语言前缀（auth.py）

```python
@router.get("/callback/gitcode")
async def gitcode_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    # 从 cookie 读语言偏好
    lang = request.cookies.get("grapeLang", "zh")
    prefix = f"/{lang}" if lang != "zh" else ""
    resp = RedirectResponse(url=f"{prefix}/?token={jwt_access_token}&first_login=...", status_code=302)
```

## 五、设置页国际化配置模块

### 5.1 语言切换

Appearance tab 底部语言切换模块：
- 中文按钮：`中文`（无国旗）
- English 按钮：`English`（无国旗）
- `selectLang(lang)` 调 `I18N.setLang(lang)` → 双写 localStorage + cookie → reload

### 5.2 界面风格默认 Liquid Glass

```javascript
// app.js
function getTheme() { return localStorage.getItem('grapeTheme') || 'apple'; }

// index.html 早期主题脚本
(function(){var t=localStorage.getItem('grapeTheme');if(!t){t='apple';}...})();
```

### 5.3 GitCode Repo 页面 tabs 修复

`renderSettings('gitcode')` 跳过 tabs 标签栏渲染（dev 原有设计缺陷，从第一个版本就有）：

```javascript
if (current !== 'gitcode') {
  html += `<div class="stabs">...tabs...</div>`;
}
```

## 六、语言切换流程

```
用户点"English"按钮
  → I18N.setLang('en')
  → localStorage.setItem('grapeLang', 'en')
  → document.cookie = 'grapeLang=en; max-age=31536000; path=/'
  → _applyUrlLang('en') → window.location = '/en/'
  → 页面 reload
  → i18n.js detectLang() 从 URL /en 检测 → lang='en'
  → init() 用 _INLINE_PACKS 同步初始化
  → applyI18nToDOM() 替换所有 [data-i18n]
  → 异步加载完整 en 语言包
  → applyI18nToDOM() 再次替换
```

## 七、改动清单

### 7.1 后端改动（2 个文件，仅路由层）

| 文件 | 改动 |
|------|------|
| `src/api/main.py` | 新增 `/en` 路由 + `/en/{path}` 404 防护 API |
| `src/api/routes/auth.py` | OAuth callback 加 `request: Request`，读 cookie `grapeLang` 决定 redirect 前缀 |

### 7.2 前端新增文件

| 文件 | 说明 |
|------|------|
| `src/api/static/js/i18n.js` | 348 行 i18n 核心模块 |
| `src/api/static/locales/zh/*.json` | 15 个中文语言包 |
| `src/api/static/locales/en/*.json` | 15 个英文语言包 |
| `docs/i18n-design.md` | 本设计文档 |

### 7.3 前端改动文件（16 个 JS + 1 个 HTML）

| 文件 | 改动内容 |
|------|---------|
| `index.html` | 28 个 data-i18n 属性 + i18n.js 同步加载 + 主题默认 apple + logo title |
| `app.js` | title.textContent / replaceState 保留 /en / 搜索标题 / Admin/Member / getTheme 默认 apple |
| `api.js` | 错误消息 I18N.t() |
| `agents.js` | Configure/Events/Save/Cancel/Agent Configuration/Event History/Skill label/status 翻译 + _agentTypeDisplay/_agentTypeDesc helper |
| `chat.js` | Backend/Skill/App ID/Send/Thinking/placeholder/scope==local 翻译后缀 |
| `dashboard.js` | 统计标签/role 翻译/scene 优先级/模板字面量修复(7处)/desc 语言包优先 |
| `events.js` | 80 个 I18N.t 调用 |
| `knowledge.js` | Knowledge Info/View/Delete/File/Repo label + 模板字面量修复(2处) |
| `observability.js` | 表头/health 状态/status 翻译/CPU/MEM/DISK label + 模板字面量修复(6处) |
| `pipeline.js` | scene_name 优先级 + 模板字面量修复(2处) |
| `repos.js` | Repositories/Hosted/Sync from GitCode/Agentic DevOps |
| `runtimes.js` | Refresh/Worker label/模板字面量修复(5处)/status 翻译 |
| `settings.js` | tabs/label/通知偏好/主题默认 apple/GitCode Repo tabs 修复 |
| `skills.js` | View/Delete/Skill Info/Name/Agent Type/Type label |
| `tasks.js` | 42 个 I18N.t 调用 |
| `templates.js` | 模板分类名用 I18N.t('templates.cat.' + name) 翻译 |

### 7.4 后端未改动（已回退到 dev 状态）

| 文件 | 差异 |
|------|------|
| `src/api/routes/chat.py` | 0 |
| `src/api/routes/dashboard.py` | 0 |
| `src/im/vod_handler_async.py` | 0 |
| `tests/test_chat_routes.py` | 0 |
| `tests/test_vod_navigation_commands.py` | 0 |

## 八、前端国际化检查方法

1. **代码搜索**：`grep -rn '>[A-Z][a-z][^<]*<' src/api/static/js/` 找 HTML 标签间硬编码英文
2. **浏览器渲染**：playwright 渲染每个页面，提取叶子节点文本，找中文模式下的纯英文
3. **语言包对齐**：zh/en key 数量一致，中文包无纯英文值（排除品牌名）
4. **代码残留检测**：浏览器渲染后搜索 "I18N.t" / "' +" / "${" 是否出现在页面上
5. **枚举值翻译**：后端返回的固定枚举（status/health/scope）用 `I18N.t('module.' + value)` 翻译

## 九、国际化过程错误总结与经验教训

### 犯过的错误

1. **模板字面量里的字符串拼接（最严重）**
   - 错误：在反引号模板字面量里用 `' + I18N.t('key') + '` 拼接
   - 结果：页面上直接显示代码文本而不是翻译值
   - 正确：模板字面量里必须用 `${I18N.t('key')}`
   - 影响范围：23 处

2. **超范围改后端**
   - 错误：把 chat.py / dashboard.py / vod_handler_async.py 的中文硬编码改成英文
   - 教训：后端 API 返回的中文属于二期，前端阶段不处理

3. **回退后端改动时误删前端 key**
   - 错误：`git checkout dev -- chat.json` 把回退前补充的 8 个前端 key 也删了
   - 教训：回退后端文件时要排除前端语言包文件

4. **浏览器验证白名单过宽**
   - 错误：把 Account/Save/Skill/Backend 等需要翻译的 UI 文案放进了白名单
   - 教训：白名单只放产品专有名词（VOD Agent / GitCode / hermes 等）

5. **代码搜索比浏览器验证更彻底**
   - 浏览器只取叶子节点文本，有些 `<th>` / `<label>` 里的短英文被白名单排除
   - 教训：代码搜索 + 浏览器双重验证

### 遗漏检查轮次

| 轮次 | 发现的问题 | 数量 |
|------|-----------|:---:|
| 第 1 轮 | 模板字面量字符串拼接 | 23 处 |
| 第 2 轮 | HTML 标签间硬编码英文（Notification Preferences/Issue events 等） | 15 处 |
| 第 3 轮 | 按钮/label 硬编码（Refresh/View/Delete/API Token/Account 等） | 8 处 |
| 第 4 轮 | 枚举状态值直接渲染（completed/failed/running 等） | 5 处 |
| 第 5 轮 | 项目模板分类名（后端配置数据，前端用 name 做 key 翻译） | 3 处 |
| 第 6 轮 | chat.json 回退丢失 key + Chat 页面欢迎语 | 8 key |

## 十、二期后端国际化问题清单（不在本阶段处理）

| # | 文件/来源 | 中文内容 | 说明 |
|---|----------|---------|------|
| 1 | configs/project-templates.json | category name: "Hello World 新手模板" 等 | 后端配置文件（前端已用 name 做 key 翻译） |
| 2 | configs/project-templates.json | template name: "小红书自动化运营助手" 等 | 后端配置文件 |
| 3 | configs/project-templates.json | template description: "极简 Flask Web 应用..." 等 | 后端配置文件 |
| 4 | DB skill 表 | display_name: "AI 博客生成"/"IaC 代码实现" 等 | 后端数据 |
| 5 | DB skill 表 | description: "AI 博客生成：学习参考..." 等 | 后端数据 |
| 6 | DB agent_type 表 | display_name + description | 后端数据（前端已用 agent_type 做 key 翻译） |
| 7 | chat.py | API 错误消息（"无可用引擎"/"对话未找到" 等） | 后端硬编码 |
| 8 | chat.py | display_name 拼接"（本机直连）" | 后端硬编码（前端已用 scope==local 加翻译后缀） |
| 9 | dashboard.py | device_name "本机直连" 等 | 后端硬编码 |
| 10 | vod_handler_async.py | 错误消息"无可用引擎" | 后端硬编码 |
| 11 | chat.py | stream 消息"思考中"/"生成中"/"后端错误" 等 | 后端硬编码 |

## 十一、验证状态

| 验证项 | 结果 |
|--------|:---:|
| 16 个 JS 语法验证 | ✅ 全部通过 |
| 后端语法验证（main.py + auth.py） | ✅ 通过 |
| 后端和 dev 差异（chat.py/dashboard.py 等） | 0（完全回退） |
| 全量测试 | 71 failed / 1229 passed（和 dev 基线一致，0 新增） |
| hermes verify | 71 failed / 1229 passed |
| 浏览器 19 个页面验证（中英文） | 0 个代码残留 |
| 语言包 key 对齐（15 模块） | ✅ 全部对齐 |
| JS I18N.t key 覆盖 | 780+ 个 key 全部在中文包里 |
| index.html fallback 一致性 | ✅ 全部一致 |

### 补充：后端动态返回的中文文案（二期，前端代码中不存在）

以下文案用户在前端页面可见，但前端 JS 代码中全局搜索 0 匹配——确认是后端 API 动态返回的数据，非前端遗漏：

| # | 文案 | 来源 | 说明 |
|---|------|------|------|
| 12 | "本机 Runtime" | 后端 API 返回的 worker/engine 名称 | 前端搜索 0 匹配 |
| 13 | "请在任务中心查看并处理" | 后端返回的任务提示消息 | 前端搜索 0 匹配 |

验证方法：`search_files(pattern="本机 Runtime|请在任务中心", path="src/api/static/js")` → 0 结果

## 十二、补漏：i18n 合并后遗漏的硬编码文案（fix/i18n-template-literal-bug 分支）

合并 dev 后深度扫描发现部分页面仍有硬编码中文/英文未做 I18N.t()，以下为补漏清单及实施方案。

### 改动范围（仅前端 JS + JSON 语言包，不改后端 Python / 不改测试）

| 文件 | 遗漏数 | 内容 |
|------|--------|------|
| dashboard.js | 47 | 场景编辑/创建/引擎选择弹窗全功能块 + 统计标签 + Hero 副标题 |
| templates.js | 33 | 模板库标题/搜索/分页/创建弹窗/收藏/精选推荐 + Toast |
| tasks.js | 6 | 场景标签对象 + 多任务/单任务徽章 + 父任务 + 流水线场景/任务模式 label |
| events.js | 2 | '（无回复）' / '（错误）' fallback |
| repos.js | 2 | '工作台' tab + '暂无已托管项目' Toast |
| observability.js | 1 | '主控' 徽章 |
| agents.js | 1 | 'Actor' 表头 |
| skills.js | 2 | 'Description' label (L298 + L391) |
| chat.js | 0 | '思考中'/'已被' 属后端返回文本匹配，二期不改 |

### 语言包新增 key 规划

dashboard.json 新增：
- scene_center_title, create_custom_scene, edit_scene, apply_scene, config_engine, cancel_edit, cancel_config, save_engine
- scene_applied, apply_failed, engine_config_saved, save_failed, please_select_repo, please_input_scene_name, scene_saved, scene_created
- select_engine, engines_available, no_engine_broadcast, online, confirm, cancel
- edit_custom_scene, save_custom_scene, scene_name, scene_name_placeholder, desc_optional, desc_placeholder, visibility, scope_user, scope_repo
- select_skill, change_engine, engine_count, broadcast, not_needed_in_scene
- hero_maintainer, hero_member, stat_active, stat_events, stat_failed, label_skill, label_engine
- desc_vod/issue/coding/qa/sre 已有

templates.json 新增：
- fav_cat, fav_cat_desc, all_cat, all_cat_desc (已有 all/all_desc)
- featured_title, loading_top1000, page_count
- gallery_title 已有, create_from_template 已有

tasks.json 新增：
- scene_label, scene_default_badge, multi_task_badge, single_task_badge, parent_task, pipeline_scene, task_mode

events.json 新增：
- no_reply, error_reply

repos.json 新增：
- tab_workspace, no_hosted_project

observability.json 新增：
- main_tag

agents.json 新增：
- event_table_actor

skills.json 新增：
- info_description (已有则复用)

### 实施顺序
1. 先更新 zh + en 语言包 JSON（新增 key）
2. dashboard.js — 最大改动，模板字面量 + 单引号拼接混合
3. templates.js — 单引号字符串拼接为主
4. tasks.js / events.js / repos.js / observability.js / agents.js / skills.js — 少量改动
5. 逐文件 node --check 语法验证
6. Playwright 中英双语渲染验证 0 泄露

## 十三、二期后端国际化清单（当前阶段不处理）

> **当前阶段只做前端页面国际化。以下后端返回的中文数据前端直接渲染，无法翻译，统一放二期。**

### 前端直接渲染后端中文数据（42处）

| 文件 | 处数 | 字段 | 数据来源 | 说明 |
|------|------|------|----------|------|
| skills.js | 13 | s.name/s.description/sub.name | DB skill 表 | 用户上传的技能名/描述/子技能名 |
| observability.js | 5 | rti.name/iss.title/k.label | 引擎实例名/Issue标题/日志标签 | 后端运行时数据 |
| runtimes.js | 5 | rt.name/s.name | 引擎名/技能名 | 后端运行时数据 |
| dashboard.js | 3 | s.name/s.skill_name/s.display_name | 场景名/技能下拉框 | 后端配置/DB 数据 |
| knowledge.js | 4 | doc.name/f.label | 文档名/文件标签 | 后端知识库数据 |
| repos.js | 3 | t.name/r.name | 模板名/仓库名 | 后端配置/GitCode 数据 |
| app.js | 2 | a.name/a.display_name | Agent 名称 | 后端 API 返回 |
| agents.js | 2 | r.name/repo.name | 仓库名 | 后端数据 |
| pipeline.js | 2 | displayPipe.title | 流水线标题 | 后端 Issue 标题 |
| chat.js | 1 | b.name | 后端引擎名 | 后端 API 返回 |
| settings.js | 1 | t.label | 配置标签 | 后端配置数据 |
| templates.js | 1 | cache.error | API 错误消息 | 后端返回的错误文本 |

### 后端 Python 硬编码中文（API 返回给前端）

| 文件 | 处数 | 内容类型 |
|------|------|----------|
| tasks.py | 83 | 任务状态消息/错误消息 |
| db_setup.py | 82 | 建表注释/默认数据 |
| event_pipeline.py | 78 | 事件处理消息 |
| chat.py | 78 | 聊天错误消息/流消息（"思考中"/"生成中"等） |
| skills.py | 66 | 技能操作消息 |
| repos.py | 58 | 仓库操作消息 |
| settings.py | 54 | 配置操作消息 |
| events.py | 51 | 事件消息 |
| knowledge.py | 51 | 知识库消息 |
| runtime.py | 47 | 运行时消息 |
| knowledge.py (非routes) | 42 | 知识库处理逻辑 |
| jwt_utils.py | 30 | 认证消息 |
| onboarding.py | 30 | 引导消息 |
| event_store.py | 29 | 事件存储消息 |
| feishu_onboard.py | 27 | 飞书引导消息 |

### 二期处理原则

1. **后端 API 返回的中文** — 后端做国际化（Accept-Language 头或 /en API 前缀），前端不改
2. **DB 动态数据**（skill name/description、scene name）— DB 存双语字段或后端翻译
3. **configs 配置数据**（project-templates.json 分类名/模板名）— 配置文件加双语字段
4. **两个已做前端翻译的特例保持不变**：
   - templates 分类名 _tmplCatName()（语言包 cat 翻译）
   - dashboard Agent 描述 descs[at]（语言包优先于 meta.description）
5. **场景下拉框不做前端翻译** — 已回退，直接用后端 ${s.name}
