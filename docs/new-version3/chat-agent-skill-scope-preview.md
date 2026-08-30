# Chat Agent Skill 配置改造预览

## 当前页面（改前）

```
┌─ Chat Agent ────────────────────────────────────┐
│                                                  │
│  Skill                                           │
│  ┌──────────────────────────────────────────┐   │
│  │ chat (官方) ▼                            │   │  ← 只有官方 skill
│  │ im-notify (官方)                         │   │     仓库/个人看不到
│  └──────────────────────────────────────────┘   │
│                                                  │
│  [Save]                                          │
└──────────────────────────────────────────────────┘
```

---

## 改造后页面（改后）— scope 和 skill 同一行

```
┌─ Chat Agent ────────────────────────────────────┐
│                                                  │
│  Skill   [全局▼] [chat                    ▼]    │
│           [项目 ]                                  │  ← 当前 scope 的 skill
│           [个人 ]                                  │
│                                                  │
│  点击 [项目] 时：                                 │
│                                                    │
│  Skill   [全局 ] [my-skill                ▼]    │  ← 仓库 skill
│           [项目▼]                                  │
│           [个人 ]                                  │
│                                                  │
│  点击 [个人] 时：                                 │
│                                                    │
│  Skill   [全局 ] [my-skill                ▼]    │  ← 个人 skill
│           [项目 ]                                  │
│           [个人▼]                                  │
│                                                  │
│  [Save]                                          │
└──────────────────────────────────────────────────┘
```

scope 下拉框和 skill 下拉框在同一行，左边 scope（固定宽度 70px），右边 skill（flex 撑满）。
和 DevOps Agent Configuration 的 Skill 行布局一致。

---

## 交互逻辑

1. 默认选中 [全局] → 加载 official skill
2. 点击 [项目] → 带 scope=repo 查询后端 → 刷新 skill 下拉
3. 点击 [个人] → 带 scope=user 查询后端 → 刷新 skill 下拉
4. 当前已配置的 skill 在当前 scope → 选中它
5. 不在当前 scope → 默认选第一个
6. 全局 scope 显示"默认官方技能（自动）"选项；项目/个人不显示
7. 保存时传 skill_id（直传 id）

---

## 前端改动点（chat.js）

| 改动 | 说明 |
|------|------|
| Skill 行改为 flex 布局，scope 下拉框 + skill 下拉框同行 | 对齐 DevOps Agent |
| scope 下拉框固定宽度 70px | flex:0 0 70px |
| option value 改 skill_id | 从 "scope:name" 改为 skill_id |
| saveConfig 传 skill_id | 不传 skill + skill_scope |
| 回显用 skill_id 匹配 | 从 scope:name 改为 id |
| scope 初始值从 skill_scope | 根据已配置 skill 的 scope 设置初始页签 |
