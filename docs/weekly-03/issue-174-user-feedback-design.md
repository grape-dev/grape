# Grape 问题反馈功能设计方案（Issue #174）

> **版本**：v2.0（2026-09-16 实施更新）
> **状态**：已实施

## 一、背景与目标

### 1.1 需求概述

Grape 工作台用户需要一个便捷的问题反馈通道，在使用过程中遇到 bug、功能建议或体验问题时可以直接从工作台提交反馈。运营管理员可以在 admin 管理台查看和处理这些反馈。用户也可以在工作台查看自己提交的反馈列表和详情。

### 1.2 现状分析

| 维度 | 现状 |
|------|------|
| 工作台（grape 主服务） | 无问题反馈入口，用户遇到问题只能通过 IM 群或 Issue 间接沟通 |
| Admin 管理台（grape-admin） | 无反馈管理页面，运营无法集中查看和处理用户反馈 |
| 数据库 | 无反馈数据表 |

### 1.3 设计目标

- 工作台侧：顶栏右上角增加"反馈"入口（返回主页与帮助之间），弹出轻量表单提交
- 工作台侧：用户中心下增加"我的反馈"列表页，支持分页和详情查看
- Admin 管理台侧：新增"问题反馈"管理页面（列表+筛选+统计），详情使用独立页面
- 数据存储：反馈数据存储在 grape_db（工作台数据库），admin 管理台通过 grapeJdbc 跨库读取
- 附件支持：图片和 zip 文件上传，OBS 模式存 OBS 桶，非 OBS 模式存本地

---

## 二、架构设计

### 2.1 整体架构

```
Grape 工作台（用户侧）                    Grape Admin 管理台（运营侧）
┌─────────────────────┐                 ┌─────────────────────────┐
│  顶栏右上角           │                 │  侧边菜单 → 系统管理     │
│  [←主页] [💬反馈] [📖]│                 │  └── 问题反馈            │
│         │反馈入口     │                 │                         │
│         ▼            │                 │  FeedbackManage.vue     │
│  ┌──────────────┐    │                 │  ├── 统计卡片            │
│  │ 反馈弹窗      │    │                 │  ├── 筛选（状态/类型）    │
│  │ 类型/标题/描述│    │                 │  ├── 列表表格            │
│  │ 附件上传      │    │                 │  └── 点击跳转详情页      │
│  └──────┬───────┘    │                 │                         │
│         │ POST        │  FeedbackDetail.vue     │
│         ▼            │  ├── 居中卡片布局        │
│  grape_db (SQLite/   │  ├── 基本信息/描述/附件  │
│  MySQL RDS)          │  ├── 处理信息           │
│                      │  ├── 状态操作区         │
│  用户中心 → 我的反馈  │  └── 确定按钮+成功弹窗  │
│  ├── 列表（分页）     │                         │
│  └── 点击跳转详情页   └─────────────────────────┘
│                      │              │
│  my_feedback_detail  │              │ grapeJdbc
│  .js (独立页面)       │              │ (跨库读 grape_db)
└──────────┬───────────┘              │
           │                          ▼
           ▼              ┌─────────────────────────┐
┌──────────────────────────┐│  Admin Backend           │
│  grape_db（工作台数据库）  ││  FeedbackController.java │
│  ┌──────────────────────┐││  ├── GET /feedback/list  │
│  │  user_feedback       │││  ├── PUT /feedback/{id}  │
│  │  (反馈数据表)         │││  └── GET /feedback/stats │
│  └──────────────────────┘│└──────────┬──────────────┘
│                          │           │
│  工作台 API:              │           │
│  POST /api/feedback       │           │
│  POST /api/feedback/upload│           │
│  GET  /api/feedback/mine  │           │
│  GET  /api/feedback/stats │           │
│  PUT  /api/feedback/{id}/status│      │
└──────────────────────────┘
```

### 2.2 数据流

1. **提交反馈**：工作台用户点击"反馈"按钮 → 弹出表单 → 可选上传附件（图片/zip）→ POST 到 grape 工作台 API → 直接写入 grape_db
2. **查看我的反馈**：工作台用户点击"我的反馈" → GET /api/feedback/mine → 分页展示列表 → 点击条目跳转详情页
3. **Admin 查看列表**：运营管理员在 admin 管理台打开"问题反馈" → admin 后端通过 grapeJdbc 查询 grape_db → 返回列表
4. **Admin 处理反馈**：运营点击条目跳转 FeedbackDetail.vue → 修改状态/优先级/备注 → 点击"确定" → 成功弹窗（返回列表/留在当前页）
5. **附件访问**：admin 管理台通过 proxy.py 代理 /static/uploads/ 到 grape 工作台 38080

### 2.3 关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 反馈数据存储位置 | grape_db（工作台数据库） | 反馈是工作台用户产生的数据，与工作台用户信息在同一库；admin 通过 grapeJdbc 跨库读取 |
| 提交 API 归属 | grape 工作台（Python FastAPI） | 数据在 grape_db，工作台直接读写，无需跨服务调用 |
| Admin 如何读取 | grapeJdbc（Spring JdbcTemplate） | 复用 admin 后端已有的 grapeJdbc 配置（健康中心等功能也用此方式读 grape_db） |
| 附件上传 | 工作台 API + OBS/本地双模式 | OBS 模式存 OBS 桶，非 OBS 模式存本地 /static/uploads/feedback/ |
| 前端入口位置 | 顶栏右上角（返回主页与帮助之间） | 全局可见，用户在任何页面都能快速反馈 |
| 我的反馈入口 | 侧边栏"用户中心"分组下 | 与 Chat Agent / IM渠道同属用户中心 |
| Admin 详情页 | 独立页面（FeedbackDetail.vue） | 不用弹窗，支持浏览器前进/后退，内容居中展示 |
| 工作台详情页 | 独立页面（my_feedback_detail.js） | 通过 app.nav 路由跳转，不用弹窗/内联展开 |
| 附件原始文件名 | attachment_name 字段 | 上传时保存原始文件名，展示时显示 |

---

## 三、数据库设计

### 3.1 user_feedback 表

存储在 grape_db（工作台数据库），通过 db_setup.py 的 _NEW_TABLES 机制创建。

```sql
CREATE TABLE IF NOT EXISTS user_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INT          NOT NULL,
    username        TEXT         NOT NULL,
    category        TEXT         NOT NULL,      -- bug/suggestion/question/other
    title           TEXT         NOT NULL,
    description     TEXT         NOT NULL,
    page_url        TEXT         DEFAULT '',
    repo_key        TEXT         DEFAULT '',
    user_agent      TEXT         DEFAULT '',
    attachment_url  TEXT         DEFAULT '',
    attachment_name TEXT         DEFAULT '',     -- 原始文件名
    status          TEXT         NOT NULL DEFAULT 'open',
    priority        INTEGER      NOT NULL DEFAULT 0,
    handler_name    TEXT         DEFAULT '',
    handler_remark  TEXT         DEFAULT '',
    handled_at      TEXT         DEFAULT NULL,
    created_at      TEXT         DEFAULT (datetime('now')),
    updated_at      TEXT         DEFAULT (datetime('now'))
);
```

> 注：实际运行在 MySQL RDS (127.0.0.1:3306/grape_db)，db_setup.py 的 MySQL 兼容层会自动转换 SQLite 语法。

### 3.2 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| category | text | bug=缺陷报告, suggestion=功能建议, question=使用疑问, other=其他 |
| status | text | open=待处理, in_progress=处理中, resolved=已解决, closed=已关闭 |
| priority | int | 0=普通, 1=紧急, 2=高优（运营手动调整） |
| attachment_url | text | 附件 URL（OBS 或本地 /static/uploads/feedback/xxx） |
| attachment_name | text | 附件原始文件名（上传时保存） |
| handler_name | text | 处理人（admin 用户名） |
| handler_remark | text | 处理备注 |

---

## 四、后端 API 设计

### 4.1 工作台侧 API（grape 主服务，Python FastAPI）

#### 4.1.1 提交反馈

```
POST /api/feedback
```

JWT 认证，从 token 解析 user_id/username。

```json
// Request
{
  "category": "bug",
  "title": "引擎列表页加载缓慢",
  "description": "打开设备监控页面时，引擎列表需要 5 秒以上才能加载完成",
  "page_url": "#observability",
  "repo_key": "gitcode:grape-dev/grape",
  "attachment_url": "/static/uploads/feedback/xxx.png",
  "attachment_name": "截图.png"
}

// Response 200
{
  "id": 1,
  "status": "open"
}
```

#### 4.1.2 上传附件

```
POST /api/feedback/upload
```

JWT 认证，接受 multipart/form-data 文件上传。支持图片（png/jpg/jpeg/webp/gif）和 zip 文件，最大 5MB。

- OBS 模式：上传到 OBS 桶，返回 OBS URL
- 非 OBS 模式：存到本地 `src/api/static/uploads/feedback/`，返回 `/static/uploads/feedback/xxx` 路径

```json
// Response 200
{
  "url": "/static/uploads/feedback/db5bb41e34614018.png",
  "file_key": "feedback/db5bb41e34614018.png",
  "is_zip": false,
  "original_name": "截图.png"
}
```

#### 4.1.3 查看我的反馈

```
GET /api/feedback/mine?page=0&size=10
```

JWT 认证，按当前用户 user_id 过滤，分页返回。

```json
{
  "items": [...],
  "total": 5,
  "page": 0,
  "total_pages": 1
}
```

#### 4.1.4 反馈统计

```
GET /api/feedback/stats
```

返回按状态和类型分组的统计数据。

#### 4.1.5 更新反馈状态（admin 调用）

```
PUT /api/feedback/{id}/status
```

```json
{
  "status": "in_progress",
  "priority": 1,
  "handler_remark": "已确认问题，正在排查"
}
```

### 4.2 Admin 后端 API（grape-admin Java Spring Boot）

Admin 后端通过 grapeJdbc（JdbcTemplate）直接读写 grape_db 的 user_feedback 表。

#### 4.2.1 反馈列表

```
GET /api/admin/feedback/list?status=open&category=bug&page=0&size=20
```

JWT 认证（admin 用户），支持状态/类型/搜索筛选 + 分页。返回 attachments 数组格式（包含 url + name）。

#### 4.2.2 更新反馈状态

```
PUT /api/admin/feedback/{id}/status
```

JWT 认证，从 admin token 解析 handler 信息。

#### 4.2.3 反馈统计

```
GET /api/admin/feedback/stats
```

---

## 五、前端设计

### 5.1 Grape 工作台 — 反馈入口

#### 5.1.1 入口位置

顶栏右上角，返回主页与帮助之间：

```
标题 | ... | [← 返回主页] [💬 反馈] [📖 帮助]
```

#### 5.1.2 反馈弹窗（feedback.js）

- 类型选择：4 个按钮（Bug 缺陷/功能建议/使用疑问/其他）
- 标题输入框
- 详细描述 textarea
- 附件上传：使用 API.upload() 自动带 JWT 认证，支持图片和 zip（≤5MB）
- 上传成功显示原始文件名 + ✓
- 提交后 showToast 成功提示并关闭弹窗
- 不展示页面信息（去除早期的 📍 #dash 显示）

#### 5.1.3 我的反馈列表（my_feedback.js）

- 侧边栏"用户中心"分组下，Chat Agent / IM渠道 之后
- 列表卡片展示：状态标签 + 标题 + 类型 + 时间 + 附件缩略图 + 处理备注预览
- 支持分页（上一页/下一页）
- 点击条目跳转到详情页：app.nav('my-feedback-detail', id)

#### 5.1.4 反馈详情页（my_feedback_detail.js）

- 独立页面（通过 app.nav 路由跳转），不用弹窗/内联展开
- 返回按钮：app.nav('my-feedback')
- 内容：基本信息网格 + 标题 + 详细描述 + 附件预览（图片缩略图/文件下载链接）+ 处理信息
- 支持 #my-feedback-detail/{id} hash 路由

### 5.2 Grape Admin 管理台 — 反馈管理

#### 5.2.1 路由

```js
// router/index.js
{ path: '/feedback/manage', name: 'feedback.manage',
  component: () => import('../views/FeedbackManage.vue'), meta: { title: '问题反馈' } },
{ path: '/feedback/:id', name: 'feedback.detail',
  component: () => import('../views/FeedbackDetail.vue'), meta: { title: '反馈详情' } },
```

#### 5.2.2 列表页（FeedbackManage.vue）

- 统计卡片行：待处理/处理中/已解决/已关闭/全部（可点击筛选）
- 筛选栏：状态下拉 + 类型下拉 + 搜索框
- 列表表格：ID/提交人/类型/标题/状态/优先级/提交时间/操作
- 点击标题或"详情"按钮跳转到 FeedbackDetail.vue
- 分页

#### 5.2.3 详情页（FeedbackDetail.vue）

- 独立页面（不用弹窗），返回按钮回到列表
- 内容居中展示（max-width:760px 卡片式布局，圆角阴影）
- 内容区块：基本信息网格 + 标题 + 详细描述 + 附件（图片预览+原始文件名+下载链接）+ 处理信息
- 状态操作区：状态下拉 + 优先级下拉 + 处理备注 textarea
- "确定"按钮（右下角，justify-content:flex-end）
- 点击确定成功后弹出确认对话框：
  - ✅ 处理成功
  - 留在当前页面 / 返回列表

#### 5.2.4 附件展示

- 后端返回 attachments 数组格式：`[{ url: "...", name: "原始文件名" }]`
- 前端使用蛇形命名（attachment_url / attachment_name / created_at 等）
- 图片：img 缩略图预览（可点击放大）+ 文件名下载链接
- 非图片：📎 图标 + 文件名下载链接

### 5.3 代理配置（proxy.py）

admin 管理台 8082 端口的 proxy.py 代理 /static/uploads/ 路径到 grape 工作台 38080，使非 OBS 模式下的附件图片在 admin 管理台可访问。

---

## 六、安全与权限

### 6.1 工作台用户侧

- 提交反馈/上传附件/查看我的反馈需要登录（JWT 认证）
- 附件上传限制 5MB，支持图片和 zip 类型
- 用户只能查看自己的反馈（/feedback/mine 按 user_id 过滤）

### 6.2 Admin 管理台侧

- 查看和处理反馈需要 admin 登录（JWT 认证）
- admin 通过 grapeJdbc 跨库读写 grape_db.user_feedback

---

## 七、代码修改清单

### 7.1 Grape 工作台（grape 主服务）

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | 新增 user_feedback 表（含 attachment_name 列） |
| `src/api/routes/feedback.py` | 新增：POST 提交 / POST 上传 / GET mine / GET stats / PUT status |
| `src/api/main.py` | 注册 feedback router |
| `src/api/static/js/pages/feedback.js` | 新增：反馈弹窗组件（API.upload 带认证） |
| `src/api/static/js/pages/my_feedback.js` | 新增：我的反馈列表页（分页+跳转详情） |
| `src/api/static/js/pages/my_feedback_detail.js` | 新增：反馈详情页（独立页面） |
| `src/api/static/js/app.js` | VIEWS 注册 my-feedback / my-feedback-detail + nav 分支 + noRepoViews |
| `src/api/static/index.html` | 引入 3 个 JS + 顶栏反馈按钮（返回主页与帮助之间）+ 侧边栏我的反馈 |
| `src/api/static/locales/zh/app.json` | 反馈相关 i18n |
| `src/api/static/locales/en/app.json` | 同上英文 |

### 7.2 Grape Admin 管理台

| 文件 | 改动 |
|------|------|
| `backend/.../controller/FeedbackController.java` | 新增：grapeJdbc CRUD API（list/status/stats） |
| `frontend/src/views/FeedbackManage.vue` | 新增：列表页（统计+筛选+表格+分页） |
| `frontend/src/views/FeedbackDetail.vue` | 新增：详情页（居中布局+附件预览+确定按钮+成功弹窗） |
| `frontend/src/router/index.js` | 新增 /feedback/manage + /feedback/:id 路由 |

### 7.3 部署配置

| 文件 | 改动 |
|------|------|
| `grape-admin-deploy/proxy.py` | 新增 /static/uploads/ 代理到 grape 工作台 38080 |

---

## 八、实施记录

| 步骤 | 内容 | 状态 |
|------|------|------|
| 1 | db_setup.py 建表 + feedback.py CRUD API | ✅ 已完成 |
| 2 | 工作台 feedback.js 弹窗 + index.html 入口 | ✅ 已完成 |
| 3 | 附件上传支持 OBS + zip + 原始文件名 | ✅ 已完成 |
| 4 | 上传改用 API.upload（自动带 JWT 认证） | ✅ 已完成 |
| 5 | admin 后端 FeedbackController（grapeJdbc） | ✅ 已完成 |
| 6 | admin 前端 FeedbackManage.vue 列表页 | ✅ 已完成 |
| 7 | admin 前端 FeedbackDetail.vue 详情页（独立页面） | ✅ 已完成 |
| 8 | 详情页布局优化（居中+确定按钮右下角+成功弹窗） | ✅ 已完成 |
| 9 | proxy.py 代理 /static/uploads/ | ✅ 已完成 |
| 10 | 工作台"我的反馈"列表页（分页） | ✅ 已完成 |
| 11 | 工作台反馈详情页（独立页面） | ✅ 已完成 |
| 12 | 侧边栏入口调整（用户中心分组下） | ✅ 已完成 |
| 13 | 顶栏反馈按钮位置调整（返回主页与帮助之间） | ✅ 已完成 |
| 14 | 附件展示字段名修复（蛇形命名） | ✅ 已完成 |
| 15 | i18n 支持 | ✅ 已完成 |

---

## 九、验证方案

### 9.1 功能验证

1. 工作台登录用户点击"反馈"按钮 → 弹窗正常显示
2. 填写表单 + 上传图片/zip → 提交成功 → grape_db 有新记录
3. 工作台"我的反馈"列表 → 分页正常 → 点击跳转详情页
4. Admin 管理台"问题反馈"列表 → 统计卡片 + 筛选 + 分页正常
5. 点击列表条目 → 跳转 FeedbackDetail.vue → 内容居中展示
6. 修改状态 + 填写备注 → 点击"确定" → 成功弹窗 → 选择返回列表/留在当前页
7. 附件图片在 admin 管理台正常展示（proxy.py 代理）

### 9.2 边界验证

1. 未登录用户不能提交反馈（API 返回 401）
2. 附件超过 5MB → 前端拦截提示
3. 附件非图片/zip 类型 → 后端拒绝
4. /feedback/mine size=100 不再 422（上限已调整为 100）
5. 附件 URL 在 admin 管理台通过 proxy.py 可访问
