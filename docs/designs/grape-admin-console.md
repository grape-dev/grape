# Grape 运营管理台设计方案（v2.2 — 已定稿）

> 日期：2026-08-11
> 状态：已评审定稿（D3/D4/D5/D6 均已决策；D5 待 DBA 执行 GRANT 时最终确认，prod 域名待最终确认）
> v2 变更：运营管理台（admin）前后端独立部署、独立域名；代码位于 grape 仓库 admin/ 目录；
> 与 grape 服务共享数据库但账号体系完全隔离。
> 参考：VOD-Developer-skills/vod-system（Vue3 前端 + RBAC 权限模型）；Grape runtime 子系统

## 1. 需求理解（v2）

1. 运营管理台 = 独立服务：前端独立部署、后端独立部署，均不与 grape 的前后端部署在一起
2. 访问域名隔离：admin 用独立域名
3. 代码位置：grape 仓库内新增 admin/ 文件夹，内含独立前端 + 独立后端
4. 数据库：与 grape 共享同一个 MySQL（dev/prod 同构；本地开发用 docker MySQL 或直连 dev 服务器库）
5. 账号体系：admin 的账号体系与 grape 账号体系完全隔离（独立用户表、独立登录、独立会话）
6. 菜单：运行时管理（设备，核心）+ AI 菜单（AI 助手）+ 系统管理（用户/角色权限），按业务主次排序

## 2. 目标架构

```
域名: admin.example.com (已定)         域名: grape.example.com (不变)
        │                                            │
┌───────▼────────┐                            ┌───────▼────────┐
│ admin 前端      │   axios /api (同域)         │ grape 前端      │
│ (Vue3 独立应用)  │◄──────────────────────────►│ (原生JS SPA)    │
└───────┬────────┘                            └───────┬────────┘
        │                                            │
┌───────▼────────┐                            ┌───────▼────────┐
│ admin 后端      │  ── 共享 DB ─────────────► │ grape 后端      │
│ (Spring Boot)   │◄───────────────────────────│ (FastAPI :3000) │
└───────┬────────┘  可写(受控): runtime_        └───────┬────────┘
        │            credentials/agent_runtimes/       │
        │            agent_tasks(清理)                  │
┌───────▼────────┐  其余表只读; 自管: admin_* 表  ┌───────▼────────┐
│ 共享数据库       │◄────────────────────────────│ 共享数据库       │
│ MySQL(dev/prod)  │                            │                │
└─────────────────┘                            └────────────────┘
```

关键边界：
- **admin 后端不调用 grape 的 HTTP API**，直接读写共享 DB（减少耦合、不依赖 grape 在线）
- **admin 后端 DB 权限：运行时管理域可写，其余只读**——
  只读：meta、事件、会话等绝大多数表；
  可写（受控操作）：runtime_credentials（签发/吊销）、agent_runtimes（清理离线引擎/设备记录）、
  agent_tasks（清理/归档终态旧任务）
- **hermes（AI 助手）账号纯只读**：AI 是自由查询，清理是人工触发+代码执行的受控操作，两者权限分离
- **admin 账号表独立**：admin_users / admin_roles / admin_permission_menus / admin_role_menu_rel / admin_user_role_rel，不碰 grape 的 users / user_accounts / 白名单 / session token

## 3. 仓库布局

```
grape/
├── src/                      # 现有 grape 服务（不动）
├── admin/                    # ★ 新增：运营管理台
│   ├── backend/              #   独立后端 Spring Boot（对齐 VOD，可单独部署）
│   │   ├── pom.xml           #   spring-boot-starter-web/data-jpa + mysql + jjwt + flyway
│   │   ├── src/main/java/com/grape/admin/
│   │   │   ├── AdminApplication.java
│   │   │   ├── controller/   #   Auth / Ai / Permission / Runtime 控制器（/api/admin/*）
│   │   │   ├── service/      #   设备/引擎/凭据/清理/AI 助手业务
│   │   │   ├── repository/   #   JPA：admin_* 表 + 共享表只读实体
│   │   │   ├── entity/       #   AdminUser/AdminRole/... + 共享表只读实体
│   │   │   ├── config/       #   JwtFilter + RbacInterceptor + SecurityConfig（对齐 VOD）
│   │   │   └── dto/
│   │   ├── src/main/resources/application.yml  # 共享库连接 + 端口 8081
│   │   └── ai_hermes_query.py #   AI 助手代理（移植 VOD：spawn hermes CLI，流式 JSON 事件）
│   ├── frontend/             #   独立前端（Vue3 + Vite，对齐 VOD）
│   │   ├── src/
│   │   │   ├── views/
│   │   │   │   ├── Login.vue / Register.vue
│   │   │   │   ├── AiAssistant.vue      (AI 菜单：AI 助手聊天页，流式+图表)
│   │   │   │   ├── UserManage.vue       (系统管理)
│   │   │   │   ├── RolePermissionConfig.vue  (系统管理)
│   │   │   │   ├── DeviceManage.vue     (运行时管理：设备列表/详情/凭据)
│   │   │   │   └── EngineOverview.vue   (运行时管理：引擎总览)
│   │   │   ├── router/  api/  components/
│   │   │   └── utils/auth.js            (JWT 存取)
│   │   └── vite.config.js               (dev proxy → admin 后端)
│   └── deploy/               #   独立部署
│       ├── nginx-admin.conf  #   admin 域名 → 前端静态 + /api 反代 admin 后端
│       ├── admin.service     #   systemd：admin 后端 java -jar（Spring Boot）
│       └── Dockerfile        #   （可选）容器化
└── docs/                     # 设计方案归档（可选）
```

## 4. 账号体系隔离设计（重点）

| 维度 | grape 现有 | admin（新建） |
|---|---|---|
| 用户表 | users / user_accounts | admin_users（独立） |
| 登录方式 | GitCode OAuth + 密码 | 用户名+密码（VOD 式），不接 GitCode OAuth |
| 会话 | session token（DB 存） | JWT access + refresh（VOD 式） |
| 权限 | 无 RBAC（仅白名单） | admin_roles + 菜单权限（对齐 VOD VodPermission*） |
| 交互 | 无 | 两套体系互不感知 |

- admin 登录只认 admin_users；grape 的 token 在 admin 无效，反之亦然
- admin 后端启动时初始化 admin_* 表 + 种子数据（SUPER_ADMIN 初始账号 + 角色 + 菜单），不依赖 grape 的 db_setup
- 密码哈希：独立实现（bcrypt/argon2 或与 VOD 一致的方案），不复用 grape 逻辑

## 5. 菜单结构（admin 内）

```
admin 运营管理台
├── 1. 运行时管理（设备）   ★ 核心业务
│   ├── 设备列表        device 维度：状态/心跳/IP/凭据/引擎数/任务数；about_to_gc 一键回收
│   ├── 设备详情        单设备：引擎表 + 任务表 + 恢复孤儿 + 吊销凭据 + 清理引擎/任务
│   ├── 引擎总览        跨设备引擎视图：全部引擎分布/版本/健康度（opencode/codearts/...）
│   └── 凭据管理        签发 rta_ token（一次性明文展示）、吊销
├── 2. AI                辅助能力
│   └── AI 助手          对话式运营助手（移植 VOD：admin 后端 → hermes CLI 流式，会话续接）
└── 3. 系统管理          配置类（惯例放最后）
    ├── 用户管理        admin_users CRUD + 绑角色
    └── 角色权限配置    角色 CRUD + 菜单权限树（保护角色 SUPER_ADMIN/VIEWER）
```

设计说明：引擎（agent_runtimes）由设备注册产生，带 credential_id，本质是设备资产 →
归属运行时管理（设备详情内嵌引擎表 + 引擎总览跨设备视图）；AI 菜单只保留 AI 助手
（对话式运营能力，VOD 的 hermes CLI 模式，不需要模型管理/Provider 配置）。

## 6. 运行时管理（设备）数据链路

- 设备 = runtime_credentials 行（device_name/last_seen_at/last_ip/status）
- 引擎 = agent_runtimes 行（credential_id 关联设备，name/version/healthy/status）
- 任务 = agent_tasks 行（runtime_id 关联，状态/时间/技能）
- 全部从共享 DB 直读（admin 后端自己的只读查询，不复用 grape API）
- 凭据签发：生成 `rta_` token → 明文一次性返回 → 落库存 sha256(token)（与 grape runtime.py `_auth_credential` 的校验约定一致）→ worker 用明文注册/心跳，天然互通
- 吊销：status → revoked，worker 下一次请求即 401，前端提示重新签发
- 清理（运行时管理域写操作，对齐 grape health.py GC 语义：离线 >6d = about_to_gc，7d 阈值）：
  - 设备回收：吊销凭据 → 删除/归档该设备 agent_runtimes → 清理孤儿任务
  - 引擎清理：删除离线引擎记录（有 dispatched/running 任务的拒绝）
  - 任务清理：按「终态 + 时间范围」归档/删除 agent_tasks
  - 护栏：跳过进行中任务（对齐 cleanup.py「进行中绝不清理」）；不可逆操作二次确认；
    操作写 admin 审计日志（谁/何时/清了什么）

## 7. admin 后端设计（Spring Boot 2.7.x，对齐 VOD）

- 技术栈：Spring Boot 2.7.18 + Spring Web + Spring Data JPA + MySQL Connector + jjwt + BCrypt + Flyway（与 VOD pom 对齐）
- 分层：controller → service → repository(JPA) → entity（对齐 VOD vod-system/backend 结构）
- 认证：JWT（JwtAuthenticationFilter + JwtUtil + TokenBlacklist，对齐 VOD SecurityConfig）
- 权限：RbacHandlerInterceptor + RbacPermissionLoader（对齐 VOD），菜单权限校验 403
- 共享库连接：application.yml 配置同一 MySQL（VOD 式 jdbc:mysql://...vod_db）；JPA 实体映射
  共享表（agent_runtimes / agent_tasks / runtime_credentials / meta），只读实体 + 受控写服务
- 端口：8081（与 grape 3000 隔离）
- D5 账号：grape_admin 独立最小权限账号（SELECT 按需 + 三张运行时表写 + admin_* 全权限）
- 访问契约：runtime_credentials.token_hash = sha256(raw) hex；status ∈ {active, revoked}；
  agent_runtimes / agent_tasks 可写仅限受控清理（§6）；meta / 事件 / 会话只读
- AI 助手：POST /api/admin/ai/chat → Java 端 ProcessBuilder spawn ai_hermes_query.py
  （对齐 VOD AiQueryService：hermes.bin / hermes.query-script 配置）→ SSE 流式转发；
  hermes 配共享库只读账号
- dev 环境（D4 连带影响）：Spring Boot/JPA 对 SQLite 支持差 → dev 直接用同一 MySQL
  （本地 docker 起一个，或直连 dev 服务器 MySQL），弃用 SQLite 双进程方案

## 8. admin 前端设计（Vue3 + Vite）

- 直接借鉴 VOD：Login/Register/UserManage/RolePermissionConfig/AIAssistant 页面结构与权限树组件
- 新增 DeviceManage.vue（设备管理）：设备卡片/表格 + 健康徽章（样式对齐 grape runtimes.js）+ 详情抽屉 + 凭据签发对话框
- vite dev proxy：/api → admin 后端；生产构建产物由 nginx 静态托管
- axios 封装：JWT 注入、401 跳登录、x-new-token 续期（对齐 VOD api/index.js）

## 9. 部署拓扑

```
nginx (admin 域名)
  ├── /        → admin 前端构建产物 (dist/)
  └── /api     → 反代 admin 后端 (127.0.0.1:8081)

systemd: admin-backend.service (java -jar admin-backend-*.jar --server.port=8081)
         admin-frontend.service (可选，或纯静态由 nginx 托管)
```

- 与 grape 完全解耦：grape 停服不影响 admin 查看设备历史数据（只读 DB）；admin 停服不影响 grape
- 唯一耦合点：共享 DB（凭据表写 + 设备表读）

## 10. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | admin 骨架：仓库目录 + Spring Boot 骨架（pom/启动类/JPA 连共享库）+ admin_* 表(Flyway) + JWT 登录 | 独立登录可用，与 grape 账号互不影响 |
| P1 | 系统管理：用户管理 + 角色权限配置（对齐 VOD） | 角色/菜单/用户绑定闭环 |
| P2 | 运行时管理：设备列表/详情/凭据签发吊销/任务（直读共享 DB） | 设备在线状态正确、签发 token 可被 worker 使用 |
| P3 | AI 菜单：AI 助手（hermes CLI 代理 + 聊天页 + 会话续接） | 流式问答可用，会话可续接 |
| P4 | 部署：nginx + systemd + 独立域名 + 文档 | 独立域名全链路可用 |

## 11. 风险与开放问题

1. ~~D4~~ 已定：Spring Boot 2.7.x（完全对齐 VOD）；连带影响：dev 也用 MySQL（弃 SQLite 双进程方案）
2. **D5 已定（暂定）**：prod MySQL 给 admin 建独立最小权限账号 —— SELECT（按需读表，不含 user_credentials）+ INSERT/UPDATE/DELETE 仅限 runtime_credentials / agent_runtimes / agent_tasks；dev 也走同一 MySQL（D4 连带：Spring Boot/JPA 弃 SQLite）；待 DBA 执行 GRANT 时最终确认
3. ~~D3~~ 已定：AI 菜单 = AI 助手（hermes CLI 模式，VOD 移植）；模型管理否决（无直连 LLM 消费方）
4. ~~D6~~ 已定：dev = admin.example.com；prod 假设 admin.grape.example.com（待最终确认）
5. 凭据签发的安全：明文 token 只在签发响应中出现一次，前端提示立即保存；签发/吊销/清理操作记 admin 操作日志（审计）
6. 清理不可逆：设备回收/引擎删除/任务删除需二次确认（输入确认词），且跳过进行中任务；审计日志可追溯
7. 账号隔离的边界：admin 不接 GitCode OAuth；如需 SSO 列二期
8. 种子数据：SUPER_ADMIN 初始密码需在部署时通过环境变量注入，不入库明文、不入 git
9. AI 助手依赖：admin 主机需安装 Hermes CLI 并配置模型（~/.hermes/config.yaml）；hermes 配共享库只读凭据（对齐 VOD B 机只读用户）；流式连接超时与并发上限在代理层控制
