# Grape 运营管理台（admin）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development（推荐）或 executing-plans 按任务实施。步骤用 `- [ ]` 复选框跟踪。
> 设计方案：`docs/designs/grape-admin-console.md`（v2.2 定稿，本计划的唯一需求源）
> VOD 参照代码：`/root/VOD-Developer-skills/vod-system/`（backend=Spring Boot 2.7.18，frontend=Vue3+Vite）

**Goal:** 在 grape 仓库 admin/ 目录实现运营管理台：独立 Spring Boot 后端 + 独立 Vue3 前端，共享 MySQL，账号体系隔离，含运行时管理（设备/引擎/凭据/任务/清理）、AI 助手、系统管理，独立域名部署。

**Architecture:** admin 后端（Spring Boot 2.7.18，端口 8081，/api/admin/*，JWT+RBAC）直连共享 MySQL（grape_admin 最小权限账号）；admin 前端（Vue3+Vite）同域 axios；AI 助手走 admin 后端 ProcessBuilder spawn hermes CLI（移植 VOD ai_hermes_query.py）；与 grape 服务完全解耦，唯一耦合点=共享库。

**Tech Stack:** Spring Boot 2.7.18 / Spring Web / Spring Data JPA / mysql-connector-java 8.0.33 / jjwt / spring-security-crypto (BCrypt) / Flyway / Vue 3 + Vite / axios / nginx / systemd

## Global Constraints（每个任务都隐含满足）

1. 代码全部位于 `grape/admin/` 下，`src/`（grape 服务）一行不动
2. 后端包名 `com.grape.admin`，端口 8081；API 前缀统一 `/api/admin/*`
3. 账号体系独立：只读写 admin_* 表；**绝不**访问 grape 的 users / user_accounts / 白名单 / session 表
4. 共享表访问契约：
   - `runtime_credentials.token_hash` = sha256(raw_token) 小写 hex；status ∈ {active, revoked}
   - `agent_runtimes` / `agent_tasks` 仅允许受控清理写（见 P2.5）；`meta` / 事件 / 会话表只读
5. 凭据明文只在签发响应中出现一次；吊销/清理操作写 `admin_audit_log`
6. 清理护栏：有 dispatched/running 任务的记录绝不清理；不可逆操作前端二次确认（输入确认词）
7. 种子数据：SUPER_ADMIN 初始密码从环境变量 `ADMIN_SEED_PASSWORD` 读取，不入库明文、不入 git
8. 健康状态语义对齐 grape `src/runtime/health.py`（实现前先读该文件核对规则）
9. 前端中文 UI；菜单/文案与设计文档一致
10. 所有 DB 写操作必须经过 Service 层校验，禁止在 Controller 直接写共享表
11. 软删除约定：admin_* 表统一五件套（created_at/created_by/updated_at/updated_by/deleted）；
    删除=UPDATE deleted=1；查询默认 WHERE deleted=0；唯一键带 deleted 维度；
    共享表无 deleted 列，清理保持物理删除

---

## Phase P0：骨架 + 独立账号 + JWT 登录

### Task P0.1 创建 admin/backend Spring Boot 骨架

**Objective:** 可启动的空 Spring Boot 应用（端口 8081）

**Files:**
- Create: `admin/backend/pom.xml`
- Create: `admin/backend/src/main/java/com/grape/admin/AdminApplication.java`
- Create: `admin/backend/src/main/resources/application.yml`
- Create: `admin/backend/.gitignore`（target/ 等）

**Step 1:** pom.xml（对齐 VOD vod-system/backend/pom.xml，改 artifactId=admin-backend，加 jjwt + flyway）

```xml
<parent>spring-boot-starter-parent 2.7.18</parent>
<dependencies>
  spring-boot-starter-web
  spring-boot-starter-data-jpa
  spring-boot-starter-validation
  mysql-connector-java 8.0.33
  flyway-core / flyway-mysql
  jjwt-api / jjwt-impl / jjwt-jackson (0.11.5)
  spring-security-crypto (BCrypt)
  spring-boot-starter-test (test)
</dependencies>
```

**Step 2:** application.yml

```yaml
server:
  port: 8081
spring:
  datasource:
    url: ${ADMIN_DB_URL:jdbc:mysql://127.0.0.1:3306/vod_db?useSSL=false&serverTimezone=Asia/Shanghai&allowPublicKeyRetrieval=true&rewriteBatchedStatements=true}
    username: ${ADMIN_DB_USER:grape_admin}
    password: ${ADMIN_DB_PASSWORD:}
  jpa:
    hibernate.ddl-auto: none
    open-in-view: false
  flyway:
    locations: classpath:db/migration
app:
  jwt-secret: ${ADMIN_JWT_SECRET:}
  jwt-expire-minutes: 120
  hermes-bin: ${HERMES_BIN:}
  hermes-query-script: ${HERMES_QUERY_SCRIPT:ai_hermes_query.py}
```

**Step 3:** 启动验证

```bash
cd /opt/grape/admin/backend
mvn -q spring-boot:run   # 或 mvn -q package && java -jar target/admin-backend-*.jar
curl -s http://127.0.0.1:8081/api/admin/health
# 期望: {"status":"ok"}（先返回固定值，连库在 P0.2 后）
```

**Verification:** `curl -s http://127.0.0.1:8081/api/admin/health` 返回 200；端口 8081 与 grape 3000 不冲突。

**Commit:** `chore(admin): Spring Boot 骨架（P0.1）`

### Task P0.2 Flyway 迁移：admin_* 表 + 种子数据

**Objective:** admin 自建 6 张表 + 种子（SUPER_ADMIN/VIEWER/OPS 角色、菜单、初始管理员）

**Files:**
- Create: `admin/backend/src/main/resources/db/migration/V1__admin_schema.sql`

**Step 1:** V1__admin_schema.sql（admin_* 表统一五件套：created_at / created_by / updated_at / updated_by / deleted 软删除；updated_at 用 MySQL `ON UPDATE CURRENT_TIMESTAMP` 自动维护）

```sql
-- 约定：admin_* 表统一含五件套 created_at/created_by/updated_at/updated_by/deleted（软删除）
-- created_by/updated_by = 操作者 admin 用户名
CREATE TABLE admin_users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL,
  password_hash VARCHAR(100) NOT NULL,
  nickname VARCHAR(64) DEFAULT '',
  status TINYINT DEFAULT 1,              -- 1 启用 0 禁用
  deleted TINYINT DEFAULT 0,             -- 软删除：0 未删除 1 已删除
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',     -- 创建人（admin 用户名）
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',     -- 最后更新人（admin 用户名）
  UNIQUE KEY uk_users_username (username, deleted)   -- 软删除下用户名可复用（唯一性带 deleted 维度）
);
CREATE TABLE admin_roles (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  code VARCHAR(64) NOT NULL,             -- SUPER_ADMIN / VIEWER / OPS / 自定义
  name VARCHAR(64) NOT NULL,
  is_protected TINYINT DEFAULT 0,
  deleted TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',
  UNIQUE KEY uk_roles_code (code, deleted)
);
CREATE TABLE admin_permission_menus (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  code VARCHAR(64) NOT NULL,             -- runtime.devices / runtime.engines / runtime.credentials / ai.assistant / sys.users / sys.roles
  name VARCHAR(64) NOT NULL,
  path VARCHAR(128) NOT NULL,            -- /runtime/devices 等前端路由
  parent_id BIGINT DEFAULT 0,
  sort INT DEFAULT 0,
  deleted TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',
  UNIQUE KEY uk_menus_code (code, deleted)
);
CREATE TABLE admin_role_menu_rel (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,  -- 软删除下改用自增主键（原复合主键无法容纳 deleted 多版本）
  role_id BIGINT NOT NULL,
  menu_id BIGINT NOT NULL,
  deleted TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',
  UNIQUE KEY uk_rm (role_id, menu_id, deleted)
);
CREATE TABLE admin_user_role_rel (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  role_id BIGINT NOT NULL,
  deleted TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',
  UNIQUE KEY uk_ur (user_id, role_id, deleted)
);
-- audit_log 保持 append-only：只有 created_at（事件日志无"更新/删除"语义）
CREATE TABLE admin_audit_log (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT, username VARCHAR(64),
  action VARCHAR(64) NOT NULL,           -- credential.issue / credential.revoke / device.cleanup / engine.delete / task.cleanup
  target VARCHAR(255), detail VARCHAR(1024),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
-- 软删除规则（全局约束）：
-- 1) 所有查询默认 WHERE deleted=0（菜单树/角色/用户列表/绑定关系同理）
-- 2) 删除操作 = UPDATE deleted=1，不做物理 DELETE；角色权限保存 = 旧绑定标 deleted=1 + 新集合插入 deleted=0
-- 3) 唯一约束带 deleted 维度（UNIQUE KEY (..., deleted)），软删除后同名/同 code 可重建
-- 4) 软删除仅适用于 admin_* 表；共享表（agent_runtimes/agent_tasks/runtime_credentials）无 deleted 列，
--    admin 对共享表的清理保持物理删除（见 P2.4）
-- created_by/updated_by 维护：所有写操作在 Service 层从 JWT 上下文取当前 username 填充；
-- JPA 实体对应映射五字段
-- 种子：角色
INSERT INTO admin_roles (code, name, is_protected) VALUES
 ('SUPER_ADMIN','超级管理员',1),('VIEWER','只读',1),('OPS','运营',0);
-- 种子：菜单（结构与设计文档 §5 一致）
INSERT INTO admin_permission_menus (code,name,path,parent_id,sort) VALUES
 ('runtime','运行时管理','',0,1),
 ('runtime.devices','设备管理','/runtime/devices',(SELECT id FROM admin_permission_menus WHERE code='runtime'),1),
 ('runtime.engines','引擎总览','/runtime/engines',(SELECT id FROM admin_permission_menus WHERE code='runtime'),2),
 ('runtime.credentials','凭据管理','/runtime/credentials',(SELECT id FROM admin_permission_menus WHERE code='runtime'),3),
 ('ai','AI','',0,2),
 ('ai.assistant','AI 助手','/ai/assistant',(SELECT id FROM admin_permission_menus WHERE code='ai'),1),
 ('sys','系统管理','',0,3),
 ('sys.users','用户管理','/sys/users',(SELECT id FROM admin_permission_menus WHERE code='sys'),1),
 ('sys.roles','角色权限配置','/sys/roles',(SELECT id FROM admin_permission_menus WHERE code='sys'),2);
-- SUPER_ADMIN 挂全部菜单；VIEWER 挂只读菜单（devices/engines/assistant）
-- 初始管理员（密码哈希由 P0.5 启动器生成并 UPDATE，不在 SQL 里写死）
```

**Step 2:** 初始管理员创建：`ApplicationRunner` 读 `ADMIN_SEED_PASSWORD`（缺省跳过）→ 若无 admin 用户则创建 `admin` 用户（BCrypt），若已有则不动；密码为空则启动失败并提示。

**Step 3:** 验证

```bash
mvn -q spring-boot:run
mysql -u root -p grape_test_db -e "SHOW TABLES LIKE 'admin_%';"   # 6 张表存在
```

> 实现发现（已验证）：
> 1. grape_test_db 是存量库 → application.yml 需 `spring.flyway.baseline-on-migrate: true` + `baseline-version: 0`，
>    否则 Flyway 报 "Found non-empty schema(s) but no schema history table" 拒绝启动
> 2. dev 连接默认值：`127.0.0.1:3306/grape_test_db`，用户 root（测试环境临时账号，密码走 ADMIN_DB_PASSWORD 环境变量，
>    不入 git）；prod 由 ADMIN_DB_URL / ADMIN_DB_USER / ADMIN_DB_PASSWORD 注入，按 D5 用 grape_admin 最小权限账号

**Verification:** flyway 迁移成功（日志 `Migrating schema ... to version 1`）；表结构 + 种子角色/菜单存在；重复启动幂等（flyway 版本控制）。

**Commit:** `feat(admin): admin_* 表结构与种子数据（P0.2）`

### Task P0.3 JPA 实体与 Repository

**Objective:** admin_* 6 表实体 + 共享表只读实体

**Files:**
- Create: `admin/backend/src/main/java/com/grape/admin/entity/AdminUser.java`、`AdminRole.java`、`AdminPermissionMenu.java`、`AdminAuditLog.java`
- Create: `admin/backend/src/main/java/com/grape/admin/repository/AdminUserRepository.java`、`AdminRoleRepository.java`、`AdminPermissionMenuRepository.java`、`AdminAuditLogRepository.java`
- Create: `admin/backend/src/main/java/com/grape/admin/entity/readonly/RuntimeCredentialRo.java`、`AgentRuntimeRo.java`、`AgentTaskRo.java`
- Create: `admin/backend/src/main/java/com/grape/admin/repository/readonly/` 对应 3 个只读 Repository

**Step 1:** 实体对齐真实表结构（共享表先 `SHOW CREATE TABLE` 核对再映射）：

```sql
-- 核对命令（实施时必须执行）
SHOW CREATE TABLE runtime_credentials;
SHOW CREATE TABLE agent_runtimes;
SHOW CREATE TABLE agent_tasks;
SHOW CREATE TABLE meta;
```

**Step 2:** 只读实体加 `@Immutable` + Repository 只暴露查询方法（findAll/findByCredentialId 等），**不提供 save/delete**。

**Step 3:** `mvn -q compile` 通过。

**Verification:** 编译通过；只读 Repository 无写方法。

**Commit:** `feat(admin): JPA 实体与 Repository（P0.3）`

### Task P0.4 JWT 认证（JwtUtil + JwtAuthenticationFilter + SecurityConfig）

**Objective:** 登录发 JWT、请求鉴权、401 语义正确

**Files:**
- Create: `admin/backend/src/main/java/com/grape/admin/config/JwtUtil.java`（对齐 VOD JwtUtil）
- Create: `admin/backend/src/main/java/com/grape/admin/config/JwtAuthenticationFilter.java`
- Create: `admin/backend/src/main/java/com/grape/admin/config/SecurityConfig.java`（WebMvcConfigurer + 拦截器注册，对齐 VOD SecurityConfig/WebMvcConfig）
- Create: `admin/backend/src/main/java/com/grape/admin/config/RbacHandlerInterceptor.java`、`RbacPermissionLoader.java`（P1 完整实现，P0 先注册空实现放行 /api/admin/auth/** 与 /health）

**Step 1:** JwtUtil：HS256 签发/解析，subject=userId，claim=username，过期=app.jwt-expire-minutes；`app.jwt-secret` 为空时启动失败。

**Step 2:** JwtAuthenticationFilter：解析 Authorization Bearer → 校验 → 放入 request attribute；无效/过期 → 401。

**Step 3:** 配置：`/api/admin/auth/**`、`/api/admin/health` 放行；其余 `/api/admin/**` 需认证（拦截器链：JWT 校验 → RBAC 校验）。

**Step 4:** 测试（JUnit）：`JwtUtilTest`（签发→解析→篡改→过期）。

```bash
mvn -q test -Dtest=JwtUtilTest
# 期望: BUILD SUCCESS
```

**Verification:** 无 token 访问 `/api/admin/users` → 401；伪造 token → 401。

**Commit:** `feat(admin): JWT 认证骨架（P0.4）`

### Task P0.5 登录/刷新/登出 API

**Objective:** POST /api/admin/auth/login|refresh|logout 可用，种子管理员可登录

**Files:**
- Create: `admin/backend/src/main/java/com/grape/admin/controller/AuthController.java`
- Create: `admin/backend/src/main/java/com/grape/admin/service/AuthService.java`
- Create: `admin/backend/src/main/java/com/grape/admin/dto/LoginRequest.java`、`LoginResponse.java`

**Step 1:** login：username+password → BCrypt 校验 → 签发 access token（+refresh token，refresh 存内存/可选）；status=0 拒绝登录。

**Step 2:** refresh：refresh token 换新 access token；logout：黑名单（对齐 VOD TokenBlacklist）。

**Step 3:** 验证（ADMIN_SEED_PASSWORD=Test@123 启动后）：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8081/api/admin/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"Test@123"}' | jq -r .token)
curl -s http://127.0.0.1:8081/api/admin/menus -H "Authorization: Bearer $TOKEN"
# 期望: 200，返回 SUPER_ADMIN 的全部菜单树
```

**Verification:** 登录成功返回 token；错误密码 401；menus 接口返回 9 条菜单（含父子结构）；禁用用户 403/401。

**Commit:** `feat(admin): 登录/刷新/登出 + 当前用户菜单（P0.5）`

### Task P0.6 前端骨架（Vue3 + Vite + 登录页）

**Objective:** admin/frontend 可构建、登录页可走通

**Files:**
- Create: `admin/frontend/package.json`、`vite.config.js`、`index.html`、`src/main.js`、`src/App.vue`
- Create: `admin/frontend/src/router/index.js`（路由 + requiresAuth + meta.permission，对齐 VOD）
- Create: `admin/frontend/src/api/index.js`（axios baseURL='/api'、JWT 注入、401 跳登录、x-new-token 续期，对齐 VOD）
- Create: `admin/frontend/src/views/Login.vue`（对齐 VOD Login.vue，去掉 GitCode 相关）
- Create: `admin/frontend/src/utils/auth.js`（token 存取）

**Step 1:** `npm create vite@latest` 骨架或手写 package.json（vue3 + vue-router + axios + vite）。

**Step 2:** vite.config.js：`server.proxy = { '/api': 'http://127.0.0.1:8081' }`；build.outDir = `dist`。

**Step 3:** 路由：`/login` 公开；其余 requiresAuth + meta.permission；登录后按 `/api/admin/menus` 动态渲染侧边菜单。

**Step 4:** 验证：

```bash
cd /opt/grape/admin/frontend && npm install && npm run build
# 期望: dist/ 产出，无报错
npm run dev   # 浏览器登录页 → admin/Test@123 → 进入首页看到三组菜单
```

**Verification:** build 通过；dev 代理 /api → 8081；登录后菜单 = 角色可见菜单（SUPER_ADMIN 全量）。

**Commit:** `feat(admin): Vue3 前端骨架与登录（P0.6）`

### Task P0.7 P0 集成验收

**Steps:**
- [ ] 后端启动无报错，flyway 幂等
- [ ] 前端 build + dev 跑通
- [ ] 端到端：登录 → 拿到菜单 → 访问受保护接口
- [ ] 与 grape 账号隔离验证：grape 的 session token 访问 admin → 401；admin token 访问 grape → 401
- [ ] `git commit` 各子任务已按 P0.1-P0.6 提交

**P0 验收标准:** 「独立登录可用，与 grape 账号互不影响」达成。

---

## Phase P1：系统管理（用户管理 + 角色权限配置）

### Task P1.1 用户管理 API

**Files:**
- Create: `admin/backend/.../controller/UserController.java`（/api/admin/users）
- Create: `admin/backend/.../service/UserService.java`
- Create: `admin/backend/.../dto/UserDto.java`、`CreateUserRequest.java`、`AssignRolesRequest.java`

**API:**
- GET /api/admin/users（分页 + 用户名模糊搜索，含角色列表）
- POST /api/admin/users（username 唯一校验 + BCrypt 密码）
- PUT /api/admin/users/{id}/status（启用/禁用；SUPER_ADMIN 用户禁操作）
- PUT /api/admin/users/{id}/roles（绑定角色；SUPER_ADMIN 角色不可分配给禁用用户）

**Verification:** curl 增删改查 + 重复用户名 400；JUnit：唯一约束、禁用用户登录拒绝。

**Commit:** `feat(admin): 用户管理 API（P1.1）`

### Task P1.2 角色与菜单权限 API

**Files:**
- Create: `admin/backend/.../controller/RoleController.java`（/api/admin/roles、/api/admin/menus）
- Create: `admin/backend/.../service/RoleService.java`

**API:**
- GET /api/admin/roles（含每角色已挂菜单 id 列表）
- POST /api/admin/roles（code 唯一；code 含 SUPER_ADMIN/VIEWER 保留字拒绝）
- DELETE /api/admin/roles/{id}（is_protected=1 拒绝；已绑定用户拒绝并提示先解绑）
- GET /api/admin/roles/{id}/permissions → {menuIds: []}
- PUT /api/admin/roles/{id}/permissions（全量替换 role_menu_rel；保护角色拒绝）
- GET /api/admin/menus（当前用户可见菜单树，登录态用）

**Verification:** 保护角色 403/400；删除已绑定角色被拒；权限保存后重新登录生效。

**Commit:** `feat(admin): 角色与菜单权限 API（P1.2）`

### Task P1.3 RBAC 拦截器完整实现

**Files:**
- Modify: `admin/backend/.../config/RbacHandlerInterceptor.java`、`RbacPermissionLoader.java`

**Step 1:** RbacPermissionLoader：缓存 role→menu 映射，菜单变更时失效（版本号或 60s 缓存）。

**Step 2:** RbacHandlerInterceptor：按「请求路径 → menu.path 前缀匹配」校验；无权限 → 403；SUPER_ADMIN 全放行；VIEWER 仅放行 GET。

**Step 3:** JUnit：`RbacInterceptorTest`——SUPER_ADMIN 全过、VIEWER 写操作 403、无菜单角色 403。

**Verification:** 角色改菜单后权限立即生效（缓存失效策略验证）。

**Commit:** `feat(admin): RBAC 拦截器（P1.3）`

### Task P1.4 前端 UserManage.vue

**Files:**
- Create: `admin/frontend/src/views/UserManage.vue`（对齐 VOD UserManage.vue：表格 + 新增对话框 + 状态切换 + 角色分配下拉）

**Verification:** 建用户 → 绑 OPS 角色 → 该用户登录后只见 OPS 可见菜单。

**Commit:** `feat(admin): 用户管理页（P1.4）`

### Task P1.5 前端 RolePermissionConfig.vue

**Files:**
- Create: `admin/frontend/src/views/RolePermissionConfig.vue`（对齐 VOD 同名组件：左侧角色列表 + 右侧菜单权限树 + 保存；保护角色锁定 🔒）

**Verification:** 勾选菜单 → 保存 → 该角色用户重新登录菜单变化。

**Commit:** `feat(admin): 角色权限配置页（P1.5）`

### Task P1.6 P1 验收

- [ ] 角色/菜单/用户绑定闭环：建角色→配菜单→建用户→绑角色→登录→菜单正确
- [ ] 保护角色（SUPER_ADMIN/VIEWER）不可改不可删
- [ ] 无权限访问返回 403（后端拦截 + 前端菜单隐藏）

**P1 验收标准:** 「角色/菜单/用户绑定闭环」达成。

---

## Phase P2：运行时管理（设备/引擎/凭据/任务/清理）

### Task P2.1 设备聚合服务（读共享库）

**Files:**
- Create: `admin/backend/.../service/RuntimeDeviceService.java`
- Create: `admin/backend/.../dto/DeviceSummaryDto.java`、`DeviceDetailDto.java`

**Step 1:** 设备列表 = runtime_credentials 全量 + 聚合：
- 每设备引擎列表（agent_runtimes WHERE credential_id=?）
- 引擎数、任务数（agent_tasks 按 runtime_id 关联 engine 再归设备，或按 credential 关联口径——实现时以 `SHOW CREATE TABLE agent_tasks` 实际列确认关联键）
- 最后心跳 = last_seen_at、最后 IP = last_ip

**Step 2:** 健康状态计算（**先读 `/opt/grape/src/runtime/health.py` 核对规则后实现**）：online / recently_lost / offline / about_to_gc。

**Verification:** dev 库有真实 worker 数据时，列表与 grape WebUI Workers 页一致（交叉核对）。

**Commit:** `feat(admin): 设备聚合与健康状态（P2.1）`

### Task P2.2 设备/引擎/任务查询 API

**Files:**
- Create: `admin/backend/.../controller/RuntimeController.java`（/api/admin/devices、/api/admin/engines、/api/admin/tasks）

**API:**
- GET /api/admin/devices（聚合列表，按状态筛选）
- GET /api/admin/devices/{cid}（详情：设备信息 + 引擎表 + 最近任务）
- GET /api/admin/engines（跨设备引擎总览：name/version 分布、健康度）
- GET /api/admin/tasks?device=&status=&limit=（任务查询，只读）

**Verification:** 与 grape WebUI 数据一致；无数据时返回空数组 200。

**Commit:** `feat(admin): 设备/引擎/任务查询 API（P2.2）`

### Task P2.3 凭据签发/吊销（sha256 契约）

**Files:**
- Create: `admin/backend/.../service/RuntimeCredentialService.java`

**Step 1:** 签发：`token = "rta_" + SecureRandom(32 bytes) base64url` → 落库 `sha256(token)` 小写 hex + device_name + status=active → 返回明文一次。

**Step 2:** 吊销：status=active → revoked（幂等），写 audit_log(action=credential.revoke)。

**Step 3:** 与 grape 契约核对：读 `/opt/grape/src/api/routes/runtime.py` 的 `_auth_credential`（sha256 → 查 token_hash → active）确认一致。

**Step 4:** JUnit：`CredentialHashTest`——签发→用同一哈希算法重算一致；吊销后模拟 grape 校验路径返回 401 语义。

**Verification（真实互通）:** 在 dev worker 机器上执行：
```bash
# 用 admin 签发的 token 重装/重启 worker（runtime.env 换新 token）
systemctl restart runtime-agent
journalctl -u runtime-agent -n 20 | grep -E "RuntimeAgent registered"
# 期望: 注册成功 —— 证明 admin 签发的 token 与 grape 协议互通
```

**Commit:** `feat(admin): 凭据签发/吊销（P2.3）`

### Task P2.4 清理服务（护栏 + 审计）

**Files:**
- Create: `admin/backend/.../service/RuntimeCleanupService.java`

**实现约束（Global Constraints 6 + 设计文档 §6）：**
- `cleanupDevice(cid)`：仅当设备所有引擎状态非 running/claimed、且凭据可吊销时执行：吊销凭据 → 删 agent_runtimes(该 credential_id) → 删孤儿 agent_tasks（status ∈ 终态集合）
- `deleteEngine(id)`：该引擎存在 dispatched/running 任务 → 拒绝（409 + 明确提示）
- `cleanupTasks({status[], before})`：仅允许终态 status（completed/failed/cancelled）+ 时间范围；进行中状态入参直接 400
- 所有操作写 admin_audit_log（user/action/target/detail/时间）
- **先核对 grape agent_tasks 状态枚举**（`src/runtime/service.py` + 设计文档 §2.3：queued/dispatched/running/completed/failed/cancelled）再实现终态集合

**API:** POST /api/admin/devices/{cid}/cleanup、DELETE /api/admin/engines/{id}、POST /api/admin/tasks/cleanup（均需 RBAC + 审计）

**Verification:** JUnit：有 running 任务的引擎删除被拒；清理后 audit_log 有记录；幂等（重复清理 200/无副作用）。

**Commit:** `feat(admin): 运行时清理服务（P2.4）`

### Task P2.5 前端 DeviceManage.vue

**Files:**
- Create: `admin/frontend/src/views/DeviceManage.vue`（列表 + 健康徽章（样式对齐 grape `src/api/static/js/pages/runtimes.js` 的 HEALTH_BADGE）+ 详情抽屉 + 凭据签发对话框（明文一次性展示 + 「已保存」确认）+ 吊销/回收（输入确认词二次确认）+ 恢复孤儿按钮）

**Verification:** 列表与 grape Workers 页一致；签发流程拿到明文提示；吊销/回收有确认词；操作后审计可查。

**Commit:** `feat(admin): 设备管理页（P2.5）`

### Task P2.6 前端 EngineOverview.vue

**Files:**
- Create: `admin/frontend/src/views/EngineOverview.vue`（引擎分组卡片：name/version/健康度计数/分布表 + 删除入口（保护：有任务拒绝提示））

**Verification:** 与设备详情内嵌引擎表数据一致。

**Commit:** `feat(admin): 引擎总览页（P2.6）`

### Task P2.7 P2 验收

- [ ] 设备列表/详情/引擎总览与 grape WebUI 数据一致
- [ ] 签发 token 在真实 worker 上注册成功（互通验证，P2.3 Step 4）
- [ ] 吊销后 worker 401；清理拒绝进行中任务；审计日志完整
- [ ] 不可逆操作全部有确认词

**P2 验收标准:** 「设备在线状态正确、签发 token 可被 worker 使用」达成。

---

## Phase P3：AI 助手

### Task P3.1 移植 ai_hermes_query.py

**Files:**
- Create: `admin/backend/ai_hermes_query.py`（从 `/root/VOD-Developer-skills/vod-system/backend/ai_hermes_query.py` 移植：--query / --session-id 参数、HERMES_BIN 解析、流式 JSON 事件 {type: token|session|done}、echarts ```chart 指引）

**Verification:** `HERMES_BIN=$(which hermes) python3 ai_hermes_query.py --query "1+1"` 输出流式 JSON 事件。

**Commit:** `feat(admin): AI 助手代理脚本（P3.1）`

### Task P3.2 AI 聊天 API（ProcessBuilder + SSE）

**Files:**
- Create: `admin/backend/.../controller/AiController.java`（POST /api/admin/ai/chat，SSE 返回）
- Create: `admin/backend/.../service/AiAssistantService.java`（对齐 VOD AiQueryService：ProcessBuilder + HERMES_BIN/query-script 配置 + 流式解析转发 + 会话续接 + 超时/并发上限）

**实现约束：**
- 并发上限（如 3 个同时会话，超出 429）+ 单次流式超时（如 120s）
- hermes 通过环境变量注入共享库只读凭据（对齐 VOD「B 机只读用户」，凭据来自 `HERMES_DB_URL/USER/PASSWORD` 环境变量，不写死）
- 失败返回结构化错误事件 {type:"error"}

**Verification:** curl -N 流式收到 token 事件；session_id 续接上下文；并发第 4 个 429。

**Commit:** `feat(admin): AI 助手聊天 API（P3.2）`

### Task P3.3 前端 AiAssistant.vue

**Files:**
- Create: `admin/frontend/src/views/AiAssistant.vue`（聊天界面：流式渲染（SSE/fetch stream）、会话列表续接、```chart 块渲染为 echarts（引入 echarts 依赖））

**Verification:** 提问「现在有多少台设备在线」→ hermes（配只读凭据）回答基于真实数据；图表块正常渲染；刷新页面后会话可续接。

**Commit:** `feat(admin): AI 助手页（P3.3）`

### Task P3.4 P3 验收

- [ ] 流式问答可用、会话可续接
- [ ] 并发上限与超时生效
- [ ] hermes 账号确认只读（尝试写操作被 MySQL 拒绝）

**P3 验收标准:** 「流式问答可用，会话可续接」达成。

---

## Phase P4：部署

### Task P4.1 nginx-admin.conf

**Files:**
- Create: `admin/deploy/nginx-admin.conf`

```
server {
  listen 80; server_name admin.example.com;
  root /opt/vod/admin/frontend/dist;
  location /api/ { proxy_pass http://127.0.0.1:8081; proxy_set_header X-Forwarded-Proto $scheme; proxy_buffering off; }
  location / { try_files $uri $uri/ /index.html; }
}
```

**Verification:** 域名解析后浏览器访问 admin.example.com 全链路可用（前端 + /api 反代）。

**Commit:** `feat(admin): nginx 配置（P4.1）`

### Task P4.2 systemd 服务

**Files:**
- Create: `admin/deploy/admin-backend.service`（Environment=ADMIN_DB_URL/ADMIN_DB_USER/ADMIN_DB_PASSWORD/ADMIN_JWT_SECRET/ADMIN_SEED_PASSWORD/HERMES_*；ExecStart=java -jar /opt/vod/admin/backend/admin-backend-*.jar --server.port=8081；Restart=always）
- Create: `admin/deploy/deploy.sh`（build 前端 + 打包后端 + 同步到 /opt/vod/admin + systemctl restart）

**Verification:** `systemctl start admin-backend` → active；重启后自动恢复。

**Commit:** `feat(admin): systemd 与部署脚本（P4.2）`

### Task P4.3 部署与验收

- [ ] admin.example.com 独立域名全链路可用
- [ ] 与 grape 服务互不影响（grape 重启/停服时 admin 仍可看设备历史）
- [ ] DBA 执行 grape_admin GRANT（SELECT 按需 + 三张运行时表写 + admin_* 全权限）后以该账号运行验证
- [ ] 方案验收清单逐项过：菜单、账号隔离、凭据互通、清理护栏、AI 助手
- [ ] 部署验证通过 → 才允许合并 `feat/admin-console` → `dev`（合并动作等验收通过后另行执行，不在本计划内自动执行）

**P4 验收标准:** 「独立域名全链路可用」+ 部署验证无问题。

---

## 里程碑与合并门禁

- P0-P4 每阶段结束：全部验收项勾选 + commit 已推 `feat/admin-console`
- 特性分支部署验证无问题 → 人工确认 → `git checkout dev && git merge feat/admin-console`（由用户在验收后发起，agent 不自动合并）

## 风险与注意（实现时）

1. 共享表结构以实际 MySQL `SHOW CREATE TABLE` 为准，先核对再映射实体
2. grape 的 `binary` 列是 MySQL 保留字（历史 bug 根源）——JPA 映射该列必须 `@Column(name="`binary`")` 或重命名列名，任何直接 SQL 都要反引号
3. 健康状态规则实现前必须读 `src/runtime/health.py` 原文，别凭设计文档猜
4. agent_tasks 状态枚举实现前读 `src/runtime/service.py` 确认终态集合
5. VOD 代码仅作参照：Controller/页面结构可直接借鉴，但账号表/接口路径/菜单 code 一律按本计划，不照抄 VOD 命名
