# Grape 运营管理台设计方案（v2.4 — 已实现同步）

> 日期：2026-08-11（v2.3 同步实现：2026-08-12；v2.4 已实现同步：2026-08-12）
> 状态：已评审定稿并实现（P0-P4 全部完成；本版本同步实际实现差异：菜单结构/独立详情页/Flyway 现状/凭据生命周期/范围白名单）
> v2 变更：运营管理台（admin）前后端独立部署、独立域名；代码位于 grape 仓库 admin/ 目录；
> 与 grape 服务共享数据库但账号体系完全隔离。
> v2.3 变更：前端布局与样式对齐 VOD（顶部导航上下结构 + VOD 样式体系）；认证 401 自动续期。
> v2.4 变更：同步实际实现——菜单改为动态（含用户新增页面）；设备详情改为独立页；
> Flyway 禁用（V 迁移手动应用）；凭据生命周期完整化（吊销/回收/删除）+ 契约参数化；
> 交互体系统一（全局 modal/toast）。
> 参考：VOD-Developer-skills/vod-system（Vue3 前端 + RBAC 权限模型）；Grape runtime 子系统

## 1. 需求理解（v2）

1. 运营管理台 = 独立服务：前端独立部署、后端独立部署，均不与 grape 的前后端部署在一起
2. 访问域名隔离：admin 用独立域名
3. 代码位置：grape 仓库内新增 admin/ 文件夹，内含独立前端 + 独立后端
4. 数据库：与 grape 共享同一个 MySQL（dev/prod 同构；本地开发用 docker MySQL 或直连 dev 服务器库）
5. 账号体系：admin 的账号体系与 grape 账号体系完全隔离（独立用户表、独立登录、独立会话）
6. 菜单：顶级直达（总览/智能体管理/Skill管理/代码仓管理/AI分析）+ 下拉
   运行时管理（设备，核心）+ 系统管理（用户/角色权限），按业务主次排序；顶部导航下拉式（对齐 VOD），动态 /menus

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
│   ├── frontend/             #   独立前端（Vue3 + Vite，布局与样式对齐 VOD）
│   │   ├── src/
│   │   │   ├── views/
│   │   │   │   ├── Login.vue
│   │   │   │   ├── Overview.vue         (总览：KPI 卡 + 智能体指标)
│   │   │   │   ├── AiAssistant.vue      (AI 分析：AI 助手聊天页，流式+图表)
│   │   │   │   ├── ChatHistory.vue      (AI 分析：AI 历史)
│   │   │   │   ├── UserManage.vue       (系统管理)
│   │   │   │   ├── RolePermissionConfig.vue  (系统管理)
│   │   │   │   ├── DeviceManage.vue     (运行时管理：设备管理/吊销/回收/删除)
│   │   │   │   ├── DeviceDetail.vue     (运行时管理：设备独立详情页 /runtime/devices/:id)
│   │   │   │   ├── EngineManage.vue     (运行时管理：引擎管理/统计/删除)
│   │   │   │   ├── CredentialManage.vue (运行时管理：凭据签发/吊销/删除)
│   │   │   │   ├── AgentManage.vue      (智能体管理)
│   │   │   │   ├── AgentDetail.vue      (智能体详情)
│   │   │   │   ├── SkillManage.vue      (Skill 管理)
│   │   │   │   ├── ReposManage.vue      (代码仓管理)
│   │   │   │   └── TemplatesManage.vue  (模板画廊)
│   │   │   ├── router/  api/  utils/
│   │   │   └── utils/auth.js            (JWT/refresh token 存取)
│   │   └── vite.config.js               (dev proxy → admin 后端)
│   ├── docs/designs/        #   设计文档（随子系统内聚，git mv 迁移自 docs/designs）
│   │   └── grape-admin-console.md / grape-admin-implementation.md
│   └── deploy/               #   独立部署
│       ├── nginx-grape-admin.conf  # 80 端口 → 前端静态 + /api 反代 admin 后端
│       ├── admin-backend.service   # systemd：admin 后端 java -jar（Spring Boot）
│       └── deploy.sh               # 一键部署脚本
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
- admin_* 表统一五件套：created_at / created_by / updated_at / updated_by / deleted（软删除，0/1）；
  created_by、updated_by = 操作者 admin 用户名；audit_log 保持 append-only，仅 created_at

## 5. 菜单结构（admin 内）

布局：顶部导航（上下结构，对齐 VOD：一级菜单 + 下拉二级 + 移动端汉堡抽屉）；
菜单数据动态来自 GET /api/admin/menus（按登录用户权限返回）——实际已含用户新增菜单。

```text
┌────────────────────────────────────────────────────────────────┐
│ 🍇 Grape运营管理系统 │ 📊总览 │ 🧩Skill管理 │ 📚代码仓管理 │      │
│ 🖥️ 运行时管理▾ │ ⚙️ 系统管理▾ │ 🤖AI分析 │ 👤 用户名(角色) 退出 │
└────────────────────────────────────────────────────────────────┘
admin 运营管理台
├── 顶级直达
│   ├── 总览            KPI 卡（设备/引擎/凭据/任务）+ 智能体指标
│   ├── 智能体管理      智能体卡片 + 详情页 + 负载预警
│   ├── Skill 管理      Skill 上传/管理/转官方技能
│   ├── 代码仓管理      仓库管理 + 模板代码仓
│   └── AI 分析         AI 助手 + AI 历史
├── 下拉：运行时管理
│   ├── 设备管理        设备维度：状态/心跳/IP/凭据/引擎数/任务数；吊销/回收/删除；
│   │                  独立详情页（引擎+任务筛选排序+统计卡+饼图）
│   ├── 引擎管理        跨设备引擎视图：类型统计卡 + 列表/卡片 + 范围过滤 + 删除
│   └── 凭据管理        签发 rta_ token（一次性明文展示）、吊销、删除
└── 下拉：系统管理
    ├── 用户管理        admin_users CRUD + 绑角色
    └── 角色权限配置    角色 CRUD + 菜单权限树（保护角色 SUPER_ADMIN/VIEWER）
```

设计说明：引擎（agent_runtimes）由设备注册产生，带 credential_id，本质是设备资产 →
归属运行时管理（设备独立详情页内嵌引擎/任务 + 引擎管理跨设备视图）；
设备详情为独立页（/runtime/devices/:id，可分享 URL，内容量大）；
凭据=设备 1:1（同一 runtime_credentials 表），凭据页聚焦身份管理（无健康/运行态，
健康属设备页概念）；AI 菜单含助手 + 历史两项；前端布局/样式对齐 VOD。

## 6. 运行时管理（设备）数据链路

- 设备 = runtime_credentials 行（device_name/last_seen_at/last_ip/status）
- 引擎 = agent_runtimes 行（credential_id 关联设备，name/version/healthy/status）
- 任务 = agent_tasks 行（runtime_id 关联，状态/时间/技能）
- 全部从共享 DB 直读（admin 后端自己的只读查询，不复用 grape API）
- 凭据签发：生成 `rta_` token → 明文一次性返回 → 落库存 sha256(token)（与 grape runtime.py `_auth_credential` 的校验约定一致）→ worker 用明文注册/心跳，天然互通
  - 契约：POST /credentials body { name, scope? }；scope 白名单参数化（SUPPORTED_SCOPES，
    当前仅 global=运营管理台只签全局凭据；扩展点：开放新范围 = 白名单加项 + 校验逻辑）；
    owner_name 回填签发人（审计追溯）；响应 { token, name }
- 吊销：POST /credentials/{cid}/revoke（凭据路径；设备页同操作用 /devices/{cid}/revoke），
  status → revoked，worker 下一次请求即 401，前端提示重新签发
- 凭据/设备生命周期（三步曲，均 modal 确认 + 审计）：
  - 吊销：凭据失效（数据全保留，可手动恢复 DB status）
  - 回收（设备页）：吊销 → 删除/归档该设备 agent_runtimes → 清理孤儿任务
  - 删除：物理删除凭据 + 引擎 + 终态任务（仅 revoked 可删，释放凭据名，worker 需重新签发注册）
- 清理（运行时管理域写操作，对齐 grape health.py GC 语义：离线 >6d = about_to_gc，7d 阈值）：
  - 设备回收：吊销凭据 → 删除/归档该设备 agent_runtimes → 清理孤儿任务
  - 引擎清理：仅 status='offline' 引擎可删（防误删在线）；删前清 agent_type
    default_runtime_id 悬空引用（置 0）；有 dispatched/running 任务的拒绝
  - 任务清理：按「终态 + 时间范围」归档/删除 agent_tasks
  - 护栏：跳过进行中任务（对齐 cleanup.py「进行中绝不清理」）；不可逆操作二次确认
    （输入名称 modal，删除/回收/吊销统一交互）；操作写 admin 审计日志（谁/何时/清了什么）

## 7. admin 后端设计（Spring Boot 2.7.x，对齐 VOD）

- 技术栈：Spring Boot 2.7.18 + Spring Web + Spring Data JPA + MySQL Connector + jjwt + BCrypt（与 VOD pom 对齐）
- Flyway 现状：**已禁用**（SPRING_FLYWAY_ENABLED=false）——迁移文件 V1-V14 完整入库，
  数据库 flyway_schema_history 与应用一致；后续新增 V 迁移需手动执行 SQL 或临时开启 Flyway
  （背景：Spring Boot 2.7.18 + Flyway 9.22.3 下 missing 校验拦不住，禁用为防重启循环）
- 分层：controller → service → repository(JPA) → entity（对齐 VOD vod-system/backend 结构）
- 认证：JWT（JwtAuthInterceptor + JwtUtil + TokenBlacklist）；access 2h / refresh 7d；
  前端 401 自动调 /auth/refresh 续期并重放原请求，refresh 失败才清登录态跳登录
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

## 8. admin 前端设计（Vue3 + Vite，对齐 VOD）

- 布局：顶部导航上下结构（对齐 VOD App.vue：topnav 一级菜单 + 下拉二级 + 移动端汉堡抽屉）；
  菜单数据动态来自 GET /api/admin/menus（按登录用户权限返回，前端映射为导航结构）；
  品牌「Grape运营管理系统」+ 顶部展示真实用户名与角色（登录响应 roles 链路，
  localStorage 非响应式 → ref + watch(route.path)/onMounted 刷新）
- 样式体系：整体对齐 VOD main.css（--primary #006eff 设计令牌、浅色卡片风、.card/.vod-table/
  .btn/.status-tag/.pagination-bar 通用组件类）；设备卡片/凭据 token/聊天流等 admin 特有组件
  按 VOD 风格补充；**全局弹窗/Toast 体系**（modal-overlay/modal-dialog/modal-header/body/footer/
  toast-message 提升至 style.css 全局共用，AI 分析/设备/引擎/凭据各页统一）；
  设备详情统计卡/信息卡并入 Grape 自家 kpi 色带卡体系（Overview/AgentManage 同款）
- 页面：Login / Overview / DeviceManage / DeviceDetail（独立页）/ EngineManage / CredentialManage /
  AiAssistant / ChatHistory / UserManage / RolePermissionConfig / AgentManage / AgentDetail /
  SkillManage / ReposManage / TemplatesManage（对齐 VOD 页面结构）
- vite dev proxy：/api → admin 后端；生产构建产物由 nginx 静态托管
- axios 封装：JWT 注入；401 先调 /auth/refresh 续期并重放原请求（并发只 refresh 一次），
  refresh 也失败才清 token 跳登录；登录后进入主页面时兜底补拉菜单（App 组件复用，
  onMounted 不重触发）

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

| 阶段 | 内容 | 验收 | 状态 |
|---|---|---|---|
| P0 | admin 骨架：仓库目录 + Spring Boot 骨架（pom/启动类/JPA 连共享库）+ admin_* 表(Flyway) + JWT 登录 | 独立登录可用，与 grape 账号互不影响 | ✅ 完成 |
| P1 | 系统管理：用户管理 + 角色权限配置（对齐 VOD） | 角色/菜单/用户绑定闭环 | ✅ 完成 |
| P2 | 运行时管理：设备管理/详情/凭据签发吊销/任务（直读共享 DB） | 设备在线状态正确、签发 token 可被 worker 使用 | ✅ 完成（扩展：详情独立页/删除/回收） |
| P3 | AI 菜单：AI 助手（hermes CLI 代理 + 聊天页 + 会话续接） | 流式问答可用，会话可续接 | ✅ 完成 |
| P4 | 部署：nginx + systemd + 独立域名 + 文档 | 独立域名全链路可用 | ✅ 完成（dev 已部署；prod 待最终域名确认） |

## 11. 风险与开放问题

1. ~~D4~~ 已定并实现：Spring Boot 2.7.x（完全对齐 VOD）；dev 用同一 MySQL（弃 SQLite 双进程方案）
2. ~~D5~~ 已确认并部署：共享库独立最小权限账号（SELECT 按需 + 三张运行时表写 + admin_* 全权限）
3. ~~D3~~ 已定：AI 菜单 = AI 助手（hermes CLI 模式，VOD 移植）；模型管理否决（无直连 LLM 消费方）
4. ~~D6~~ 已定：dev = admin.example.com；prod 假设 admin.grape.example.com（待最终确认）
5. 凭据签发的安全：明文 token 只在签发响应中出现一次，前端提示立即保存；签发/吊销/清理操作记 admin 操作日志（审计）
6. 清理不可逆：设备回收/引擎删除/任务删除需二次确认（输入确认词 modal），且跳过进行中任务；审计日志可追溯
7. 账号隔离的边界：admin 不接 GitCode OAuth；如需 SSO 列二期
8. 种子数据：SUPER_ADMIN 初始密码需在部署时通过环境变量注入，不入库明文、不入 git
9. AI 助手依赖：admin 主机需安装 Hermes CLI 并配置模型（~/.hermes/config.yaml）；hermes 配共享库只读凭据（对齐 VOD B 机只读用户）；流式连接超时与并发上限在代理层控制
10. Flyway 禁用为长期现状：后续新增 V 迁移需手动 SQL 或临时开启（见 §7）

## 12. 已实现功能清单（v2.4 追溯）

| 功能 | 说明 |
|---|---|
| 设备管理 | 列表/卡片双视图、健康徽章行（在线/刚失联/离线/待回收/已吊销）、分页 10/20/50、吊销/回收/删除（modal+输入确认+toast） |
| 设备独立详情页 | /runtime/devices/:id：信息区字段卡化（kpi 色带）+ 状态标签；引擎列表；任务表（列头筛选/排序/已筛选 N 条/清除）；任务状态统计卡 + echarts 饼图；VOD 式智能返回按钮 |
| 引擎管理 | 类型统计卡 + 列表/卡片 + 范围过滤 + 删除（仅 offline 可删 + agent_type 悬空引用清理） |
| 凭据管理 | 签发（范围白名单仅全局 + owner 回填）、吊销（凭据路径别名）、删除（物理清理）；凭据页聚焦身份（无健康/运行态） |
| 凭据契约 | POST /credentials {name, scope}、POST/DELETE /credentials/{cid}[/revoke] 凭据路径语义化；GET /credentials 响应 name/ownerName/scope |
| 品牌与账号 | 「Grape运营管理系统」+ 登录响应 roles 链路（用户名/角色展示）；401 自动续期 |
| 全局交互体系 | modal-overlay/modal-dialog/toast 提升全局 style.css；AI 分析/设备/引擎/凭据统一 |
| AI 分析 | AI 助手（hermes CLI 流式 + 图表）+ AI 历史（会话续接/删除） |
| 用户功能（非本次文档范围） | 总览 KPI 卡/智能体管理/详情/负载预警、Skill 管理/上传、代码仓管理、模板画廊 |
