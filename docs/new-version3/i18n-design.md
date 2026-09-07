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
| `setLang(lang)` | 双写 localStorage + cookie → `_updateUrlLang` (replaceState 不刷新) → loadPacks → applyI18nToDOM → `_rerenderCurrentPage` |
| `_updateUrlLang(lang)` | zh 去掉 /en，en 加 /en（replaceState，不触发页面刷新）|
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
- `selectLang(lang)` 调 `I18N.setLang(lang)` → 双写 localStorage + cookie → replaceState 更新 URL 前缀（不刷新页面）→ loadPacks 异步加载 → applyI18nToDOM + _rerenderCurrentPage 原地重渲染

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
  → _updateUrlLang('en') → history.replaceState（不刷新页面，URL 变为 /en/）
  → loadPacks('en') 异步加载英文语言包
  → applyI18nToDOM() 替换所有 [data-i18n]
  → _rerenderCurrentPage() 原地重渲染当前页面（保留 settings 子 tab 状态）
  → 无页面 reload，无状态丢失
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
6. **tasks.js 场景名不做前端翻译** — 已回退，直接用后端 t.scene_name || t.scene_id

## 十四、最终验证报告（第一轮，2026-08-31）

### 验证范围

| 模式 | 场景数 | 覆盖内容 |
|------|--------|----------|
| 中文 | 20 | 11个主页面 + repos 3个子tab + settings 4个子tab + dashboard 2个交互模式 |
| 英文 | 25 | 同上 + dashboard 引擎弹窗 + observability 错误日志 + templates 创建弹窗 + runtimes 技能详情 |
| 登录页 | 2 | 中英文各1 |

### 验证结果（第一轮，第二轮补漏后详见第十五章）

| 检查项 | 结果 |
|--------|:---:|
| I18N.t 字面量泄露 | 0 处 |
| 模板字面量内 '+ I18N.t() +' bug | 0 处（已修复 observability.js:606） |
| 单引号字符串内嵌 I18N.t() 不拼接 | 0 处 |
| 前端 JS 硬编码中文 | 0 处（仅剩3处后端文本匹配/注释，二期不改） |
| 硬编码英文 UI 文案（第一轮） | 0 处 |
| 硬编码英文 UI 文案（第二轮发现并修复） | 5 处 → 0 处（详见 15.2） |
| 中文包纯英文值（第一轮） | 0 处 |
| 中文包纯英文值（第二轮发现并修复） | 7 处 → 0 处（详见 15.1） |
| 语言包 zh/en key 对齐 | 全部对齐（15个模块） |
| 语言包 JSON 合法性 | 全部合法 |
| node --check 语法 | 15个 JS 文件全部 OK |
| 语言切换不刷新页面 | 已验证（原地重渲染 + URL 前缀更新） |
| settings tab 状态保留 | 已验证（4个子tab切换语言后均保留） |
| F5 刷新语言保持 | 已验证 |
| 前进后退正常 | 已验证 |
| 后端测试回归 | 0 新增 failed（7 failed 均为预先存在） |

### 英文模式中文残留（全部是后端动态数据，二期）

| 页面 | 残留内容 | 数据来源 |
|------|----------|----------|
| chat | "你好" 欢迎语 | 后端返回 |
| skills | "对话助手"/"IM对话处理"/"IM通知" | DB skill 表 |
| repos-templates | "静态网页模板"/"Python小游戏合集" | configs/project-templates.json |

### 与 dev（上个版本）的差异

改动文件 27 个，+468/-151 行：

**JS 代码（11个文件）**：
- i18n.js — setLang 改为不刷新页面（replaceState + 原地重渲染），settings tab 状态保留
- observability.js — L606 模板字面量 bug 修复，L276 "主控"徽章国际化
- dashboard.js — 47处硬编码中文→I18N.t()，Agent 描述语言包优先，场景下拉框回退
- templates.js — 33处硬编码中文→I18N.t()，分类名 _tmplCatName() 前端翻译
- tasks.js — 6处硬编码中文→I18N.t()，场景名语言包 fallback 回退
- events.js — 2处 "（无回复）"/"（错误）" → I18N.t()
- repos.js — 2处 "工作台"/"暂无已托管项目" → I18N.t()
- agents.js — 1处 "Actor" 表头 → I18N.t()
- skills.js — 2处 "Description" label → I18N.t()

**语言包 JSON（16个文件，zh+en 各8个）**：
- dashboard.json +43 key、templates.json +10 key、tasks.json +6 key
- events.json +2 key、repos.json +2 key、observability.json +1 key
- agents.json +1 key、skills.json +1 key

**设计文档（1个文件）**：
- docs/i18n-design.md +121行：范围声明强化 + 第十二章补漏清单 + 第十三章二期清单 + 第十四章验证报告

## 十五、第二轮补漏：zh 语言包英文值 + JS 硬编码英文 UI 文案（2026-08-31，已实施）

> **全面浏览器验证发现两类遗漏：(A) 中文语言包里 7 个 key 值写成了英文；(B) 5 处 JS 代码里硬编码英文 UI 文案未走 I18N.t()。**
> **全部已修复并验证通过。**

### 15.1 已修复：zh/dashboard.json 7 个英文值 → 中文

| key | 旧值（英文） | 新值（中文） | 代码引用位置 |
|-----|-------------|-------------|-------------|
| `label_skill` | Skill | 技能 | dashboard.js:463 `${I18N.t('dashboard.label_skill')}` |
| `label_engine` | Engine | 引擎 | dashboard.js:467 `${I18N.t('dashboard.label_engine')}` |
| `hero_maintainer` | Maintainer | 维护者 | dashboard.js:87-88 `${I18N.t('dashboard.hero_maintainer')}` |
| `hero_member` | Member | 成员 | dashboard.js:87-88 `${I18N.t('dashboard.hero_member')}` |
| `stat_active` | Active | 活跃 | dashboard.js:91 `${I18N.t('dashboard.stat_active')}` |
| `stat_events` | Events | 事件 | dashboard.js:92 `${I18N.t('dashboard.stat_events')}` |
| `stat_failed` | Failed | 失败 | dashboard.js:93 `${I18N.t('dashboard.stat_failed')}` |

**根因：** 第一轮改造时 zh 包直接复制了 en 值，未翻译成中文。JS 代码已走 I18N.t()，仅语言包值错误。

**附带修复：** dashboard.js:87 角色徽章 `${role}` 直接输出后端值 "Maintainer"，改为 `${role === 'Maintainer' ? I18N.t('dashboard.hero_maintainer') : I18N.t('dashboard.hero_member')}`（第 88 行已有相同判断逻辑，复用）。

### 15.2 已修复：5 处 JS 硬编码英文 UI 文案 → I18N.t()

#### (1) pipeline.js:48 + 156 — 统计标签 Active / Today / Failed

**位置：** 流水线页顶部 3 个统计数字下方的标签（2 处，模板字面量直接内容）

```javascript
// 修复前：
<div class="l">Active</div>
<div class="l">Today</div>
<div class="l">Failed</div>

// 修复后：
<div class="l">${I18N.t('pipeline.stat_active')}</div>
<div class="l">${I18N.t('pipeline.stat_today')}</div>
<div class="l">${I18N.t('pipeline.stat_failed')}</div>
```

新增 key：`pipeline.stat_active`（活跃/Active）、`pipeline.stat_today`（今日/Today）、`pipeline.stat_failed`（失败/Failed）

#### (2) agents.js:200 — 表头 Seq

**位置：** Agent 事件历史表格第 1 列表头（模板字面量）

```javascript
// 修复前：
<th style="...">Seq</th>
// 修复后：
<th style="...">${I18N.t('agents.event_table_seq')}</th>
```

新增 key：`agents.event_table_seq`（序号/Seq）

#### (3) agents.js:250 — 标签 Backend Engines

**位置：** Agent 配置面板的引擎池下拉标签（模板字面量）

```javascript
// 修复前：
<label>Backend Engines<br>...</label>
// 修复后：
<label>${I18N.t('agents.backend_engines_label')}<br>...</label>
```

新增 key：`agents.backend_engines_label`（后端引擎/Backend Engines）

#### (4) observability.js:909 + 1109 + 580 — 表头 Seq / Agent / Skill / Issue / Seq / Exit

**位置 909：** 事件列表表格表头（单引号字符串拼接）
**位置 1109：** 任务列表表格表头（单引号字符串拼接）
**位置 580：** 异常任务表格表头（数组传参）

```javascript
// L909 修复前：
'<table...><thead><tr><th>Seq</th><th>' + I18N.t('observability.col_event') + '</th><th>Agent</th>...<th>Skill</th>...'
// L909 修复后：
'<table...><thead><tr><th>' + I18N.t('observability.col_seq') + '</th><th>' + I18N.t('observability.col_event') + '</th><th>' + I18N.t('observability.col_agent') + '</th>...<th>' + I18N.t('observability.col_skill') + '</th>...'

// L1109 修复前：
'<table...><thead><tr><th>Issue / Seq</th><th>Agent</th>...<th>Exit</th>...'
// L1109 修复后：
'<table...><thead><tr><th>' + I18N.t('observability.col_issue_seq') + '</th><th>' + I18N.t('observability.col_agent') + '</th>...<th>' + I18N.t('observability.col_exit') + '</th>...'

// L580 修复前：
table(['Issue/Seq', 'Agent', 'Repo', 'Failed', I18N.t('observability.col_result')], ...)
// L580 修复后：
table([I18N.t('observability.col_issue_seq'), I18N.t('observability.col_agent'), I18N.t('observability.col_repo'), I18N.t('observability.col_failed_short'), I18N.t('observability.col_result')], ...)
```

新增 key：`observability.col_seq`（序号/Seq）、`observability.col_agent`（Agent/Agent）、`observability.col_skill`（技能/Skill）、`observability.col_issue_seq`（Issue / 序号 / Issue / Seq）、`observability.col_failed_short`（失败/Failed）
复用已有 key：`observability.col_exit`（退出码/Exit）、`observability.col_repo`（仓库/Repo）

#### (5) skills.js:82 — 分组标题后缀 "Skills"

**位置：** 技能列表按 Agent 类型分组，每组标题（模板字面量）

```javascript
// 修复前：
<h3 ...>${ico} ${label} Skills</h3>
// 修复后：
<h3 ...>${ico} ${label} ${I18N.t('skills.group_skills_suffix')}</h3>
```

新增 key：`skills.group_skills_suffix`（技能/Skills）

### 15.3 语言包新增 key 汇总（11 个，比规划多 1 个）

| 模块 | 新增 key | zh 值 | en 值 | 说明 |
|------|---------|------|-------|------|
| pipeline.json | `stat_active` | 活跃 | Active | 统计卡片标签（无冒号） |
| pipeline.json | `stat_today` | 今日 | Today | 统计卡片标签（无冒号） |
| pipeline.json | `stat_failed` | 失败 | Failed | 统计卡片标签（无冒号） |
| agents.json | `event_table_seq` | 序号 | Seq | 事件表头（注意：实际 key 名为 event_table_seq，非 event.table_seq） |
| agents.json | `backend_engines_label` | 后端引擎 | Backend Engines | 配置面板标签（实际 key 名为 backend_engines_label，非 engine.backend_engines_label） |
| observability.json | `col_seq` | 序号 | Seq | 事件列表表头 |
| observability.json | `col_agent` | Agent | Agent | 事件列表表头（品牌名，中英文一致） |
| observability.json | `col_skill` | 技能 | Skill | 事件列表表头 |
| observability.json | `col_issue_seq` | Issue / 序号 | Issue / Seq | 任务列表表头（统一带空格格式） |
| observability.json | `col_failed_short` | 失败 | Failed | 异常任务表头（规划时遗漏，实施时发现补充） |
| skills.json | `group_skills_suffix` | 技能 | Skills | 分组标题后缀 |

> **与规划差异：** (1) agents.json 的 2 个 key 实际放在顶层而非嵌套在 event/engine 下（key 名 `event_table_seq` / `backend_engines_label`，非 `event.table_seq` / `engine.backend_engines_label`），因为 agents.json 的 event/engine 是嵌套对象，顶层 key 更简单且和已有 `table_actor` 一致。(2) observability.json 额外新增了 `col_failed_short`（L580 异常任务表头的 "Failed" 列），规划时遗漏。

### 15.4 不做翻译的项（确认保留英文）

| 项 | 位置 | 原因 |
|----|------|------|
| Feishu / WeCom / DingTalk | chat.js:152-154 | IM 平台品牌名 |
| English | settings/appearance 语言选项 | 语言名，中文模式显示 "English" 正确 |
| Grape Default | settings/appearance 主题名 | 产品品牌名 |
| Agent 名称 (VOD Agent 等) | app.nav.* / agents.type.*.name | 品牌名，已确认保留 |
| App ID / App Secret / Bot App ID / Bot Secret | chat.js / vod.js | 技术术语 |
| IP / CPU / TMP | runtimes / observability | 标准缩写 |
| IaC / API Skill | pipeline.json | 枚举标签 |
| 运行时实例名 (ecs-* / worker-* / hermes v*) | runtimes / observability | 后端动态数据 |
| 仓库名/用户名 (gcw_IEzSI05D / PracticeHub) | repos | 后端 API 返回 |

### 15.5 实施记录

| 步骤 | 状态 |
|------|------|
| 1. 更新 zh + en 语言包 JSON（新增 11 个 key） | ✅ 8 个 JSON 文件全部合法 |
| 2. pipeline.js — 2 处统计标签（L48 + L156） | ✅ 模板字面量 `${I18N.t()}` |
| 3. agents.js — 2 处（L200 表头 + L250 标签） | ✅ 模板字面量 `${I18N.t()}` |
| 4. observability.js — 3 处（L909 + L1109 + L580） | ✅ 单引号字符串拼接 |
| 5. skills.js — 1 处（L82） | ✅ 模板字面量 `${I18N.t()}` |
| 6. 逐文件 `node --check` 语法验证 | ✅ 5 个 JS 文件全部通过 |
| 7. Playwright 中英双语渲染验证 | ✅ 0 硬编码英文残留（详见 15.6） |

### 15.6 浏览器验证结果

| 检查项 | 中文模式 | 英文模式 |
|--------|---------|---------|
| pipe 统计标签 | 活跃 / 今日 / 失败 ✓ | Active / Today / Failed ✓ |
| vod 事件表头 | 序号 / 时间 / 内容 / Issue / Actor / 耗时 / 引擎 / 状态 ✓ | Seq / Time / Content / Issue / Actor / Duration / Engine / Status ✓ |
| vod 配置面板标签 | 后端引擎 ✓ | Backend Engines ✓ |
| skills 分组标题 | ◈ Chat Agent 技能 / 💻 Coding Agent 技能 等 ✓ | ◈ Chat Agent Skills / 💻 Coding Agent Skills 等 ✓ |
| I18N.t 代码泄露 | 0 处 ✓ | 0 处 ✓ |
| 控制台错误 | 0 ✓ | 0 ✓ |

### 15.7 settings.json 2 个英文值（不改）

| key | 当前值 | 应改为 | 不改原因 |
|-----|-------|-------|---------|
| `credentials.access_key` | Access Key | 访问密钥 | 用户确认不动 |
| `credentials.secret_key` | Secret Key | 秘密密钥 | 用户确认不动 |

### 15.8 后端测试无回归

`pytest tests/test_api_routes.py`：76 passed / 7 failed。7 个 failed 已用 `git stash` 验证为预先存在（与本次改动无关，均为 chat config / skills 操作相关测试）。本次改动 0 新增 failed。

## 十六、模块顶层缓存 I18N.t() 陷阱（2026-09-04 修复）

### 16.1 问题描述

i18n 语言包是**异步加载**的（`loadPacks()` 返回 Promise），但部分 JS 文件在**模块加载时（同步阶段）**就用 `const` 缓存了 `I18N.t()` 的返回值。这导致切换语言后这些缓存值不会更新，页面上的标签仍显示旧语言。

**时序分析：**

```
1. 浏览器加载 xxx.js → 执行 const FOO = I18N.t('module.key') || '中文兜底'
2. 此时语言包还没加载完 → I18N.t() 找不到 key → 返回 key 本身 'module.key'
3. 如果有 || '中文兜底' → 兜底中文生效 → FOO 被永久冻结成中文
4. 用户切换语言 → setLang() 重新加载语言包 + 调 _rerenderCurrentPage() → app.nav() 重渲染
5. 渲染代码使用 FOO → 但这个 const 从步骤1就固化了，永远不会重新求值
```

**根因：** `setLang()` 能让 `I18N.t()` 在后续调用时返回正确翻译，但无法让已经求值过的 `const` 重新计算。

### 16.2 规则

**禁止在模块顶层用 `const`/`let`/`var` 缓存 `I18N.t()` 的返回值。** 必须在函数内部调用 `I18N.t()`，确保每次渲染时实时取值。

正确写法：
```javascript
// 方式1：函数封装，每次调用实时取值
function _scopeName(scope) {
  return { official: I18N.t('skills.scope_official'),
           repo: I18N.t('skills.scope_repo'),
           user: I18N.t('skills.scope_user') }[scope];
}

// 方式2：直接内联在渲染函数中
const html = `<a>${I18N.t('skills.scope_official')}</a>`;
```

错误写法：
```javascript
// ❌ 模块顶层缓存 — 脚本加载时求值一次后固化
const _SCOPE_NAMES = {
  official: I18N.t('skills.scope_official'),
  repo: I18N.t('skills.scope_repo'),
};
```

### 16.3 受影响文件及修复

| # | 文件:行 | 变量 | 影响 | 修复方式 |
|---|---------|------|------|----------|
| 1 | `skills.js:24-28` | `_SCOPE_NAMES` | 技能市场三个 tab 标签不跟随语言切换 | 改为函数 `_scopeName(scope)` |
| 2 | `chat.js:24` | `_NO_REPLY` | 聊天页"暂无回复"占位文案不切换 | 改为内联 `I18N.t('chat.no_reply')` |
| 3 | `pipeline.js:6` | `agentLabels` | 流水线表格 Agent 列首字母不切换 | 改为函数 `_agentLabel(idx)` |
| 4 | `pipeline.js:8` | `stageLabels` | 流水线表格阶段名不切换 | 改为函数 `_stageLabel(stage)` |
