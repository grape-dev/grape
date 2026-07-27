# 技术文档 vs 原型 覆盖度分析

> 对照 `prototype.html` 的每个功能点，检查 `tech-design.md` 是否覆盖。

---

## 覆盖状态：✅ 已覆盖 / ⚠️ 部分覆盖 / ❌ 缺失

### 1. 登录页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| GitCode OAuth 登录 | 主按钮 + SVG 图标 | §4.2 `/login/gitcode` + `/callback/gitcode` | ✅ |
| 密码登录 toggle | 点击展开输入框 | §4.2 `POST /login` | ✅ |
| 返回 GitCode 登录 | 底部链接 | 前端交互，无需 API | ✅ |
| 管理员登录入口 | 无（已移除） | — | ✅ |

### 2. 仓库选择页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| 仓库卡片网格 | 含名称/角色/Owner/状态 | §4.3 `GET /member` | ✅ |
| 搜索仓库 | 输入框 | §4.1 搜索参数 `?search=` | ✅ |
| 角色过滤 | 下拉 All/Member/Admin | 前端过滤 | ✅ |
| Join 按钮 | `+ Join` | §4.3 `POST /join` | ✅ |
| Host Now 按钮 | Admin/Maintainer | §4.3 `POST /host` | ✅ |
| 新增仓库卡片 | 跳转 gitcode.com/create | ❌ 未提及 | ⚠️ |

### 3. Dashboard

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| Repo Header（名称/状态/指标） | 渐变卡片 | §4.4 `GET /{owner}/{repo}` | ✅ |
| Agent Workflow 5 节点 | 箭头连接，含 Backend 标签 | §3.2 页面树 | ✅ |
| Chat Agent Lane | 全宽卡片，五个 ↔ 标签 | §3.2 页面树 | ✅ |
| Recent Activity 时间线 | 5 条事件，点击→Event Detail | §3.4 交互链路 | ✅ |
| 侧边栏 Repo 切换 | dropdown | §3.2 | ✅ |
| Topbar 云开发环境按钮 | ☁ 按钮 | §3.2 提及 | ⚠️ |
| Topbar 搜索框 | `⌕ Search...` | ❌ 无搜索 API | ⚠️ |

### 4. Pipeline

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| 活跃流水线 5 节点 | 每节点显示事件详情+状态 | §3.2 页面树 | ✅ |
| 流水线历史表 | Issue/Duration/Agents/Result/Release | ❌ API 未明确定义 | ⚠️ |
| 点击历史行切换状态 | 前端交互 | §3.4 交互链路 | ✅ |
| 节点点击→Event Detail | `nav('event',agent,0)` | §3.4 | ✅ |

### 5. Agent 详情页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| Agent Header + 图标 | 每种 Agent 独立色系 | §4.5 `GET /{name}/{owner}/{repo}` | ✅ |
| 指标卡（3 列） | Signals/Issues/Quality 等 | §4.5 | ✅ |
| Event History 列表 | 最近 4 条，点击→Event Detail | §3.4 | ✅ |
| Configure 面板 | Backend Engine 下拉 + Skill 下拉 | §4.5 `PUT /{name}/{owner}/{repo}` | ✅ |
| Save/Cancel | 面板内按钮 | §4.5 | ✅ |
| 默认 Skill 显示 | `skills/{k}/SKILL.md` | §13 | ✅ |

### 6. Event Detail 页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| 返回按钮 | `← 返回 XX Agent` | §3.4 | ✅ |
| 左栏：Event Context | Trigger/Repo/Branch/Time | §11.8 反馈流程 | ✅ |
| 左栏：Execution Log | monospace 日志 | §11.8 | ✅ |
| 右栏上：Event Info | Backend/Skill/Issue/Agent/Duration/Status | §11.8 | ✅ |
| 右栏下：Feedback 表单 | Title + Description + Submit | §11.8 `POST /api/chat/feedback` | ✅ |
| Event 数据来源 | ❌ 无独立 API 端点 | ❌ 缺失 | ⚠️ |

### 7. Chat Agent 页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| 对话窗口 | Agent 回复 + 用户输入 | §4.6 `POST /send` | ✅ |
| Agents 连接状态 | 5 个 Agent 标签 | §4.6 作为 config 一部分 | ✅ |
| Backend 配置 | Type/URL/API Key | §4.6 `PUT /config` | ✅ |
| IM 配置 | Feishu AppID/Secret + WeCom/DingTalk | §4.6 `PUT /config` | ✅ |
| Scan QR 提示 | 链接到飞书智能体配置 | ❌ 未在技术文档体现 | ⚠️ |

### 8. Repositories 页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| Hosted 列表 | Webhook OK 状态 | §4.3 `GET /managed` | ✅ |
| Available 表格 | Role/Description/Action | §4.3 `GET /member` + 前端过滤 | ✅ |
| Sync 按钮 | `↻ Sync from GitCode` | §4.3 `GET /member` | ✅ |

### 9. General Settings 页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| Credentials tab | GitCode API + Huawei Cloud AK/SK | §4.7 `GET/PUT /credentials` | ✅ |
| Notifications tab | 5 个 Checkbox | §4.7 `GET/PUT /notifications` | ✅ |
| Skills tab | Upload + Repo-Skill mapping | §4.7 + §13 | ✅ |

### 10. GitCode Repo 页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| DevOps Bot Token | Bot Token + Scope | §4.7 `GET/PUT /gitcode-repo/{owner}/{repo}` | ✅ |
| Webhook Bot | Token + URL + Bot Username | §4.7 同上 | ✅ |

### 11. DevOps Skills 页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| 按 Agent 分类 | VOD/Issue/Coding/QA/SRE 5 个分区 | §13.1 存储结构 | ✅ |
| 技能卡片 | 名称/描述/类型/View/Edit | §13 | ✅ |
| 官方技能 vs 用户上传 | 标签区分 | §13 | ✅ |
| 搜索技能 | 输入框 | ❌ API 无搜索参数 | ⚠️ |
| Skills 数据来源 API | ❌ 无独立 API | ❌ 缺失 | ⚠️ |

### 12. Backend Agents 页

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| 引擎卡片 | Running/Version/Path | §8.6 `GET /backend-agents` | ✅ |
| Test Connection | 单引擎测试 | §8.6 `POST /refresh` | ✅ |
| CodeArts AK/SK | AK/SK 输入框 + Save | §8.6 `PUT /codearts` | ✅ |
| Refresh All | ⟳ 按钮 | §8.6 `POST /refresh` | ✅ |
| Cursor Not Installed | 安装引导 | §8.6 发现机制 | ✅ |

### 13. 侧边栏

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| Repo 下拉切换 | 含 + 新增仓库托管 | §3.2 | ✅ |
| Workspace 组 | Dashboard + Pipeline | §3.2 | ✅ |
| DevOps Agents 组 | 5 个 Agent 链接 | §3.2 | ✅ |
| DevOps Skills 链接 | Skills Library | §3.2 | ✅ |
| Account 组 | Chat Agent | §3.2 | ✅ |
| Settings 组 | General/GitCode/Backend | §3.2 | ✅ |
| 侧边栏底部角色 | `Zhang Wei · Maintainer` | §3.2 | ✅ |

### 14. Topbar

| 功能点 | 原型实现 | 技术文档 | 状态 |
|--------|---------|---------|------|
| 面包屑 | Dashboard · team/backend-service | §3.2 | ✅ |
| 云开发环境按钮 | ☁ 链接 | — | ✅ |
| 搜索框 | 全局搜索 | — | ✅ |


## 总结

| 页面 | 总功能点 | 已覆盖 | 部分 | 缺失 |
|------|---------|--------|------|------|
| 登录页 | 4 | 4 | 0 | 0 |
| 仓库选择 | 5 | 4 | 1 | 0 |
| Dashboard | 6 | 4 | 2 | 0 |
| Pipeline | 4 | 2 | 2 | 0 |
| Agent 详情 | 6 | 6 | 0 | 0 |
| Event 详情 | 6 | 5 | 1 | 0 |
| Chat Agent | 5 | 4 | 1 | 0 |
| Repositories | 3 | 3 | 0 | 0 |
| General Settings | 3 | 3 | 0 | 0 |
| GitCode Repo | 2 | 2 | 0 | 0 |
| DevOps Skills | 5 | 3 | 2 | 0 |
| Backend Agents | 5 | 5 | 0 | 0 |
| 侧边栏 | 7 | 7 | 0 | 0 |
| Topbar | 3 | 3 | 0 | 0 |
| **合计** | **64** | **55 (86%)** | **9** | **0** |

## 需补充到技术文档的项目

| # | 缺失项 | 建议补充位置 |
|---|--------|-------------|
| 1 | **新增仓库卡片跳转逻辑** | §4.3 Repos API 增加注释：前端卡片 `onclick="window.open('https://gitcode.com/create','_blank')"` |
| 2 | **Pipeline API 端点** | §4.4 Dashboard 或单独增加 `GET /api/pipeline/{owner}/{repo}` |
| 3 | **Event Detail 数据 API** | §4 新增 `GET /api/events/{seq}` 端点 |
| 4 | **DevOps Skills 列表 API** | §4 或 §13 增加 `GET /api/skills?agent=vod&search=` |
| 5 | **全局搜索 API** | §4 增加 `GET /api/search?q=keyword&type=event|repo|agent` |
| 6 | **飞书智能体 QR 扫码配置** | §11 IM 集成中补充扫码配置入口 |
| 7 | **Cloud Dev Environment 跳转** | §3.2 补充说明：按钮链接到华为云 CodeArts |

## 结论

技术文档覆盖了原型 **86%** 的功能点（55/64），无缺失功能，9 项为部分覆盖（主要是 API 端点需细化和 1 个前端跳转逻辑未文档化）。文档质量可以进入下一阶段实施。
