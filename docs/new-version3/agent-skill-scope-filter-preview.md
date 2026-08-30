# Agent Configuration Skill 下拉改造 — 页面效果预览

## 当前页面（改前）

```
┌─────────────────────────────────────────────────────────────────┐
│  ● VOD Agent                                                    │
│  从开发者声音中收集反馈、聚类分析共性、提炼价值                  │
│                                            [Configure] [Events] │
└─────────────────────────────────────────────────────────────────┘

┌─ Agent Configuration ───────────────────────────────────────────┐
│                                                                 │
│  Backend Engines                          [选择引擎...]          │
│  下拉多选，任务将分配到空闲引擎                                 │
│                                                                 │
│  Skill           [vod (官方) ▼]                                │
│                  ┌──────────────┐                               │
│                  │ 默认官方技能  │                               │
│                  │ vod (官方)    │  ← 只有官方 skill             │
│                  │ repo-onboard  │     仓库/个人看不到            │
│                  └──────────────┘                               │
│                                                                 │
│                                          [Save] [Cancel]        │
└─────────────────────────────────────────────────────────────────┘
```

问题：Skill 下拉只能看到官方 skill，仓库和个人 skill 看不到。

---

## 改造后页面（改后）

```
┌─────────────────────────────────────────────────────────────────┐
│  ● VOD Agent                                                    │
│  从开发者声音中收集反馈、聚类分析共性、提炼价值                  │
│                                            [Configure] [Events] │
└─────────────────────────────────────────────────────────────────┘

┌─ Agent Configuration ───────────────────────────────────────────┐
│                                                                 │
│  Backend Engines                          [选择引擎...]          │
│  下拉多选，任务将分配到空闲引擎                                 │
│                                                                 │
│  Skill           [全局] [项目] [个人]                           │
│                   ▔▔▔▔                            ┌──────────────┐
│                  点击切换 scope                   │ 默认官方技能  │
│                                                   │ vod           │  ← 当前 scope 的 skill
│                                                   │ repo-onboarding│
│                                                   └──────────────┘
│                                                                 │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │
│                                                                 │
│  点击 [项目] 时：                                               │
│                                                                 │
│  Skill           [全局] [项目] [个人]                           │
│                          ▔▔▔▔                   ┌──────────────┐
│                                                   │ my-skill      │  ← 仓库 skill
│                                                   └──────────────┘
│                                                                 │
│  点击 [个人] 时：                                               │
│                                                                 │
│  Skill           [全局] [项目] [个人]                           │
│                                 ▔▔▔▔            ┌──────────────┐
│                                                   │ my-skill      │  ← 个人 skill
│                                                   └──────────────┘
│                                                                 │
│  ⚠️ 若当前已配置的 skill 不在当前 scope，额外保留下拉：         │
│                                                                 │
│  Skill           [全局] [项目] [个人]                           │
│                   ▔▔▔▔                            ┌──────────────┐
│                                                   │ 默认官方技能  │
│                                                   │ vod           │
│                                                   │ repo-onboarding│
│                                                   │ my-skill(项目)│  ← 已配置但不在当前 scope
│                                                   └──────────────┘
│                                                                 │
│                                          [Save] [Cancel]        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 交互流程

```
用户打开 Agent Configuration
        │
        ▼
默认选中 [全局] → 加载 official skill 列表
        │
        │  点击 [项目]
        ▼
清除下拉缓存 → 带 scope=repo 查询后端
        │
        │  后端返回 scope='repo' AND owner_id=当前仓库 的 skill
        ▼
填充下拉（只显示仓库 skill）
        │
        │  点击 [个人]
        ▼
清除下拉缓存 → 带 scope=user 查询后端
        │
        │  后端返回 scope='user' AND owner_id=当前用户ID 的 skill
        ▼
填充下拉（只显示个人 skill）
        │
        │  当前已配置的 skill 不在当前 scope？
        ▼
   是 → 在下拉里额外保留（标注原 scope）
   否 → 正常显示
```

---

## 按钮样式（对齐现有设计）

```
未选中状态：灰色文字 + 灰色边框
[全局]  [项目]  [个人]

选中状态：主色背景 + 白色文字（对齐 .bt-p 样式）
[全局]  [项目]  [个人]
 ▔▔▔▔
```

CSS 变量：
- 选中：background: var(--a) (#3168e0), color: #fff
- 未选中：background: transparent, border: 1px solid var(--b) (#e3e6ee), color: var(--t2) (#5b667b)
- 圆角：var(--rd) (8px)
- 字号：11px
- padding：7px 14px

---

## 后端 API

```
GET /skills?repo=xiaowu2119/hello-skill&scope=official
→ 只返回 scope='official' 的 skill

GET /skills?repo=xiaowu2119/hello-skill&scope=repo
→ 只返回 scope='repo' AND owner_id='xiaowu2119/hello-skill' 的 skill

GET /skills?scope=user
→ 只返回 scope='user' AND owner_id=当前用户ID 的 skill

GET /skills?repo=xiaowu2119/hello-skill
（不传 scope → 保持原有行为，返回全部）
```
