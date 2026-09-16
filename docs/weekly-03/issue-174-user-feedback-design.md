# Grape 问题反馈功能设计方案（Issue #174）

## 一、背景与目标

### 1.1 需求概述

Grape 工作台用户需要一个便捷的问题反馈通道，在使用过程中遇到 bug、功能建议或体验问题时可以直接从工作台提交反馈。运营管理员可以在 admin 管理台查看和处理这些反馈。

### 1.2 现状分析

| 维度 | 现状 |
|------|------|
| 工作台（grape 主服务） | 无问题反馈入口，用户遇到问题只能通过 IM 群或 Issue 间接沟通 |
| Admin 管理台（grape-admin） | 无反馈管理页面，运营无法集中查看和处理用户反馈 |
| 数据库 | 无反馈数据表，admin_db 中缺少相关存储 |

### 1.3 设计目标

- 工作台侧：在侧边栏底部（帮助按钮旁）增加"问题反馈"入口，弹出轻量表单提交
- Admin 管理台侧：新增"问题反馈"管理页面，支持列表查看、筛选、状态处理
- 数据隔离：反馈数据存储在 admin_db（运营管理台独立数据库），工作台通过 API 跨库写入
- 轻量实现：不引入新的中间件，复用现有 grape-admin 后端 + grape 工作台前端

---

## 二、架构设计

### 2.1 整体架构

```
Grape 工作台（用户侧）                    Grape Admin 管理台（运营侧）
┌─────────────────────┐                 ┌─────────────────────────┐
│  侧边栏底部           │                 │  侧边菜单 → 问题反馈      │
│  ┌───┐ ┌───┐ ┌───┐  │                 │                         │
│  │ ⚙ │ │ 💡│ │ ↩ │  │                 │  FeedbackManage.vue     │
│  └───┘ └───┘ └───┘  │                 │  ├── 列表表格            │
│         │反馈入口     │                 │  ├── 筛选（状态/类型）    │
│         ▼            │                 │  ├── 详情弹窗            │
│  ┌──────────────┐    │                 │  └── 状态操作            │
│  │ 反馈弹窗      │    │                 │                         │
│  │ 类型/内容/截图 │    │                 └─────────────────────────┘
│  └──────┬───────┘    │                          │
│         │ POST        │                          │ GET/PUT
│         ▼            │                          ▼
└─────────┼────────────┘              ┌─────────────────────────┐
          │                           │  Admin Backend           │
          │                           │  FeedbackController.java │
          │                           │  ├── GET /feedback/list  │
          │                           │  ├── PUT /feedback/{id}  │
          │                           │  └── GET /feedback/stats │
          │                           └──────────┬──────────────┘
          │                                      │
          ▼                                      ▼
┌─────────────────────────────────────────────────────────┐
│              admin_db（运营管理台数据库）                   │
│              ┌─────────────────────────┐                │
│              │   admin_user_feedback    │                │
│              │   (反馈数据表)            │                │
│              └─────────────────────────┘                │
└─────────────────────────────────────────────────────────┘
```

### 2.2 数据流

1. **提交反馈**：工作台用户点击"问题反馈"按钮 → 弹出表单 → 填写内容 → POST 到 admin 后端 API → 写入 admin_db
2. **查看列表**：运营管理员在 admin 管理台打开"问题反馈"页面 → GET 列表 → 展示表格
3. **处理反馈**：运营点击某条反馈 → 修改状态（待处理→处理中→已解决/已关闭）→ PUT 更新 → 可追加处理备注

### 2.3 关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 反馈数据存储位置 | admin_db（grape_admin_db） | 反馈是运营管理范畴的数据，与 admin 用户/角色/审计在同一库 |
| 提交 API 归属 | grape-admin 后端（Java） | 数据在 admin_db，由 admin 后端统一管理读写 |
| 工作台如何调用 admin API | 直接 HTTP 调用（跨服务） | 工作台已有 admin 后端地址配置（用于 AI 助手等功能） |
| 认证方式 | 工作台 JWT 用户身份 → admin 后端验证 | 复用现有工作台 JWT，admin 后端解析用户信息 |
| 前端入口位置 | 侧边栏底部（帮助按钮旁） | 不侵入主导航，与帮助/登出同属辅助功能区 |

---

## 三、数据库设计

### 3.1 admin_user_feedback 表

存储在 admin_db（grape_admin_db），通过 Flyway 迁移创建。

```sql
-- V10__user_feedback.sql
CREATE TABLE IF NOT EXISTS admin_user_feedback (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    -- 提交者信息（从工作台 JWT 解析）
    user_id         INT          NOT NULL COMMENT '工作台用户 ID（user_accounts.id）',
    username        VARCHAR(128) NOT NULL COMMENT '工作台用户名',
    -- 反馈内容
    category        VARCHAR(32)  NOT NULL COMMENT '类型：bug/suggestion/question/other',
    title           VARCHAR(200) NOT NULL COMMENT '反馈标题（一句话概述）',
    description     TEXT         NOT NULL COMMENT '详细描述',
    -- 上下文信息（自动采集）
    page_url        VARCHAR(500) DEFAULT '' COMMENT '提交时所在页面 URL',
    repo_key        VARCHAR(200) DEFAULT '' COMMENT '当前仓库（如有）',
    user_agent      VARCHAR(500) DEFAULT '' COMMENT '浏览器 UA',
    -- 附件
    attachment_url  VARCHAR(500) DEFAULT '' COMMENT '截图/附件 URL（OBS 或本地存储）',
    -- 状态管理
    status          VARCHAR(20)  NOT NULL DEFAULT 'open' COMMENT 'open/in_progress/resolved/closed',
    priority        TINYINT      NOT NULL DEFAULT 0 COMMENT '0=普通 1=紧急 2=高优',
    -- 处理信息
    handler_id      BIGINT       DEFAULT NULL COMMENT '处理人（admin 用户 ID）',
    handler_name    VARCHAR(128) DEFAULT '' COMMENT '处理人用户名',
    handler_remark  TEXT         DEFAULT NULL COMMENT '处理备注',
    handled_at      DATETIME     DEFAULT NULL COMMENT '处理时间',
    -- 时间戳
    created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    -- 索引
    KEY idx_user (user_id),
    KEY idx_status (status),
    KEY idx_category (category),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户问题反馈';
```

### 3.2 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| category | enum | bug=缺陷报告, suggestion=功能建议, question=使用疑问, other=其他 |
| status | enum | open=待处理, in_progress=处理中, resolved=已解决, closed=已关闭 |
| priority | tinyint | 运营手动调整，默认 0=普通，1=紧急，2=高优 |
| page_url | varchar | 自动采集当前页面 hash，帮助定位用户遇到问题的具体页面 |
| repo_key | varchar | 自动采集当前仓库（如有），帮助定位上下文 |
| attachment_url | varchar | 截图上传后的 URL（可选，复用工作台已有上传接口） |
| handler_remark | text | 运营处理时填写的备注，每次状态变更可追加 |

---

## 四、后端 API 设计

### 4.1 工作台侧 API（grape 主服务）

工作台侧仅提供一个轻量代理——将用户反馈转发到 admin 后端。不直接操作 admin_db。

#### POST /api/feedback

提交问题反馈。

```json
// Request
{
  "category": "bug",
  "title": "引擎列表页加载缓慢",
  "description": "打开设备监控页面时，引擎列表需要 5 秒以上才能加载完成",
  "page_url": "#observability",
  "repo_key": "gitcode:grape-dev/grape",
  "attachment_url": ""
}

// Response 200
{
  "id": 1,
  "status": "open",
  "created_at": "2026-09-16T11:00:00"
}
```

**实现方式**：grape 主服务收到请求后，从 JWT 解析 user_id/username，附带上下文信息（page_url/repo_key/user_agent），通过 HTTP 转发到 admin 后端 `POST /api/feedback`。

```python
# src/api/routes/feedback.py
@router.post("/feedback")
def submit_feedback(body: FeedbackBody, user: dict = Depends(get_current_user)):
    """提交问题反馈（转发到 admin 后端）。"""
    import requests
    admin_url = os.environ.get("GRAPE_ADMIN_URL", "http://localhost:8081")
    payload = {
        "user_id": user.get("id") or 0,
        "username": user.get("username") or "",
        "category": body.category,
        "title": body.title,
        "description": body.description,
        "page_url": body.page_url or "",
        "repo_key": body.repo_key or "",
        "user_agent": "",  # 从 request.headers 获取
    }
    try:
        resp = requests.post(f"{admin_url}/api/feedback", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        log.warning(f"feedback submit failed: {e}")
        return {"error": "反馈提交失败，请稍后重试"}
```

### 4.2 Admin 后端 API（grape-admin Java）

#### 4.2.1 提交反馈（工作台调用）

```
POST /api/feedback
```

无 JWT 认证（工作台侧已认证），接受 user_id/username 由工作台传入。

#### 4.2.2 反馈列表（admin 管理台调用）

```
GET /api/feedback/list?status=open&category=bug&page=1&size=20
```

```json
// Response
{
  "total": 42,
  "items": [
    {
      "id": 1,
      "user_id": 5,
      "username": "songspace",
      "category": "bug",
      "title": "引擎列表页加载缓慢",
      "description": "...",
      "page_url": "#observability",
      "repo_key": "gitcode:grape-dev/grape",
      "attachment_url": "",
      "status": "open",
      "priority": 0,
      "handler_name": "",
      "handler_remark": null,
      "handled_at": null,
      "created_at": "2026-09-16T11:00:00"
    }
  ]
}
```

#### 4.2.3 更新反馈状态

```
PUT /api/feedback/{id}/status
```

```json
// Request
{
  "status": "in_progress",
  "priority": 1,
  "handler_remark": "已确认性能问题，正在排查引擎健康分查询的 N+1 问题"
}
```

#### 4.2.4 反馈统计

```
GET /api/feedback/stats
```

```json
{
  "total": 42,
  "open": 15,
  "in_progress": 3,
  "resolved": 20,
  "closed": 4,
  "by_category": {
    "bug": 18,
    "suggestion": 12,
    "question": 8,
    "other": 4
  }
}
```

---

## 五、前端设计

### 5.1 Grape 工作台 — 反馈入口

#### 5.1.1 入口位置

在侧边栏底部（`.sb-ft` 区域），帮助按钮旁增加"问题反馈"按钮：

```html
<!-- index.html 侧边栏底部，help-btn 旁 -->
<a onclick="openFeedbackDialog()" class="help-btn" title="问题反馈">
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="2" stroke-linecap="round" stroke-linecap="round">
    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
  </svg>
</a>
```

#### 5.1.2 反馈弹窗

轻量弹窗（不用全页路由），从右侧滑出或居中模态：

```
┌──────────────────────────────────────┐
│  💡 问题反馈                       ✕  │
├──────────────────────────────────────┤
│  类型：  [bug ▾]                      │
│         ○ Bug 缺陷                    │
│         ○ 功能建议                    │
│         ○ 使用疑问                    │
│         ○ 其他                        │
│                                      │
│  标题：  [____________________]       │
│                                      │
│  详细描述：                           │
│  ┌──────────────────────────────┐    │
│  │                              │    │
│  │  请详细描述遇到的问题...      │    │
│  │                              │    │
│  └──────────────────────────────┘    │
│                                      │
│  截图（可选）：[选择文件]              │
│  当前页面：#observability（自动采集）  │
│                                      │
│  ┌──────────────────────────────┐    │
│  │           提交反馈            │    │
│  └──────────────────────────────┘    │
└──────────────────────────────────────┘
```

#### 5.1.3 实现要点

- 弹窗用 overlay + dialog 模式（与现有 `_grapeConfirm` 风格一致）
- 类型选择用 segmented control 或下拉
- 截图上传复用工作台已有上传接口 `/api/upload`
- `page_url` 自动取 `window.location.hash`
- `repo_key` 从 `localStorage` 或当前 app 状态获取
- 提交后 `showToast("反馈已提交，感谢您的反馈！")` 并关闭弹窗
- 新增 `src/api/static/js/pages/feedback.js`，在 `index.html` 中引入

### 5.2 Grape Admin 管理台 — 反馈管理页

#### 5.2.1 路由与菜单

```js
// router/index.js 新增路由
{ path: '/feedback/manage', name: 'feedback.manage',
  component: () => import('../views/FeedbackManage.vue'),
  meta: { title: '问题反馈' } }
```

```sql
-- Flyway V10 迁移中同步插入菜单
INSERT INTO admin_permission_menus (code, name, path, parent_id, sort)
SELECT 'feedback.manage', '问题反馈', '/feedback/manage',
       id, 1 FROM admin_permission_menus WHERE code = 'sys';
```

菜单归属：放在"系统管理"分组下，sort 值在"工作台用户管理"之前。

#### 5.2.2 页面布局

```
┌─────────────────────────────────────────────────────────────────┐
│  问题反馈                                          [刷新]        │
├─────────────────────────────────────────────────────────────────┤
│  统计卡：  待处理 15  |  处理中 3  |  已解决 20  |  已关闭 4     │
├─────────────────────────────────────────────────────────────────┤
│  筛选：[全部状态 ▾] [全部类型 ▾] [搜索标题/描述...] [查询]      │
├──────────┬──────┬──────┬────────┬────────┬──────┬───────────────┤
│ ID       │ 提交人│ 类型 │ 标题    │ 状态    │ 优先级│ 提交时间      │
├──────────┼──────┼──────┼────────┼────────┼──────┼───────────────┤
│ #42      │张三   │ bug  │ 列表缓慢│ 待处理  │ 普通  │ 09-16 11:00  │
│ #41      │李四   │ 建议 │ 增加快捷│ 处理中  │ 紧急  │ 09-15 14:30  │
│ ...                                                            │
├─────────────────────────────────────────────────────────────────┤
│              < 1 2 3 ... >                  共 42 条             │
└─────────────────────────────────────────────────────────────────┘
```

#### 5.2.3 详情与处理弹窗

点击列表行弹出详情弹窗：

```
┌──────────────────────────────────────────────┐
│  反馈 #42                                ✕   │
├──────────────────────────────────────────────┤
│  提交人：  songspace（ID: 5）                 │
│  类型：    🐛 Bug 缺陷                       │
│  提交时间：2026-09-16 11:00:00               │
│  当前页面：#observability                     │
│  仓库：    gitcode:grape-dev/grape            │
│                                              │
│  标题：引擎列表页加载缓慢                     │
│  ┌──────────────────────────────────────┐    │
│  │ 打开设备监控页面时，引擎列表需要 5...  │    │
│  └──────────────────────────────────────┘    │
│  附件：  [截图查看]                           │
│                                              │
│  ────── 处理操作 ──────                      │
│  状态：    [处理中 ▾]                        │
│  优先级：  [紧急 ▾]                          │
│  处理备注：                                  │
│  ┌──────────────────────────────────────┐    │
│  │ 已确认性能问题，正在排查...           │    │
│  └──────────────────────────────────────┘    │
│  历史备注：                                   │
│  · [09-16 11:30] admin: 已收到，初步排查中    │
│  · [09-16 12:00] admin: 确认为 N+1 查询问题   │
│                                              │
│              [保存处理]                       │
└──────────────────────────────────────────────┘
```

#### 5.2.4 实现要点

- `FeedbackManage.vue` 参考 `WhitelistManage.vue` / `BetaApplyManage.vue` 的列表+弹窗模式
- 状态用彩色标签：open=蓝, in_progress=黄, resolved=绿, closed=灰
- 类型用图标：bug=🐛, suggestion=💡, question=❓, other=📝
- 处理备注每次保存追加到历史（不覆盖，JSON 数组存储或单独表）
- 分页复用现有 `PaginationBar.vue` 组件

---

## 六、安全与权限

### 6.1 工作台用户侧

- 提交反馈需要登录（`get_current_user` JWT 认证）
- 每个用户每天最多提交 10 条反馈（防刷，可在 admin 后端配置）
- description 最长 5000 字符，title 最长 200 字符
- 附件上传复用工作台已有上传接口（有大小/类型限制）

### 6.2 Admin 管理台侧

- 查看和处理反馈需要 admin 登录（现有 JWT 认证）
- 菜单权限通过 `admin_permission_menus` 控制（与现有菜单体系一致）
- 处理反馈操作记录到 `admin_audit_log`（action=feedback.handle）

### 6.3 跨服务调用安全

工作台 → admin 后端的 API 调用需要安全措施：

| 措施 | 实现 |
|------|------|
| 内网调用 | admin 后端只监听 8081 端口，不对外暴露 /api/feedback 提交接口 |
| 请求来源校验 | 工作台调用时携带内部 token（env `GRAPE_ADMIN_INTERNAL_TOKEN`），admin 后端校验 |
| 速率限制 | admin 后端对 /api/feedback POST 做速率限制（每 IP 每分钟 10 次） |

---

## 七、代码修改清单

### 7.1 Grape 工作台（grape 主服务）

| 文件 | 改动 |
|------|------|
| `src/api/routes/feedback.py` | 新增：反馈提交路由，转发到 admin 后端 |
| `src/api/main.py` | 注册 feedback router |
| `src/api/static/js/pages/feedback.js` | 新增：反馈弹窗组件 |
| `src/api/static/index.html` | 引入 feedback.js + 侧边栏增加反馈按钮 |
| `src/api/static/locales/zh/app.json` | 新增反馈相关 i18n key |
| `src/api/static/locales/en/app.json` | 同上英文 |

### 7.2 Grape Admin 管理台

| 文件 | 改动 |
|------|------|
| `backend/src/main/resources/db/migration/V10__user_feedback.sql` | 新增：建表 + 菜单插入 |
| `backend/src/main/java/com/grape/admin/entity/AdminUserFeedback.java` | 新增：JPA 实体 |
| `backend/src/main/java/com/grape/admin/controller/FeedbackController.java` | 新增：CRUD API |
| `frontend/src/views/FeedbackManage.vue` | 新增：反馈管理页面 |
| `frontend/src/router/index.js` | 新增路由 |

---

## 八、实施计划

| 步骤 | 内容 | 预计工作量 |
|------|------|-----------|
| 1 | Flyway 迁移建表 + JPA 实体 | 0.5h |
| 2 | Admin 后端 CRUD API | 1h |
| 3 | Admin 前端 FeedbackManage.vue | 2h |
| 4 | 工作台 feedback.py 路由 + 转发 | 0.5h |
| 5 | 工作台 feedback.js 弹窗 + index.html 入口 | 1.5h |
| 6 | i18n + 联调测试 | 1h |
| 合计 | | 6.5h |

---

## 九、验证方案

### 9.1 功能验证

1. 工作台登录用户点击"问题反馈"按钮 → 弹窗正常显示
2. 填写表单并提交 → showToast 提示成功 → admin_db 中有新记录
3. Admin 管理台打开"问题反馈"页面 → 列表展示刚提交的反馈
4. 点击列表行 → 详情弹窗展示完整信息
5. 修改状态为"处理中" + 填写备注 → 保存成功
6. 再次打开详情 → 历史备注正确展示
7. 工作台侧不登录时点击反馈按钮 → 提示需要登录

### 9.2 边界验证

1. 未登录用户不能提交反馈（API 返回 401）
2. 空标题/空描述 → 前端校验拦截
3. 超长描述（>5000 字）→ 前端截断 + 后端校验
4. 速率限制：同一用户连续提交 11 条 → 第 11 条被拒
5. Admin 管理台无权限用户不能访问反馈页面（菜单不可见）
