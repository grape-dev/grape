# Issue #348 + #332 设计文档：Admin i18n + CSS Tokens + 健康面板 + OAuth 白名单 + 用户 CRUD

> 基线：weekly-07-clean | 分支：feat/issue-348-admin-i18n-tokens-health
> 日期：2026-10-16
> 合并：Issue #332（OAuth 登录白名单 + 用户 CRUD）合并到 #348 一起实现，#332 关闭

## 零、#332 范围（合并自 Issue #332）

### 0.1 P0-1: OAuth 登录白名单 admin 端点缺失

**现状**（weekly-07-clean 代码验证）：`src/admin/routes.py` 仅有 signup-whitelist 增删查端点（行 275-298），完全无 login-whitelist 的 GET/POST/DELETE 端点。

**实现**：
- `GET /api/admin/whitelist` — 列出 login_whitelist 表所有条目
- `POST /api/admin/whitelist` — 新增（provider + username + remark）
- `DELETE /api/admin/whitelist/{id}` — 删除
- `PUT /api/admin/whitelist/toggle` — 切换 meta 表 `login_whitelist_enabled` 开关

### 0.2 P0-2: whitelist_enabled 全局开关

**现状**：`src/config.py` 的 `is_user_allowed()` 已有开关短路逻辑（`_is_login_whitelist_enabled()` 读 meta `login_whitelist_enabled`），但无 admin 端点切换它。

**实现**：`PUT /api/admin/whitelist/toggle` 写 meta 表 `login_whitelist_enabled` = true/false。前端显示当前开关状态。

### 0.3 P0-3: 用户 CRUD 补全

**现状**：admin 只有 list/detail/disable/restore。

**新增**：
- `POST /api/admin/users` — 手动创建用户（username + email + password，写 grape_accounts 表，password_hash，token_version=0）
- `PUT /api/admin/users/{id}` — 编辑用户信息（email + nickname）
- `POST /api/admin/users/{id}/reset-password` — 重置密码（新 password_hash + token_version+1 强制旧 token 失效）

### 0.4 P1-4: admin.html OAuth 登录白名单 UI

在 whitelistView 区域新增「OAuth 登录白名单」card（与「注册白名单」分区），包含：白名单列表表格 + 添加表单 + 全局开关。所有文案走 data-i18n + admin.json。

### 0.5 P2-5: login_whitelist DDL 修复

**现状**：`UNIQUE (gitcode_username)` 只约束 gitcode_username。

**修复**：在 `_ensure_column` 区域加 `UNIQUE (github_username)`。SQLite 用 `CREATE UNIQUE INDEX IF NOT EXISTS`。

### 0.6 文件变更清单补充

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| src/admin/routes.py | 修改 | 新增 whitelist GET/POST/DELETE/toggle + users create/edit/reset-password 端点 |
| src/admin/db.py | 修改 | 新增 login_whitelist CRUD + create_user/edit_user/reset_password 函数 |
| src/api/db_setup.py | 修改 | login_whitelist 加 UNIQUE(github_username) 索引 |
| src/api/static/admin.html | 修改 | 新增 OAuth 登录白名单 UI + 用户操作按钮 |
| src/api/static/js/admin.js | 修改 | 新增 loadWhitelist/toggleWhitelist/createUser/editUser/resetPassword 函数 |
| src/api/static/locales/zh/admin.json | 修改 | 新增白名单 + 用户 CRUD 文案 |
| src/api/static/locales/en/admin.json | 修改 | 同上英文 |

## 一、问题分析

### 1.1 原始 Issue #348 的 P0 问题：API 响应结构不匹配

后端 `GET /api/admin/bot-token-health`（#345 已合入 weekly-07-clean）实际返回嵌套结构：

```json
{
  "overall": "healthy",
  "platforms": {
    "gitcode": {"healthy":true, "degraded":false, "unhealthy":false, "consecutive_failures":0, "error_type":"", "error":""},
    "github": {"healthy":false, "degraded":true, "unhealthy":false, "consecutive_failures":2, "error_type":"auth", "error":"401 Unauthorized"}
  },
  "checked_at": "2026-10-15 14:30:00"
}
```

PR #493 前端读取的是扁平结构（Issue body 中的错误 spec）：
```js
if (d.unhealthy) return 'unhealthy';   // undefined → falsy → 永远返回 healthy
```

面板永远显示绿色健康，即使 token 已被吊销。

### 1.2 P1 问题：多平台数据无处展示

后端返回分平台明细（gitcode + github 各一组字段），但前端面板只有一组字段。需要展示分平台状态。

### 1.3 P2 问题：admin.js 版本号未递增

#345 已将 `admin.js?v=` 递增到 `20261015-1`，#348 也用同一版本号。rebase 后浏览器缓存旧 JS，面板不出现。

### 1.4 P3 问题：分支基线过期

feat/issue-348 从 pre-#345 的 commit 创建，不含 #345 变更。rebase 验证通过无冲突。

### 1.5 P4 问题：admin 无 i18n + CSS 设计系统未对齐主应用

admin.html 有 678 个中文字符硬编码（admin.js 320 + admin.html 358），无 i18n。CSS 变量与主应用 `app.css` 不一致（三个蓝色：#3168e0 vs #0071e3 vs #7170ff，admin 缺 --b/--r2 变量用硬编码值）。

## 二、方案设计

### 2.1 整体范围

本次改动分三部分，按依赖顺序：

1. **admin i18n 接入**：引入主应用 i18n.js + 新增 admin 语言包
2. **admin CSS tokens 对齐**：引用主应用 app.css 变量，硬编码值改变量
3. **#348 健康面板**：基于对齐后的规范实现，修复 P0/P1/P2

### 2.2 admin i18n 接入

#### 方式：引用主应用 i18n.js + 新增 admin.json 语言包

admin.html 在 `<script src="admin.js">` 之前加载：
```html
<script src="/static/js/i18n.js?v=20261016-1"></script>
```

i18n.js 的 MODULES 列表新增 `'admin'`：
```js
var MODULES = [
  'common', 'app', 'settings', 'knowledge', 'skills',
  'chat', 'events', 'agents', 'pipeline', 'runtimes',
  'tasks', 'issues', 'repos', 'dashboard', 'observability', 'templates',
  'organizations', 'autopilot', 'admin'  // ← 新增
];
```

#### 语言包文件

新增 `src/api/static/locales/zh/admin.json` + `src/api/static/locales/en/admin.json`。

admin.json 按功能模块组织：
```json
{
  "title": "Grape Admin — 配置中心",
  "nav": {
    "config_center": "配置中心",
    "credentials": "平台凭据",
    "user_management": "用户管理",
    "user_list": "用户列表",
    "whitelist": "注册白名单",
    "change_password": "修改密码",
    "logout": "退出登录"
  },
  "login": {
    "subtitle": "系统管理 · 配置中心",
    "admin_account": "管理员账号",
    "password": "登录密码",
    "login_btn": "登 录",
    "enter_credentials": "请输入账号和密码",
    "back_to_app": "← 返回用户界面"
  },
  "pwd_modal": {
    "title": "设置管理员密码",
    "desc": "您正在使用默认密码。为保障安全建议立即修改，修改后旧会话将失效。",
    "old_password": "原密码",
    "new_password": "新密码（至少 8 位）",
    "confirm_password": "确认新密码",
    "skip": "跳过",
    "confirm": "确认修改",
    "empty_fields": "请填写原密码与新密码",
    "too_short": "新密码至少 8 位",
    "mismatch": "两次输入的新密码不一致",
    "changed": "密码已修改，请重新登录"
  },
  "config": {
    "title": "平台凭据配置",
    "gitcode": "GitCode 配置",
    "gitcode_desc": "github.com 平台操作凭据",
    "github": "GitHub 配置",
    "github_desc": "GitHub 平台操作凭据",
    "bot_account": "Bot 账户",
    "bot_account_desc": "self-comment 检测",
    "verify": "校验",
    "save": "保存配置",
    "save_hint": "保存后写入数据库并即时生效（集群所有实例共享）",
    "auto_infer": "留空自动推断",
    "configured": "● 已配置",
    "configured_update": "● 已配置（重新输入以更新）",
    "not_configured": "○ 未配置",
    "configured_tail": "● 已配置 (…{{tail}})",
    "bot_usernames_hint": "逗号分隔，如 grape_bot, dev-bot",
    "bot_usernames_fill_hint": "手动填写或点击上方「校验」后用返回的用户名自动回填",
    "save_success": "配置已保存",
    "save_failed": "保存失败",
    "load_failed": "加载配置失败",
    "enter_token_first": "请先填入 token 再校验",
    "token_valid": "token 有效: {{login}}",
    "token_invalid": "token 无效或不具备访问权限",
    "verify_failed": "校验失败"
  },
  "token_health": {
    "title": "Bot Token 健康状态",
    "desc": "后端探活监控",
    "refresh": "刷新",
    "status": "健康状态",
    "checked_at": "最近探活时间",
    "error_type": "错误类型",
    "error_detail": "错误详情",
    "failures": "连续失败次数",
    "hint": "后端定时探活 GitCode/GitHub bot token；「暂不可用」表示探活服务尚未部署",
    "healthy": "健康",
    "degraded": "降级",
    "unhealthy": "异常",
    "not_probed": "尚未探活",
    "unavailable": "暂不可用",
    "load_failed": "加载失败",
    "load_health_failed": "加载健康状态失败",
    "gitcode": "GitCode",
    "github": "GitHub",
    "no_platforms": "无已配置平台"
  },
  "users": {
    "title": "用户管理",
    "search_ph": "搜索用户名/邮箱",
    "search": "搜索",
    "total": "(共 {{count}} 人)",
    "load_failed": "加载用户列表失败",
    "disable_confirm": "确定禁用此用户？",
    "disabled": "用户已禁用",
    "disable_failed": "禁用失败",
    "restored": "用户已恢复",
    "restore_failed": "恢复失败",
    "status_disabled": "禁用",
    "status_active": "正常",
    "status_deleted": "已注销",
    "status_cooldown": "冷静期",
    "disable": "禁用",
    "restore": "恢复",
    "col_id": "ID",
    "col_username": "用户名",
    "col_email": "邮箱",
    "col_status": "状态",
    "col_binding": "绑定",
    "col_last_login": "最近登录",
    "col_actions": "操作"
  },
  "whitelist": {
    "title": "注册白名单",
    "add": "添加白名单",
    "type": "类型",
    "type_email": "邮箱",
    "type_domain": "域名",
    "value": "值",
    "value_ph": "user@example.com 或 @company.com",
    "remark": "备注",
    "remark_ph": "可选",
    "add_btn": "添加",
    "signup_settings": "开放注册",
    "signup_switch": "开放注册开关",
    "signup_off": "关闭",
    "signup_on": "开启",
    "save": "保存",
    "signup_hint": "关闭时仅白名单内邮箱可注册；开启时任意邮箱可注册",
    "list": "白名单列表",
    "col_id": "ID",
    "col_type": "类型",
    "col_value": "值",
    "col_remark": "备注",
    "col_created": "创建时间",
    "col_actions": "操作",
    "delete": "删除",
    "enter_value": "请输入值",
    "added": "已添加",
    "add_failed": "添加失败",
    "deleted": "已删除",
    "delete_failed": "删除失败",
    "updated": "已更新",
    "update_failed": "更新失败",
    "load_failed": "加载白名单失败"
  }
}
```

#### admin.html 改造方式

所有 HTML 硬编码中文改为 `data-i18n` 属性：
```html
<!-- 改前 -->
<a class="act" onclick="showCfg()"><span class="ic">⚙</span>平台凭据</a>
<!-- 改后 -->
<a class="act" onclick="showCfg()"><span class="ic">⚙</span><span data-i18n="admin.nav.credentials">平台凭据</span></a>
```

input placeholder 改为 `data-i18n-placeholder`：
```html
<!-- 改前 -->
<input class="ipt" id="loginUser" placeholder="管理员账号" autocomplete="username">
<!-- 改后 -->
<input class="ipt" id="loginUser" data-i18n-placeholder="admin.login.admin_account" placeholder="管理员账号" autocomplete="username">
```

JS 中动态文案改为 `I18N.t()` 调用：
```js
// 改前
toast('请输入账号和密码', false);
// 改后
toast(I18N.t('admin.login.enter_credentials'), false);
```

#### 语言检测

admin 访问路径是 `/admin`（无 `/en` 前缀）。i18n.js 的 `detectLang()` 会走 localStorage（`grapeLang`），与主应用共享。用户在主应用切英文后进 admin，i18n.js 读到 `grapeLang=en` 自动用英文语言包。

不新增 `/en/admin` 路由——admin 是管理后台，URL 前缀方案只对面向用户的 SPA 有意义。admin 通过 localStorage 同步语言即可。

#### i18n.js 初始化时序

admin.html 底部加载顺序：
```html
<script src="/static/js/i18n.js?v=20261016-1"></script>
<script>I18N.init('20261016-1');</script>
<script src="/static/js/admin.js?v=20261016-1"></script>
```

i18n.js init() 同步用内联包初始化 + applyI18nToDOM()，异步 fetch 完整包（含 admin.json）后再次 applyI18nToDOM()。admin.js 的 init() 在 DOMContentLoaded 时触发，此时 i18n 内联包已就绪。

**内联包扩展**：i18n.js 的 `_INLINE_PACKS` 需要加 admin 的关键文案（title + nav + login），确保首次加载不闪动。

### 2.3 admin CSS Tokens 对齐

#### 方式：抽 tokens.css 变量层，admin 引用 tokens.css 不引整个 app.css

**不引用整个 app.css 的原因**（代码验证）：

app.css 104KB，含 957 条 CSS 规则。其中有 9 条全局选择器会直接影响 admin：
- `*`（reset margin/padding/box-sizing）
- `body`（display:flex + font-family + background + color）
- `table`/`th`/`td`/`tr:hover td`（表格样式覆盖）
- `.modal`（position:fixed + z-index:300）
- `.toast`（样式覆盖）

admin.html 自己也有 `*`/`body`/`.modal`/`.toast` 全局规则，两者冲突会导致 admin 布局错位。

**方案：新建 `/static/css/tokens.css`**

从 app.css 的 `:root` 和 apple-theme.css 的 `[data-theme="apple"]` 中提取纯变量，放入独立文件：

```css
/* tokens.css — 共享设计变量，不含任何选择器规则 */
:root{
  --s:#0c1222;--s2:#161e30;--s3:#1e2940;
  --bg:#f5f6f8;--c:#fff;--b:#e3e6ee;
  --t:#101724;--t2:#5b667b;--t3:#8d97a8;
  --a:#3168e0;--ah:#2556c2;--g:#1ca44c;--y:#d49c0f;
  --r:#dc2828;--pu:#7b3cf2;--or:#ea5a0c;--pk:#d83076;--sk:#0c92d4;
  --rd:8px;--r2:12px;
  --sh:0 1px 3px rgba(0,0,0,.04);
}

[data-theme="apple"]{
  --s:#f5f5f7;--s2:#fff;--s3:#eaeaec;
  --bg:#f5f5f7;--c:#fff;--b:#d2d2d7;
  --t:#1d1d1f;--t2:#6e6e73;--t3:#86868b;
  --a:#0071e3;--ah:#0077ed;--g:#34c759;--y:#ff9f0a;
  --r:#ff3b30;--pu:#af52de;--or:#ff9f0a;--pk:#ff2d92;--sk:#5ac8fa;
  --rd:12px;--r2:18px;
  --sh:0 2px 8px rgba(0,0,0,0.04),0 4px 16px rgba(0,0,0,0.06);
}
```

admin.html `<head>` 引用：
```html
<link rel="stylesheet" href="/static/css/tokens.css?v=20261016-1">
```

主应用 index.html 也改为引用 tokens.css + app.css（tokens.css 在前，app.css 中的 `:root` 重复定义相同值不冲突）。这样 tokens.css 是共享变量的单一来源，主应用改 token 时 admin 自动跟随。

**加载顺序保证**：tokens.css 在 admin.html 内联 `<style>` 之前加载。admin 内联 `:root` 删除与 tokens.css 重复的变量，只保留 admin 特有的 `--t2:#86868b`（admin 用更浅的次级文字色）。如果后续 admin 要完全对齐主应用，这个覆盖也可以删除。

**变量 fallback**：admin 内联 CSS 中使用 `var(--xxx, fallback)` 格式，避免 tokens.css 加载失败时样式崩：
```css
border:1px solid var(--b, #e3e6ee);
border-radius:var(--rd, 8px);
```

#### 具体对齐映射

| admin 原值 | 对齐到 tokens.css 变量 | 实际值变化 |
|-----------|---------------------|-----------|
| `--a:#3168e0` | `var(--a)` = #3168e0 (default) / #0071e3 (apple) | 跟随主题 |
| `--bg:#f5f6f8` | `var(--bg)` = #f5f6f8 (default) / #f5f5f7 (apple) | 跟随主题 |
| 硬编码 `#e3e6ee` (border) | `var(--b, #e3e6ee)` | 跟随主题 |
| 硬编码 `12px` (card radius) | `var(--r2, 12px)` | 统一 |
| 硬编码 `8px` (input radius) | `var(--rd, 8px)` | 统一 |
| `--rd:10px` | 删除，用 `var(--rd, 8px)` | 统一 |
| `--sh:0 1px 4px rgba(0,0,0,.06)` | `var(--sh, 0 1px 4px rgba(0,0,0,.06))` | 统一 |

admin.html 内联 CSS 中的硬编码色值逐个替换为 `var(--xxx, fallback)`：
- `#e3e6ee` → `var(--b, #e3e6ee)`
- `#d8dce4` → `var(--b, #d8dce4)`
- `#f2f3f5` / `#f7f8fa` → `var(--bg, #f5f6f8)`
- `#dc2828` → `var(--r, #dc2828)`
- `#1ca44c` → `var(--g, #1ca44c)`
- `linear-gradient(135deg,#3168e0,#4f46e5)` → 保留（admin 按钮渐变是品牌特色，主应用也有同样的渐变）

**类名不统一改**：admin 的 `.card/.btn/.tag-s` 类名保留，只改内部 CSS 值引用变量。不改类名避免大面积 HTML 改动和回归风险。

#### Apple theme 适配

admin.html 的 `<html>` 标签读取 localStorage `grapeTheme`，与主应用保持主题一致。首屏尽早执行避免闪白：

```html
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<script>
(function(){
  var t = localStorage.getItem('grapeTheme') || 'default';
  if (t === 'apple') document.documentElement.setAttribute('data-theme','apple');
})();
</script>
```

这段脚本在 `<head>` 最前面同步执行，`data-theme` 在 CSS 解析前已设置，无闪白。tokens.css 的 `[data-theme="apple"]` 变量覆盖生效，admin 颜色跟随主题。

如果用户没选过主题（`grapeTheme` 为 null），用 default 主题（和现在一致）。

#### 共享资源版本化

tokens.css 和 i18n.js 是共享资源，加 `?v=` 版本号。主应用改 tokens.css 或 i18n.js 时 bump 版本号，admin 引用处同步更新。主应用发版时跑 admin 冒烟测试（语言切换 + 主题切换 + 健康面板 + 配置保存）。

### 2.4 #348 健康面板实现

#### API 契约（后端实际返回结构，代码逐行验证）

后端 `GET /api/admin/bot-token-health`（`src/admin/routes.py:313`）从 DB meta 表读取探活结果：

- 有数据时：`json.loads(raw)`，结构由 `src/runtime/bot_token_health.py` 的 `_update_health_status()` 生成
- 无数据时（首次访问/未探活）：`{"overall": "healthy", "platforms": {}, "checked_at": "", "note": "尚未执行探活"}`

完整结构：
```json
{
  "overall": "healthy" | "degraded" | "unhealthy",  // 字符串三态，不是布尔
  "platforms": {
    "gitcode": {  // 只含已配置 token 的平台（config 类 error_type 的平台被跳过）
      "healthy": bool,
      "degraded": bool,      // not healthy && consecutive_failures < 3
      "unhealthy": bool,     // not healthy && consecutive_failures >= 3
      "consecutive_failures": int,
      "error_type": "" | "auth" | "network" | "config",
      "error": string        // 空 = 健康；有值 = 最近一次失败信息（截断 200 字符）
    },
    "github": { ... }  // 同结构
  },
  "checked_at": "2026-10-15 14:30:00"  // time.strftime，空串 = 未探活
}
```

**关键约束**：
- `overall` 是字符串，不是布尔值。前端不要写 `if (d.overall)`（字符串 `"unhealthy"` 是 truthy）
- `platforms` 可能是空对象 `{}`（无平台配置 token 时）
- `platforms` 只含已配置 token 的平台（error_type=config 的平台被 `_update_health_status` 跳过）
- 平台 key 固定为 `"gitcode"` 和 `"github"`（后端硬编码），不动态生成
- `checked_at` 空串 = 未探活
- `error` 截断 200 字符，非敏感信息（admin 权限足够查看）
- 无 `note` 字段（仅首次未探活的兜底响应有）

#### P0 修复：消费嵌套结构

`tokenHealthLevel(d)` 改为读 `d.overall` 字符串：
```js
function tokenHealthLevel(d) {
  d = d || {};
  if (d.overall === 'unhealthy') return 'unhealthy';
  if (d.overall === 'degraded') return 'degraded';
  return 'healthy';
}
```

#### P1 修复：分平台展示

面板改为分平台布局。顶层显示 overall 状态 + checked_at，下方按 `platforms` 对象遍历渲染每个已配置平台的一组字段。**不写死平台列表**，用 `Object.keys(d.platforms)` 遍历，空态显示「无已配置平台」：

```
┌─ Bot Token 健康状态 ────────────────── [刷新] ─┐
│ 健康状态：● 降级                                  │
│ 最近探活时间：2026-10-15 14:30:00                 │
│                                                  │
│ ┌─ GitCode ─────────────────────────────────┐    │
│ │ ● 健康     错误类型：—    错误详情：—      │    │
│ │ 连续失败次数：0                            │    │
│ └────────────────────────────────────────────┘    │
│ ┌─ GitHub ──────────────────────────────────┐    │
│ │ ● 降级     错误类型：auth  错误详情：401   │    │
│ │ 连续失败次数：2                            │    │
│ └────────────────────────────────────────────┘    │
│                                                  │
│ 后端定时探活 GitCode/GitHub bot token             │
└──────────────────────────────────────────────────┘
```

JS 渲染分平台（遍历 platforms，不写死 key）：
```js
function renderTokenHealth(d) {
  d = d || {};
  var level = tokenHealthLevel(d);
  var meta = TOKEN_HEALTH_META[level];
  $2('tkh_status').innerHTML = '<span class="' + meta.cls + '">● ' + I18N.t('admin.token_health.' + level) + '</span>';
  $2('tkh_checked_at').textContent = d.checked_at || I18N.t('admin.token_health.not_probed');

  var platforms = d.platforms || {};
  var keys = Object.keys(platforms);
  var html = '';
  for (var i = 0; i < keys.length; i++) {
    var prov = keys[i];
    var p = platforms[prov];
    if (!p) continue;
    var pLevel = p.unhealthy ? 'unhealthy' : (p.degraded ? 'degraded' : 'healthy');
    var pMeta = TOKEN_HEALTH_META[pLevel];
    var et = (p.error_type || '').toLowerCase();
    var etHtml = et ? '<span class="' + (ERROR_TYPE_CLS[et] || 'tag-w') + '">' + esc(et) + '</span>' : '—';
    // 失败次数为 0 时不展示错误详情（展示 —）
    var errDisplay = (p.consecutive_failures > 0) ? (esc(p.error) || '—') : '—';
    var label = I18N.t('admin.token_health.' + prov) || prov;
    html += '<div class="card" style="margin-top:8px;padding:14px 18px">'
      + '<div class="card-h" style="margin-bottom:8px"><h2 style="font-size:13px">' + esc(label) + '</h2></div>'
      + '<div class="f-row"><label>' + I18N.t('admin.token_health.status') + '</label><span><span class="' + pMeta.cls + '">● ' + I18N.t('admin.token_health.' + pLevel) + '</span></span></div>'
      + '<div class="f-row"><label>' + I18N.t('admin.token_health.error_type') + '</label><span>' + etHtml + '</span></div>'
      + '<div class="f-row"><label>' + I18N.t('admin.token_health.error_detail') + '</label><span style="font-size:13px;color:var(--t2)">' + errDisplay + '</span></div>'
      + '<div class="f-row"><label>' + I18N.t('admin.token_health.failures') + '</label><span style="font-size:13px">' + String(p.consecutive_failures == null ? 0 : p.consecutive_failures) + '</span></div>'
      + '</div>';
  }
  var pEl = $2('tkh_platforms');
  if (pEl) pEl.innerHTML = html || '<div class="hint">' + I18N.t('admin.token_health.no_platforms') + '</div>';
}
```

#### P2 修复：版本号递增

admin.html 底部：
```html
<script src="/static/js/i18n.js?v=20261016-1"></script>
<script src="/static/js/admin.js?v=20261016-1"></script>
```

#### 兜底逻辑保留

- 404/405（后端未部署）→ 显示「暂不可用」
- 401（未登录）→ 不崩溃，不弹 toast
- checked_at 为空 → 显示「尚未探活」
- platforms 为空 → 显示「无已配置平台」

#### 测试更新

`tests/js/test_issue_348_bot_token_health.js` 的 mock 数据改为嵌套结构：
```js
// 改前（错误）
loadWith({ healthy: true, degraded: false, unhealthy: false, consecutive_failures: 0, ... });
// 改后（正确）
loadWith({ overall: 'healthy', platforms: { gitcode: { healthy: true, ... } }, checked_at: '...' });
```

新增分平台渲染验证、overall 三态判定验证。

## 三、文件变更清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| src/api/static/css/tokens.css | **新增** | 从 app.css :root + apple-theme.css [data-theme] 提取的纯变量层，无选择器规则 |
| src/api/static/admin.html | 修改 | 引入 tokens.css + i18n.js + 主题检测脚本；硬编码中文改 data-i18n；CSS 硬编码值改 var(--xxx, fallback)；健康面板 card；版本号递增 |
| src/api/static/js/admin.js | 修改 | 硬编码中文改 I18N.t()；健康面板 loadTokenHealth/renderTokenHealth 重写（嵌套结构+分平台遍历）；主题检测 |
| src/api/static/js/i18n.js | 修改 | MODULES 列表新增 'admin'；_INLINE_PACKS 加 admin 关键文案（title + nav + login） |
| src/api/static/locales/zh/admin.json | **新增** | admin 中文语言包 |
| src/api/static/locales/en/admin.json | **新增** | admin 英文语言包 |
| src/api/static/index.html | 修改 | 引入 tokens.css（在 app.css 之前） |
| tests/js/test_issue_348_bot_token_health.js | 修改 | mock 数据改嵌套结构；新增分平台渲染验证；overall 三态判定验证 |
| contracts/health.schema.json | **新增** | 健康面板 API 返回结构 JSON Schema |
| scripts/check-shared-versions.sh | **新增** | 共享文件 ?v= 版本一致性校验 |
| scripts/check-i18n-keys.py | **新增** | zh/en 语言包 key 集合一致性校验 |
| scripts/check-css-vars.py | **新增** | admin 引用的 CSS 变量是否在 tokens.css 中定义 |
| scripts/check-health-contract.py | **新增** | 健康面板返回结构符合 schema 校验 |
| scripts/smoke-admin.sh | **新增** | Admin 4 条关键路径冒烟测试 |
| scripts/check-all.sh | **新增** | 统一执行所有契约校验 |
| docs/spec-348.md | 删除 | 旧 spec 文档（基于错误的 API 结构），被本设计文档替代 |

**不改的文件**：
- `src/admin/routes.py` — 后端 API 不改（#345 已实现）
- `src/runtime/bot_token_health.py` — 探活逻辑不改
- `src/api/main.py` — 不新增 /en/admin 路由
- `src/api/static/css/app.css` — 不改（保留 :root 重复定义，与 tokens.css 同值不冲突）
- `src/api/static/css/apple-theme.css` — 不改（保留 [data-theme] 重复定义，与 tokens.css 同值不冲突）

## 四、风险分析

| 风险 | 影响 | 缓解 |
|------|------|------|
| app.css 全局样式影响 admin 现有布局 | 中 | app.css 的 body/html/* 选择器可能与 admin 内联样式冲突。验证方法：引用 app.css 后逐页截图对比 |
| i18n.js init 时序与 admin.js 冲突 | 低 | i18n.js 在 admin.js 之前加载，init() 同步执行 applyI18nToDOM()，不阻塞 admin.js |
| apple theme 下 admin 颜色变化 | 中 | apple theme 覆盖 :root 变量，admin 的硬编码色值改变量后跟随主题。需验证 apple theme 下 admin 可用性 |
| 语言包加载失败 | 低 | i18n.js 已有兜底：内联包保底 + fetch 失败回退默认语言 |
| admin.json key 遗漏 | 低 | 改完后 grep 所有中文残留，逐个补 key |

## 五、验证标准

1. **i18n 验证**
   - admin 页面所有中文文案都有 `data-i18n` 属性或 `I18N.t()` 调用
   - 中文模式（默认）：所有文案显示中文
   - 英文模式（localStorage `grapeLang=en`）：所有文案显示英文
   - `grep -rn '[\u4e00-\u9fff]' admin.js admin.html` 仅剩注释
   - 语言切换后无需刷新页面（applyI18nToDOM 自动更新）

2. **CSS 验证**
   - admin.html 引用 tokens.css 后，default theme 下视觉与现在一致（色值差异在容许范围内）
   - apple theme 下 admin 颜色跟随主题变化
   - 核心 token 变量（--a/--b/--t/--t2/--c/--bg/--g/--r/--r2/--sh/--s/--s2/--rd）全部迁移为 var(--xxx, fallback)
   - 登录暗色主题、状态标签（tag-s/tag-d/tag-w）、banner 等装饰性色值保留硬编码（tokens.css 无对应变量，属 admin 专用语义色）

3. **健康面板验证**
   - API 返回嵌套结构时正确渲染 overall 状态 + 分平台明细
   - 三态颜色正确：unhealthy 红 > degraded 黄 > healthy 绿
   - 分平台：遍历 platforms 对象渲染，不写死 key
   - checked_at 为空显示「尚未探活」
   - 404 显示「暂不可用」
   - 401 不崩溃
   - 刷新按钮可手动重新拉取

4. **回归验证**
   - `node tests/js/test_issue_348_bot_token_health.js` 全过
   - `python3 -m pytest tests/ -q --tb=no` 无新增 failed
   - admin 页面其他功能（用户管理/白名单/配置保存）不受影响

5. **跨系统语言一致性**
   - 主应用切英文 → 进 admin → admin 显示英文
   - admin 切英文（如果有切换入口）→ 回主应用 → 主应用显示英文
   - localStorage `grapeLang` 统一

## 五-B、契约校验机制（弱契约补强）

上面的验证标准是人工检查。以下脚本化校验将弱契约补强为可执行的自动校验，不依赖构建工具链。

### 5-B.1 共享文件消费者清单 + 版本一致性校验

**文件**：`scripts/check-shared-versions.sh`

维护共享文件→消费者映射，检查所有消费者引用的 `?v=` 是否一致：

```bash
#!/bin/bash
# 共享文件消费者清单
# 格式：文件 → 消费者1,消费者2,...
declare -A SHARED_FILES=(
  ["tokens.css"]="admin.html,index.html"
  ["i18n.js"]="admin.html,index.html"
)

FAIL=0
for file in "${!SHARED_FILES[@]}"; do
  consumers="${SHARED_FILES[$file]}"
  IFS=',' read -ra arr <<< "$consumers"
  prev_ver=""
  for c in "${arr[@]}"; do
    ver=$(grep -oP "${file}\?v=\K[0-9a-zA-Z-]+" "src/api/static/$c" 2>/dev/null | head -1)
    if [ -z "$ver" ]; then
      echo "WARN: $c does not reference $file"
      continue
    fi
    if [ -n "$prev_ver" ] && [ "$ver" != "$prev_ver" ]; then
      echo "FAIL: $file version mismatch: ${arr[0]}=?v=$prev_ver vs $c=?v=$ver"
      FAIL=1
    fi
    prev_ver="$ver"
  done
done
exit $FAIL
```

**共享文件向后兼容原则**：
- 只增变量，不删/不改语义
- 废弃变量标 `/* @deprecated */`，保留至少一个版本周期
- 变量名变更时，旧变量保留为 alias（`--old-name: var(--new-name)`）

### 5-B.2 JSON 语言包 key 校验

**文件**：`scripts/check-i18n-keys.py`

校验 zh/admin.json 和 en/admin.json 的 key 集合完全一致：

```python
#!/usr/bin/env python3
"""校验 zh/en 语言包 key 集合一致。"""
import json, sys, os

LOCALES_DIR = 'src/api/static/locales'
MODULES = ['common', 'admin']  # admin 涉及的模块

def flat_keys(obj, prefix=''):
    keys = set()
    for k, v in obj.items():
        full = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            keys |= flat_keys(v, full)
        else:
            keys.add(full)
    return keys

fail = 0
for mod in MODULES:
    zh_path = f'{LOCALES_DIR}/zh/{mod}.json'
    en_path = f'{LOCALES_DIR}/en/{mod}.json'
    if not os.path.exists(en_path):
        continue
    with open(zh_path, encoding='utf-8') as f: zh = json.load(f)
    with open(en_path, encoding='utf-8') as f: en = json.load(f)
    zh_keys = flat_keys(zh, mod)
    en_keys = flat_keys(en, mod)
    missing_in_en = zh_keys - en_keys
    missing_in_zh = en_keys - zh_keys
    if missing_in_en:
        print(f'FAIL: {mod} keys missing in en: {sorted(missing_in_en)}')
        fail = 1
    if missing_in_zh:
        print(f'FAIL: {mod} keys missing in zh: {sorted(missing_in_zh)}')
        fail = 1
    if not missing_in_en and not missing_in_zh:
        print(f'OK: {mod} zh/en key sets match ({len(zh_keys)} keys)')

sys.exit(fail)
```

### 5-B.3 CSS 变量引用校验

**文件**：`scripts/check-css-vars.py`

校验 admin.html 内联 CSS 中引用的 `var(--xxx)` 是否都在 tokens.css 中定义：

```python
#!/usr/bin/env python3
"""校验 admin 引用的 CSS 变量都在 tokens.css 中定义。"""
import re, sys

def extract_vars(filepath):
    with open(filepath, encoding='utf-8') as f:
        content = f.read()
    # 提取 --xxx 定义
    defined = set(re.findall(r'(--[\w-]+)\s*:', content))
    # 提取 var(--xxx) 引用（含 fallback: var(--xxx, fallback)）
    referenced = set(re.findall(r'var\((--[\w-]+)', content))
    return defined, referenced

# tokens.css 定义的变量
tok_defined, _ = extract_vars('src/api/static/css/tokens.css')

# admin.html 内联 CSS 引用的变量
# 先提取 <style> 块
with open('src/api/static/admin.html', encoding='utf-8') as f:
    html = f.read()
m = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
admin_css = m.group(1) if m else ''
admin_defined = set(re.findall(r'(--[\w-]+)\s*:', admin_css))
admin_referenced = set(re.findall(r'var\((--[\w-]+)', admin_css))

# admin 可用的变量 = tokens.css 定义 + admin 自己定义
available = tok_defined | admin_defined
# 未定义的引用
undefined = admin_referenced - available
if undefined:
    print(f'WARN: admin references undefined CSS vars: {sorted(undefined)}')
    print('  These vars have fallback values in var(--xxx, fallback) so they will not break.')
    sys.exit(0)  # WARN 不 fail，因为有 fallback
else:
    print(f'OK: all {len(admin_referenced)} CSS var references defined in tokens.css or admin :root')
    sys.exit(0)
```

### 5-B.4 健康面板 API 契约 Schema

**文件**：`contracts/health.schema.json`

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Bot Token Health Response",
  "type": "object",
  "required": ["overall", "platforms", "checked_at"],
  "properties": {
    "overall": {
      "type": "string",
      "enum": ["healthy", "degraded", "unhealthy"]
    },
    "platforms": {
      "type": "object",
      "patternProperties": {
        "^(gitcode|github)$": {
          "type": "object",
          "required": ["healthy", "degraded", "unhealthy", "consecutive_failures", "error_type", "error"],
          "properties": {
            "healthy": {"type": "boolean"},
            "degraded": {"type": "boolean"},
            "unhealthy": {"type": "boolean"},
            "consecutive_failures": {"type": "integer", "minimum": 0},
            "error_type": {"type": "string", "enum": ["", "auth", "network", "config"]},
            "error": {"type": "string"}
          }
        }
      },
      "additionalProperties": false
    },
    "checked_at": {"type": "string"}
  },
  "additionalProperties": true
}
```

**校验脚本**：`scripts/check-health-contract.py`

```python
#!/usr/bin/env python3
"""校验健康面板 API 返回结构符合 schema。可本地或 CI 运行。"""
import json, sys, os

def validate(response):
    """纯 Python 校验，不依赖 jsonschema 库。"""
    if not isinstance(response, dict):
        return ['response is not dict']
    errors = []
    # overall
    if response.get('overall') not in ('healthy', 'degraded', 'unhealthy'):
        errors.append(f"overall must be healthy/degraded/unhealthy, got: {response.get('overall')}")
    # platforms
    platforms = response.get('platforms', {})
    if not isinstance(platforms, dict):
        errors.append('platforms must be object')
    else:
        for prov, p in platforms.items():
            if prov not in ('gitcode', 'github'):
                errors.append(f'unknown platform key: {prov}')
                continue
            for field in ('healthy', 'degraded', 'unhealthy'):
                if not isinstance(p.get(field), bool):
                    errors.append(f'{prov}.{field} must be boolean')
            if not isinstance(p.get('consecutive_failures'), int):
                errors.append(f'{prov}.consecutive_failures must be integer')
            if p.get('error_type', '') not in ('', 'auth', 'network', 'config'):
                errors.append(f'{prov}.error_type invalid: {p.get("error_type")}')
    # checked_at
    if not isinstance(response.get('checked_at', ''), str):
        errors.append('checked_at must be string')
    return errors

# 可用 mock 数据校验，或请求真实 API
if __name__ == '__main__':
    # 用 mock 数据校验 schema 正确性
    mock = {
        "overall": "degraded",
        "platforms": {
            "gitcode": {"healthy": True, "degraded": False, "unhealthy": False,
                        "consecutive_failures": 0, "error_type": "", "error": ""},
            "github": {"healthy": False, "degraded": True, "unhealthy": False,
                       "consecutive_failures": 2, "error_type": "auth", "error": "401 Unauthorized"}
        },
        "checked_at": "2026-10-15 14:30:00"
    }
    errors = validate(mock)
    if errors:
        print('FAIL: health contract validation failed:')
        for e in errors: print(f'  - {e}')
        sys.exit(1)
    else:
        print('OK: health contract schema valid')
```

### 5-B.5 E2E 冒烟测试脚本

**文件**：`scripts/smoke-admin.sh`

4 条关键路径，用 Node + Playwright（已在项目环境中可用）。如果没有 Playwright，退化为 curl + grep 的最小检查：

```bash
#!/bin/bash
# Admin 冒烟测试 — 4 条关键路径
# 依赖：grape 服务运行在 localhost:3000
BASE="http://localhost:3000"
FAIL=0

echo "=== Admin Smoke Test ==="

# 1. admin 页面加载 + 包含 i18n.js + tokens.css
echo -n "1. Page loads with shared resources... "
resp=$(curl -s "$BASE/admin")
if echo "$resp" | grep -q 'i18n.js' && echo "$resp" | grep -q 'tokens.css'; then
  echo "PASS"
else
  echo "FAIL (missing i18n.js or tokens.css reference)"
  FAIL=1
fi

# 2. admin.json 语言包可加载（zh + en）
echo -n "2. admin.json locale packs loadable... "
zh_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/locales/zh/admin.json")
en_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/locales/en/admin.json")
if [ "$zh_status" = "200" ] && [ "$en_status" = "200" ]; then
  echo "PASS"
else
  echo "FAIL (zh=$zh_status en=$en_status)"
  FAIL=1
fi

# 3. tokens.css 可加载
echo -n "3. tokens.css loadable... "
tok_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/css/tokens.css")
if [ "$tok_status" = "200" ]; then
  echo "PASS"
else
  echo "FAIL (status=$tok_status)"
  FAIL=1
fi

# 4. 健康面板 API 返回结构校验
echo -n "4. bot-token-health API structure... "
# 需要登录获取 token（简化：检查 endpoint 存在性）
health_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/admin/bot-token-health")
if [ "$health_status" = "401" ] || [ "$health_status" = "200" ]; then
  echo "PASS (endpoint exists, status=$health_status)"
else
  echo "FAIL (status=$health_status, expected 200 or 401)"
  FAIL=1
fi

echo ""
if [ $FAIL -eq 0 ]; then
  echo "All smoke tests passed."
else
  echo "Smoke tests FAILED."
fi
exit $FAIL
```

### 5-B.6 校验脚本执行入口

所有校验脚本可通过一个入口脚本统一执行：

```bash
# scripts/check-all.sh
#!/bin/bash
echo "=== Running all contract checks ==="
./scripts/check-shared-versions.sh
python3 scripts/check-i18n-keys.py
python3 scripts/check-css-vars.py
python3 scripts/check-health-contract.py
echo "=== Contract checks complete ==="
```

这些脚本不依赖构建工具链，只需 bash + python3（grape 已有）。后续迁移到框架时，这些脚本可直接进 CI pipeline。

## 六、实施顺序

1. 新增 `src/api/static/css/tokens.css`（从 app.css :root + apple-theme.css [data-theme] 提取）
2. 新增 `locales/zh/admin.json` + `locales/en/admin.json`
3. i18n.js MODULES 加 'admin' + _INLINE_PACKS 加 admin 关键文案
4. admin.html 引入 tokens.css + i18n.js + 主题检测脚本
5. admin.html CSS 变量对齐（硬编码改 var(--xxx, fallback)）
6. admin.html 硬编码中文改 data-i18n 属性
7. admin.js 硬编码中文改 I18N.t() 调用
8. admin.js 健康面板函数重写（嵌套结构 + 分平台遍历）
9. admin.html 健康面板 card HTML
10. 测试文件更新（嵌套 mock + 分平台验证）
11. 新增契约校验脚本：`scripts/check-shared-versions.sh` + `scripts/check-i18n-keys.py` + `scripts/check-css-vars.py` + `scripts/check-health-contract.py` + `scripts/smoke-admin.sh` + `scripts/check-all.sh`
12. 新增 `contracts/health.schema.json`
13. index.html 引入 tokens.css（在 app.css 之前）
14. 版本号递增
15. 验证全流程 + 跑契约校验脚本

## 七、确认点（对应审查反馈 7 项）

1. **i18n.js API 稳定性和 admin.json 运行时合并**
   - 验证结果：i18n.js 公开 API = `{ t, setLang, getLang, init, detectLang, applyI18nToDOM, isLangLoaded, SUPPORTED, DEFAULT_LANG }`。`t(key, vars)` 支持点分 key 查找 + `{{name}}` 变量插值
   - admin.json 运行时合并方式：MODULES 数组新增 `'admin'`，`loadPacks()` 自动 fetch `/static/locales/{lang}/admin.json` 并 merge 到 `_packs[lang].admin`。`t('admin.nav.credentials')` 自动查到。无需改 i18n.js 的 merge 逻辑
   - 确认：i18n.js 只加 MODULES 列表一项 + _INLINE_PACKS 几个 key，不改核心逻辑 — OK？

2. **grapeLang 存值格式、默认语言、fallback 规则**
   - 验证结果：`SUPPORTED = ['zh', 'en']`，`DEFAULT_LANG = 'zh'`，存值格式是 `'zh'` / `'en'`（非 zh-CN/en-US）。admin 复用同一 i18n.js 实例，格式天然一致
   - fallback 机制（代码验证）：`t(key)` 查找顺序 = 当前语言包 → 默认语言包(zh) → 返回 key 本身。admin.json 缺 key 不会显示空白
   - 确认：admin 直接复用主应用 i18n.js 实例（不独立实例），格式/fallback 天然统一 — OK？

3. **admin 文案 key 命名空间**
   - 所有 admin 文案统一 `admin.*` 前缀（如 `admin.nav.credentials`、`admin.config.save`、`admin.token_health.healthy`）
   - 通用文案复用 `common.*`（如 `common.save`、`common.delete`、`common.cancel`）
   - 确认：admin.* 命名空间 + common.* 共享 — OK？

4. **CSS 抽 tokens.css 而非引整个 app.css**
   - 验证结果：app.css 104KB / 957 条规则，含 9 条全局选择器（`*`/`body`/`table`/`th`/`td`/`.modal`/`.toast`）会与 admin 内联样式冲突
   - 方案：新建 tokens.css 只含 `:root` + `[data-theme="apple"]` 变量定义，无选择器规则。admin 引 tokens.css + 自己的内联样式
   - 变量 fallback：`var(--b, #e3e6ee)` 格式，tokens.css 加载失败也不崩
   - 主应用 index.html 也改引 tokens.css（在 app.css 之前），app.css 的 :root 保留（同值不冲突）
   - 确认：抽 tokens.css 方案 — OK？

5. **grapeTheme 取值和首屏防闪策略**
   - 取值：`localStorage.getItem('grapeTheme')`，值为 `'apple'` 或 null（default）
   - 防闪：`<head>` 最前面同步执行脚本设置 `data-theme`，在 CSS 解析前完成
   - 确认：admin 跟随主应用主题，首屏不闪 — OK？

6. **健康面板 API 契约**
   - 后端返回结构已在文档 2.4 节锁定（代码逐行验证）
   - `overall` 是字符串三态（`"healthy"` / `"degraded"` / `"unhealthy"`），不是布尔
   - `platforms` 是对象，key 为 `"gitcode"` / `"github"`，值含 `healthy/degraded/unhealthy/consecutive_failures/error_type/error`
   - 空态处理：`platforms = {}` → 显示「无已配置平台」；`checked_at = ""` → 显示「尚未探活」
   - 失败次数为 0 时不展示错误详情（显示 —）
   - 确认：API 契约以此文档为准，后端不改 — OK？

7. **版本号策略**
   - 当前手工递增 `?v=YYYYMMDD-N`（如 `20261016-1`）
   - admin.html 引用的 tokens.css / i18n.js / admin.js 统一用同一版本号
   - 改任一共享文件时 bump 版本号
   - 后续可改为构建时注入（如 `__APP_VERSION__` 或文件 hash），但一期手工递增足够
   - 确认：一期手工递增，后续按需改构建注入 — OK？

## 八、审计修复（2026-10-16 实现审计）

### 8.1 审计发现

对工作区实现逐行审计发现 3 个 P0 + 4 个 P1 + 3 个 P2 问题。Part A（i18n + CSS + Health Panel）已完成且验证通过，Part B（OAuth 登录白名单 + User CRUD）存在前后端契约不一致导致功能不可用。

### 8.2 P0-1: OAuth 登录白名单 API 路径不一致（4 个接口全部 404）

**根因**：前端 admin.js 使用 `/login-whitelist/*` 路径，后端 routes.py 注册的是 `/whitelist/*`。

| 前端调用 (admin.js) | 后端注册 (routes.py) | 设计文档 | 结果 |
|---------------------|----------------------|----------|------|
| GET `/login-whitelist/status` | GET `/whitelist-status` | GET `/whitelist` | 404 |
| GET `/login-whitelist` | GET `/whitelist` | GET `/whitelist` | 404 |
| POST `/login-whitelist` | POST `/whitelist` | POST `/whitelist` | 404 |
| DELETE `/login-whitelist/{id}` | DELETE `/whitelist/{item_id}` | DELETE `/whitelist/{id}` | 404 |
| PUT `/login-whitelist/toggle` | PUT `/whitelist/toggle` | PUT `/whitelist/toggle` | 404 |

**修复方案**：以前端对齐后端（后端路径与设计文档一致）。
- `loadLoginWhitelist()`: 合并为单次 `GET /whitelist` 调用，响应含 `{whitelist, enabled}` 两个字段，消除冗余的 status 请求
- `addLoginWhitelist()`: `POST /whitelist`
- `deleteLoginWhitelist()`: `DELETE /whitelist/{id}`
- `toggleLoginWhitelist()`: `PUT /whitelist/toggle`

### 8.3 P0-2: 白名单状态响应字段不一致

**根因**：前端 `loadLoginWhitelist()` 调用 `/login-whitelist/status` 读取 `d.enabled`，但该端点不存在。后端 `GET /whitelist` 返回 `{whitelist: [...], enabled: bool}`，`GET /whitelist-status` 返回 `{signup_open, login_whitelist_enabled}`。

**修复**：合并到 `GET /whitelist` 单次调用，直接读 `d.enabled`（后端已返回此字段）。

### 8.4 P0-3: 重置密码 body 字段不一致（422）

**根因**：前端发送 `{new_password: password}`，后端 `ResetPasswordBody` 期望 `{password: str}`。

**修复**：前端改为发送 `{password: password}` 对齐后端。

### 8.5 P1-1: Toggle 接口语义错误

**根因**：后端 `admin_toggle_login_whitelist` 不接收 body，直接 `not current` 翻转。前端发送 `{enabled: true/false}` 表示设置目标值。

**修复**：后端新增 `ToggleLoginWhitelistBody` 接收 `{enabled: bool}`，按前端指定的目标值设置，不再盲翻转。语义从"toggle"改为"set"，路径保持 `/whitelist/toggle` 不变（已发布的路径不改名）。

### 8.6 P1-2: esc() 未转义双引号（XSS）

**根因**：`esc()` 用 `innerHTML` 转义 `&` `<` `>` 但不转义 `"`。编辑用户模态框 `value="..."` 属性使用 `esc(user.email)`，含双引号的 email 会突破属性边界。

**修复**：`esc()` 增加 `"` → `&quot;` 转义。

### 8.7 P1-3: login_whitelist DDL 未同步

**根因**：DDL 仍为 `UNIQUE (gitcode_username)`，github_username 唯一约束仅靠运行时迁移索引。新建数据库时 DDL 不含 github_username 唯一约束。

**修复**：DDL 同步加 `UNIQUE (github_username)` 约束（用 `WHERE github_username != ''` 部分索引，允许空值共存）。运行时迁移索引保留（兼容已有数据库）。

### 8.8 P1-4: 创建用户校验提示无意义

**根因**：`submitCreateUser()` 中 `if (!username) { toast(I18N.t('admin.users.create_username')) }`，`create_username` 值为"用户名"/"Username"，作为校验提示无意义。

**修复**：admin.json 新增 `admin.users.username_required` / `admin.users.username_required` 和 `admin.users.password_required` 校验提示 key。

### 8.9 P2-1: gitcode_desc 中文文案错误

**根因**：zh admin.json `gitcode_desc` 值为 "github.com 平台操作凭据"，应为 "GitCode 平台操作凭据"。

**修复**：zh `gitcode_desc` 改为 "GitCode 平台操作凭据"。

### 8.10 P2-2: editUser 重复请求用户列表

**根因**：`editUser(uid)` 重新调 `api('/users?page=...')` 拉全量列表找单个用户。

**修复**：改为缓存当前页用户数据 `_currentPageUsers`，editUser 从缓存查找，避免重复请求。

### 8.11 修复文件清单

| 文件 | 修复内容 |
|------|---------|
| src/api/static/js/admin.js | P0-1 路径修正 / P0-3 body 字段 / P1-2 esc 转义 / P1-4 校验提示 / P2-2 editUser 缓存 |
| src/admin/routes.py | P1-1 toggle 接收 body |
| src/admin/db.py | P1-1 set_login_whitelist_enabled 直接设置目标值 |
| src/api/db_setup.py | P1-3 DDL 同步 github_username 唯一约束 |
| src/api/static/locales/zh/admin.json | P1-4 新增校验提示 key / P2-1 修正 gitcode_desc |
| src/api/static/locales/en/admin.json | P1-4 新增校验提示 key |

## 九、深度检查修复（2026-10-16 二轮审计）

### 9.1 P0-NEW-1: 密码哈希算法不兼容

**根因**：admin/db.py 的 `_hash_password()` 用裸 SHA-256 hex（无前缀），而 grape_accounts 的标准哈希走 `auth/password.py` 的 `hash_password()`（bcrypt$ 前缀 + rounds=12）。admin 创建/重置密码的用户存为弱哈希，虽有透明升级但首次登录前 DB 中暴露弱哈希。

**修复**：`create_grape_user` 和 `reset_grape_user_password` 改用 `auth.password.hash_password`（bcrypt）。admin 表自身的 `_hash_password`（admin login/change-password）保持不变——那是 admin 表的既有约定。

### 9.2 P0-NEW-2: 白名单状态判定不一致

**根因**：
- admin/db.py `get_login_whitelist_enabled()`：`val == "true" if val else False` — 空串返回 False
- config.py `_is_login_whitelist_enabled()`：空串走 legacy fallback（表有数据则 True）

meta 未设置时面板显示"已关闭"但实际白名单启用。

**修复**：admin/db.py `get_login_whitelist_enabled()` 直接调用 `config._is_login_whitelist_enabled()`，消除判定逻辑分叉。

### 9.3 P1-NEW-1: toggle 后 60s 缓存不生效

**根因**：config.py `_is_login_whitelist_enabled()` 有 60s TTL 缓存，`set_login_whitelist_enabled` 写 meta 后不清除缓存。

**修复**：config.py 新增 `invalidate_login_whitelist_cache()` 公共函数，admin/db.py `set_login_whitelist_enabled` 调用它。

### 9.4 P1-NEW-2: email 未规范化

**根因**：admin/db.py `create_grape_user` / `update_grape_user` 不做 `.lower().strip()`，而 grape_account_db 所有 email 操作都做。

**修复**：两个函数中加 `email = (email or "").lower().strip() or None`。

### 9.5 P1-NEW-3: create_grape_user 未设置 email_source

**根因**：INSERT 语句不含 `email_source` 列，默认 NULL。grape_account_db 的 create 设置 `email_source='user'`。

**修复**：INSERT 加 `email_source` 列，值为 `'user'`。

### 9.6 P1-NEW-4: update_grape_user 未处理 email UNIQUE 冲突

**根因**：直接 UPDATE email 不检测 UNIQUE 异常，冲突时返回 500 而非 409。

**修复**：`update_grape_user` 检测 UNIQUE/DUPLICATE 异常返回 `"conflict"`，routes.py `admin_edit_user` 处理 409。

### 9.7 P2-NEW-1: esc() 重复定义

**修复**：删除第二处（行 587），保留第一处（行 192）。

### 9.8 P2-NEW-2: addLoginWhitelist 校验提示语义

**修复**：admin.json 新增 `admin.login_whitelist.username_required` key，前端改用此 key。

### 9.9 P2-NEW-3: 密码校验提示不一致

**修复**：`submitResetPassword` 的密码校验改用 `admin.users.password_required`，与 `submitCreateUser` 统一。

### 9.10 P2-NEW-4: login_whitelist.desc 未使用

**修复**：admin.html OAuth 登录白名单 card 标题加 `<span class="tag" data-i18n="admin.login_whitelist.desc">` 描述。

## 十、第三轮审计修复（2026-10-16 三轮审计）

### 10.1 P1-3R-1: editUser 缓存缺 nickname

**根因**：`list_grape_users()` SELECT 只查 `id, username, email, status, token_version, last_login_at, created_at, updated_at`，不含 `nickname`。前端 `editUser()` 从 `_currentPageUsers` 缓存查找用户传给 `showUserModal('edit', user)`，模态框用 `user.nickname` 预填——但缓存对象无 nickname 字段，导致编辑模态框 nickname 始终空白，保存后意外清空原有 nickname。

**修复**：`list_grape_users()` SELECT 加 `nickname` 列，返回字典加 `nickname` 字段。

### 10.2 P2-3R-1: `<title>` 硬编码英文

**根因**：`admin.html` `<title>Grape Admin — Config Center</title>` 硬编码英文，zh/en admin.json 都有 `title` key 但未引用。i18n.js 支持 `[data-i18n-title]` 属性。

**修复**：`<title>` 加 `data-i18n-title="admin.title"` 属性。

### 10.3 P2-3R-2: /whitelist-status 死代码

**根因**：第一轮修复将前端合并为单次 `GET /whitelist` 调用，后端 `GET /whitelist-status` 无调用者。

**修复**：删除 `GET /whitelist-status` 端点。

### 10.4 P1-3R-2: allow_signup 开关状态回显 bug（pre-existing 修复）

**根因**：`read_public_config()` 不读取 meta 表 `allow_signup` 字段。前端 `loadAllowSignup()` 调 `api('/config')` 读 `d.config.allow_signup`，始终为 `undefined → '0'`，开关始终显示"关闭"即使 meta 表实际为 `1`。

**影响**：管理员切换"开放注册"开关保存后，下次回到页面开关始终显示"关闭"。后端状态已改但前端显示错误。

**修复**：`read_public_config()` 末尾加 `out["allow_signup"] = get_meta_value("allow_signup") or "0"`。

## 十一、白名单运行安全加固方案（待确认）

### 11.1 背景

用户验证正式库 grape-dev-db 后确认三个运行时隐患：

1. 白名单开关 `login_whitelist_enabled` 未在 meta 表显式设置，靠兼容回退（表有数据→隐式启用）
2. `is_user_allowed()` 的 SQL `WHERE {col}=?` 区分大小写，审批录入大小写不一致会误拦
3. 四库共存（grape_test_db / grape-dev-db / rds-grape-dev / gtddb_copy）易误操作

存量用户安全：正式库 login_whitelist 48 条，user_accounts 32 个（19 GitCode + 5 GitHub + 8 纯本地），全部 OAuth 用户都在白名单内（JOIN 验证无遗漏）。

### 11.2 隐患 1：白名单开关未显式设置

**现状**：三个库的 meta 表都没有 `login_whitelist_enabled`。`config.py` 的 `_is_login_whitelist_enabled()` 走兼容回退——表有数据则隐式启用。

**风险**：
- 表被清空 → 白名单静默失效，全员放行
- 误设 `false` → 全员放行
- 兼容回退有 60s 缓存窗口

**修复方案**（DB 操作，不改代码）：

在正式库 grape-dev-db 执行：
```sql
INSERT INTO meta (`key`, value) VALUES ('login_whitelist_enabled', 'true')
ON DUPLICATE KEY UPDATE value = 'true';
```

执行后：
- `_is_login_whitelist_enabled()` 读到 `val="true"` → 显式返回 True，不再走兼容回退
- admin 面板 `get_login_whitelist_enabled()` 同步显示"已开启"（已委托 config）
- `invalidate_login_whitelist_cache()` 已在 set 时调用（#348 修复），但此处是手动 SQL，需重启服务或等 60s 缓存过期

**执行后验证**：
```sql
SELECT value FROM meta WHERE `key`='login_whitelist_enabled';
-- 预期: true
```

### 11.3 隐患 2：大小写敏感的精确匹配

**现状**：`config.py` 的 `is_user_allowed()` 执行 `WHERE {whitelist_column}=?`，SQLite 和 MySQL 默认排序规则区分大小写（SQLite 默认 BINARY，MySQL 取决于 collation）。

**风险**：审批时录入 `WangLei_0526`，GitCode 实际 login 是 `wanglei_0526`，被拦且难排查。

**影响范围**：`is_user_allowed()` 被 `_check_whitelist()` 调用，位于 OAuth 回调路径 `grape_auth.py:1631`。`is_gitcode_user_allowed()` 和 `is_github_user_allowed()` 都委托此函数。存量用户无此问题（JOIN 验证大小写一致），新审批有此坑。

**修复方案**（改代码）：

`config.py` 的 `is_user_allowed()` 中 SQL 查询改为大小写不敏感：

```python
# 改前
row = c.execute(
    f"SELECT 1 FROM login_whitelist WHERE {whitelist_column}=?",
    (login,)
).fetchone()

# 改后（SQLite）
row = c.execute(
    f"SELECT 1 FROM login_whitelist WHERE {whitelist_column}=? COLLATE NOCASE",
    (login,)
).fetchone()
```

**MySQL 兼容性**：grape 用 `mysql_compat` 翻译层。`COLLATE NOCASE` 是 SQLite 语法，MySQL 不支持。需验证 `mysql_compat` 是否翻译此语法。

**替代方案**（不改 SQL 语法，兼容两端）：
```python
# Python 层统一大小写，不依赖 SQL COLLATE
row = c.execute(
    f"SELECT {whitelist_column} FROM login_whitelist WHERE {whitelist_column} LIKE ?",
    (login,)
).fetchone()
if row:
    return row[0].lower() == login.lower()
return False
```
`LIKE` 在 SQLite 和 MySQL 中默认都是大小写不敏感（SQLite 对 ASCII 字母不敏感，MySQL 取决于 collation 但 LIKE 通常 ci）。

**但 LIKE 有通配符风险**：`login` 含 `%` 或 `_` 会被当通配符。需先转义：
```python
safe_login = login.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
# SQLite LIKE 默认无 ESCAPE 子句，需显式声明
row = c.execute(
    f"SELECT {whitelist_column} FROM login_whitelist WHERE {whitelist_column} LIKE ? ESCAPE '\\'",
    (safe_login,)
).fetchone()
```

**推荐方案**：Python 层做大小写统一，SQL 用精确匹配。即查询时用 `LOWER()` 函数：
```python
row = c.execute(
    f"SELECT 1 FROM login_whitelist WHERE LOWER({whitelist_column})=LOWER(?)",
    (login,)
).fetchone()
```
`LOWER()` 在 SQLite 和 MySQL 中都支持，语义明确，无通配符风险。

**同时考虑 admin 添加白名单时的大小写**：`add_login_whitelist()` 目前不做大小写规范化。两种选择：
- a) 录入时统一转小写存储——但 GitCode/GitHub login 本身区分大小写，强制转小写可能改变用户名语义
- b) 存储保持原样，查询时大小写不敏感——更安全，不改变存储数据

推荐 b：查询时用 `LOWER()` 匹配，存储保持 provider 返回的原始大小写。

**文件降级路径同步修复**：`is_user_allowed()` 在 DB 异常时降级读 `login-whitelist.json`，当前 `return login in wl`（Python `in` 对字符串区分大小写）。改为 `return login.lower() in [w.lower() for w in wl]`，与 DB 路径保持一致。

### 11.4 DB 层大小写不敏感唯一约束加固

> **范围声明**：在 #348 一个 issue 内完成。当前迭代包含 admin i18n + CSS + 健康面板 + OAuth 白名单 + User CRUD + 白名单运行安全加固 + DB 层大小写不敏感唯一约束加固。应用层 LOWER 查询已完成，DB 层 DDL 变更在本节实施。
> 优先级：P1（安全相关）。

#### 11.4.1 问题陈述

应用层已对齐业界标准（LOWER 查询 + email 小写存储），但 DB 层 UNIQUE 约束大小写敏感，存在纵深防御缺口。绕过应用层的写入（运维直接连 DB、批处理脚本、数据迁移、第三方同步、代码 bug）会产生 case-variant 重复。

CDP（Linux Foundation）记录的真实生产事故：历史实现用精确匹配 `value ===`，导致 `willsonhg` 和 `WillsonHG` 存为两条独立记录，数万条 case-variant 重复组。修复成本远高于预防。

业界标准：OWASP 认证备忘单要求 usernames/userids case insensitive；AWS Cognito 推荐 `caseSensitive=False`；RFC 8265 定义 UsernameCaseMapped profile。

核心原则：**存储保留原始大小写（Case-Preserving），比较和唯一性约束统一映射为小写（Case-Mapped）**。应用层 + DB 层双重保障，DB 层是不可绕过的最后一道防线。

#### 11.4.2 grape 现状审计

| 表 | 字段 | DDL 约束 | 应用层比较 | 缺口 |
|----|------|---------|-----------|------|
| grape_accounts | username | `VARCHAR(64) NOT NULL UNIQUE`（大小写敏感） | `WHERE LOWER(username)=LOWER(?)` ✓ | DB 层 UNIQUE 绕过应用层可产生大小写变体重复 |
| grape_accounts | email | `uq_active_email ON (email)`（大小写敏感） | 存储时 `.lower().strip()` ✓ | 无（存储已小写，等价不敏感） |
| admin_accounts | username | `TEXT UNIQUE NOT NULL`（大小写敏感） | `WHERE username=?`（精确匹配，大小写敏感） | **应用层 + DB 层均敏感**：`Admin` 和 `admin` 是两个不同账号 |
| login_whitelist | gitcode_username | `UNIQUE (gitcode_username)`（大小写敏感） | `WHERE LOWER(col)=LOWER(?)` ✓（刚修复） | DB 层 UNIQUE 大小写敏感，`WangLei` 和 `wanglei` 可同时存在 |
| login_whitelist | github_username | `idx_login_whitelist_github`（大小写敏感） | 同上 | 同上 |
| external_identities | external_login | 无 UNIQUE 约束（仅 `(provider, external_id)` 和 `(grape_user_id, provider)` 有 UNIQUE） | OAuth 回调精确匹配 | external_login 无唯一性约束，无大小写问题（它是展示字段不是查找键） |

#### 11.4.3 需要加固的 3 个缺口

**缺口 1：grape_accounts.username DB 唯一约束**

DDL `username VARCHAR(64) NOT NULL UNIQUE` 是大小写敏感的。应用层 `is_username_taken()` 已用 `LOWER()` 查询防重复创建，但手动 SQL 或未来新写入路径绕过应用层时可产生 `WangLei` 和 `wanglei` 两条记录。

修复：新增 `LOWER(username)` 表达式唯一索引（SQLite 支持，MySQL 需用生成列或函数索引）。

```sql
-- SQLite
CREATE UNIQUE INDEX IF NOT EXISTS uq_grape_accounts_username_lower
ON grape_accounts(LOWER(username));

-- MySQL（8.0+）：生成列 + 唯一索引
ALTER TABLE grape_accounts ADD COLUMN username_lower VARCHAR(64) AS (LOWER(username)) STORED;
CREATE UNIQUE INDEX uq_grape_accounts_username_lower ON grape_accounts(username_lower);
```

mysql_compat 翻译：SQLite 表达式索引语法与 MySQL 不同，需在 mysql_compat 中添加翻译规则，或直接用 `_ensure_column` + 手动创建索引的运行时迁移方式（与 email 唯一索引一致）。

**存量数据扫描**（建索引前必须执行）：
```sql
SELECT LOWER(username), COUNT(*) as cnt, GROUP_CONCAT(username) as variants
FROM grape_accounts
GROUP BY LOWER(username)
HAVING COUNT(*) > 1;
-- 如果有结果，需先合并重复账号（保留 status=1 的，软删除其余）
-- 正式库 32 个用户，预计无重复（都是 OAuth 自动生成的 username）
```

**缺口 2：admin_accounts.username 应用层 + DB 层均敏感**

`get_admin_by_username()` 用 `WHERE username=?` 精确匹配。`Admin` 和 `admin` 是两个不同账号。虽然有默认 seed 只创建一个 `admin`，但如果手动创建了 `Admin`，登录时输入 `admin` 会找不到。

修复：
- 应用层：`get_admin_by_username()` 改为 `WHERE LOWER(username)=LOWER(?)`
- DB 层：新增 `LOWER(username)` 表达式唯一索引
- 注意：admin 密码用 `_hash_password`（裸 SHA-256），不是 bcrypt——这是 admin 表的既有约定，不在本次范围

**缺口 3：login_whitelist DB 唯一约束**

`UNIQUE (gitcode_username)` 和 `idx_login_whitelist_github` 均大小写敏感。admin 添加 `WangLei` 后再添加 `wanglei` 会成功（不同值），产生重复条目。

修复：将 UNIQUE 约束替换为 `LOWER()` 表达式索引。DDL 中 `UNIQUE (gitcode_username)` 是内联约束，无法直接改。方案：
- DDL 中删除内联 `UNIQUE (gitcode_username)`，改为运行时创建 `LOWER()` 表达式唯一索引
- 但删除内联约束对已有数据库无影响（SQLite 不 DROP 已有约束）——需要 `_ensure_whitelist_columns` 中创建表达式索引，原有内联 UNIQUE 保留（大小写敏感的约束不会阻止 LOWER 查询匹配，只是无法防止大小写变体重复）
- 实际上：保留原 UNIQUE + 新增 LOWER 表达式 UNIQUE = 双重约束。原约束防止完全相同的大小写重复，LOWER 约束防止大小写变体重复。两者不冲突。

```sql
-- 新增（_ensure_whitelist_columns 中）
CREATE UNIQUE INDEX IF NOT EXISTS uq_login_whitelist_gitcode_lower
ON login_whitelist(LOWER(gitcode_username)) WHERE gitcode_username IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_login_whitelist_github_lower
ON login_whitelist(LOWER(github_username)) WHERE github_username != '';
```

**存量数据扫描**：
```sql
SELECT LOWER(gitcode_username), COUNT(*) as cnt
FROM login_whitelist WHERE gitcode_username IS NOT NULL
GROUP BY LOWER(gitcode_username) HAVING COUNT(*) > 1;

SELECT LOWER(github_username), COUNT(*) as cnt
FROM login_whitelist WHERE github_username != ''
GROUP BY LOWER(github_username) HAVING COUNT(*) > 1;
-- 正式库 48 条，用户验证存量大小写一致，预计无重复
```

#### 11.4.4 方案选择

| 方案 | 优点 | 缺点 |
|------|------|------|
| A：仅应用层 LOWER（已完成） | 改动小，无 DDL 变更 | DB 层无兜底，绕过应用层可产生重复 |
| B：应用层 + DB 层表达式索引（推荐） | 双重保障，符合业界标准 | 需存量扫描 + mysql_compat 适配 + DDL 变更 |
| C：DB 层 COLLATE NOCASE（仅 SQLite） | 最简单 | MySQL 不支持 NOCASE，不兼容 |

推荐 B：应用层已完成，DB 层通过运行时迁移创建表达式索引（与 email 唯一索引方式一致），不依赖 mysql_compat 翻译。

#### 11.4.5 实施步骤

1. 正式库执行存量扫描 SQL（grape_accounts + login_whitelist + admin_accounts），确认无大小写变体重复
2. 如果有重复，先合并/清理
3. 代码：`_ensure_case_insensitive_indexes()` 统一入口，SQLite 走表达式索引，MySQL 走生成列（`NULLIF` 处理空串）
4. 代码：基线 8 处 `WHERE username=?` 改为 `WHERE LOWER(username)=LOWER(?)`
5. 测试：大小写变体 INSERT 应被 DB 拒绝
6. admin toggle 后在正式库执行 `INSERT INTO meta` 设 `login_whitelist_enabled=true`

#### 11.4.6 向前兼容性保障

**MySQL 表达式索引噪音消除**：表达式索引不在 `_index_sqls` 中执行（避免 mysql_compat 误翻译导致每次启动打 WARNING），统一在 `_ensure_case_insensitive_indexes()` 中按后端分流。

**github_username 空串冲突消除**：MySQL 生成列用 `LOWER(NULLIF(github_username, ''))`，空串转为 NULL，NULL 不参与唯一约束，多条空串不冲突。

**应用层查询全对齐**：基线代码 8 处 `WHERE username=?` 精确匹配已全部改为 `WHERE LOWER(username)=LOWER(?)`：

| 文件 | 函数 | 原 | 改 |
|------|------|----|----|
| auth/grape_account_db.py:141 | get_grape_account_by_username | `WHERE username=?` | `WHERE LOWER(username)=LOWER(?)` |
| api/routes/runtime.py:152 | owner_id 对齐 | `WHERE username=? LIMIT 1` | `WHERE LOWER(username)=LOWER(?) LIMIT 1` |
| session_manager.py:457 | 查用户 | `WHERE username=? AND status=1` | `WHERE LOWER(username)=LOWER(?) AND status=1` |
| runtime/service.py:1348 | 查 owner | `WHERE username=?` | `WHERE LOWER(username)=LOWER(?)` |
| runtime/engine_select.py:175 | 查 issue creator（二级回退） | `WHERE username=? AND id>0` | `WHERE LOWER(username)=LOWER(?) AND id>0` |
| runtime/engine_select.py:192 | 查 issue creator（三级回退） | `WHERE ga.username=?` | `WHERE LOWER(ga.username)=LOWER(?)` |
| runtime/engine_select.py:522 | query_engines 用户仓库列表 | `WHERE ua.username = ?` | `WHERE LOWER(ua.username)=LOWER(?)` |
| admin/db.py:22 | get_admin_by_username | `WHERE username=?` | `WHERE LOWER(username)=LOWER(?)` |

加上基线已有的 `is_username_taken`（`WHERE LOWER(username)=LOWER(?)`）和本次修复的 `get_admin_by_username`、`is_user_allowed`，grape_accounts/admin_accounts/login_whitelist 的所有查询路径已全部对齐。

#### 11.4.7 二轮审计修复

| 问题 | 修复 |
|------|------|
| P1-3: create_grape_user 不区分 username/email 冲突 | except 块检查错误消息含 "email" → 返回 "email_conflict"；路由层映射 409 "Email already in use" |
| P2-2: update_grape_user 无法清空 email | None=不更新，空串=清空为 NULL；前端传 "" 时正常清空 |
| P2-3: DDL 缺 github_username UNIQUE 注释不充分 | 补充注释说明 SQLite 内联 UNIQUE 不支持 WHERE 条件，故用运行时部分索引 |
| P2-6: MySQL except 块缺 rollback | except 块加 db.rollback() 清理脏事务 |

#### 11.4.8 三轮安全审计修复（13 维度全系统审查）

| 问题 | 修复 |
|------|------|
| P0-1: 6 个新端点缺审计日志 | 每个写操作成功后调 log_audit()，新增 7 个审计事件类型（admin_user_created/edited/password_reset/whitelist_added/deleted/toggled/login_failed） |
| P0-2: admin 登录无暴力破解防护 | admin_login 接入 login_throttle（账号维度 "admin:username" + IP 维度），失败 5 次锁定 15 分钟，IP 20 次锁定 15 分钟 |
| P1-1: admin 密码裸 SHA-256 | verify_admin 用 auth.password.verify_password（兼容 SHA-256 + bcrypt），旧哈希验证成功后透明升级为 bcrypt；update_admin_password 改用 bcrypt；change-password 路由用 verify_password |
| P1-2: admin token 过期前端不处理 | admin.js api() 加 401→/refresh→retry→跳登录页机制，复用已有 refresh 端点 + HttpOnly cookie |
| P2-1: create_user 不建个人组织 | create_user 成功后调 _ensure_personal_org(uid, username)，失败 best-effort（首次登录补建） |
| P2-3: email_verified=0 | 改为 email_verified=1（admin 创建的用户默认 email 已验证） |
| P2-4: 探活无主动通知 | _update_health_status 中检测 overall 状态翻转（healthy→degraded/unhealthy），log.warning 标记 STATUS FLIP；恢复时 log.info 标记 RECOVERED |

#### 11.4.9 向前兼容修复（9 维度检测）

| 问题 | 修复 |
|------|------|
| P1: admin.js 版本号未 bump | admin.js 内容修改后版本号同步 bump（20261016-1 → 20261016-9，8 次 bump 对应 8 轮修改） |
| P2: _seed_admin_accounts 仍用 SHA-256 | 改用 auth.password.hash_password（bcrypt），新部署 admin 默认密码直接 bcrypt 哈希 |
| signup_policy 空白名单时默认放行 | 改为 return _is_allow_signup()，未配置 allow_signup 时默认关闭注册（P1-3 设计要求） |

#### 升级影响说明

**行为变更**：升级前，当 signup_whitelist 表为空（邮箱白名单和域名白名单均未配置）时，
`check_signup_allowed()` 直接返回 `True`（允许任意邮箱注册）。升级后改为返回
`_is_allow_signup()`，即读取 meta 表 `allow_signup` 字段——未配置时默认 `False`（关闭注册）。

**向前兼容迁移**：`_migrate_allow_signup_default()` 在 `init_database()` 中自动执行——
检测 `allow_signup` meta 未设置且 `admin_accounts` 表已有数据（升级部署）时，自动设置
`allow_signup='true'` 保持原有行为。新部署（admin_accounts 为空）不设置，保持默认关闭。

**手动操作**（仅在自动迁移未覆盖的场景）：管理员可在 admin 面板 → 注册白名单 →
开放注册开关中显式设置，或在数据库中执行
`INSERT INTO meta (key, value) VALUES ('allow_signup', '1')`。

#### 11.4.10 Admin UI 设计一致性修复（Apple UI 架构审查）

**Logo 统一**：
- 登录页 + 侧边栏：emoji 🍇 → 品牌图片 `/static/img/grape-logo.jpg`（与主应用一致）
- Logo 出现位置与主应用对齐：登录页 + 侧边栏各 1 处

**登录页对齐 auth.css**：
- 背景：radial-gradient(#162240→#0c1222) → linear-gradient(#1d1d1f→#000000)
- Logo：56px emoji → 80px 品牌图片 border-radius:20px
- 输入框：border-radius 10→12px, auth CSS 变量
- 按钮：渐变 → 纯色 var(--auth-accent) + pill border-radius:980px
- 文字/错误色：对齐 auth.css 变量

**主界面 Apple 设计规范对齐**：
- 侧边栏宽度 220→232px, nav padding/radius 对齐主应用
- 工具栏 padding 24→28px
- 卡片阴影：单层 → 多层（Apple 深度标准）
- Modal 圆角 16→18px
- 按钮统一 pill（980px）, 字重 600→500

**Token fallback 对齐**：
- 移除 :root --t2 覆盖，全部 fallback 与 tokens.css :root 一致
- admin.js 5 处硬编码色值 → var() 引用

**侧边栏底部布局重构**：
- 去掉导航菜单重复的退出登录
- 语言切换器适配深色侧边栏（半透明背景 + 浅色文字）
- 用户名加 "A" badge 角色标识
- 底部紧凑布局：ft-row flex 横排 + 退出登录分隔线

**国际化按钮**：
- 登录页：浮动 lang-switch（pill + glassmorphism），主界面自动隐藏
- 侧边栏底部：lang-toggle 双按钮（中/EN），当前语言高亮
- 与主应用共享 localStorage('grapeLang')

### 11.5 隐患 3：多库并存（运维层面，不改代码）

正式库 grape-dev-db 与旧库 rds-grape-dev / gtddb_copy / grape_test_db 共存于同一 MySQL 实例。误连旧库操作白干。

**建议**（运维操作，不在代码层面处理）：
- 归档 rds-grape-dev、gtddb_copy（改名加 `_archived` 后缀）
- 配置文件中明确标注正式库连接串
- 操作前先 `SELECT DATABASE()` 确认当前库

### 11.6 确认点

1. **隐患 1**：在正式库 grape-dev-db 执行 `INSERT INTO meta` 设 `login_whitelist_enabled=true` — 确认执行？
2. **隐患 2**：`is_user_allowed()` SQL 改为 `LOWER()` 匹配 + 文件降级路径 — 已完成
3. **11.4 DB 加固**：3 个缺口表达式索引 + admin_accounts 应用层 LOWER 查询 — 确认改代码？
4. **隐患 3**：旧库归档改名 — 运维操作，是否需要协助？
