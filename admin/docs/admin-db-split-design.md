# admin 运营管理台数据库拆分设计（grape_admin_db 独立库）

版本：v1.0（定稿，评审确认版）
日期：2026-08-13
分支：feat/admin-console
状态：已评审确认——§5 六个决策点全部按推荐项定稿，进入实施

修订说明（v0.3 → v1.0）：
- §5 六项决策评审确认（全部选推荐项）：①原生双 DataSource ②旧库 admin 表保留至稳定期
  ③结构+全量数据迁移 ④Flyway 基线方式（baseline-version=16）⑤dev 先行后上 prod ⑥停服迁移
- 正文按确认决策定稿（正文内容 v0.3 即按推荐项撰写，无需结构性变更）
- 修正架构图 grape 业务表数量（8 → 10 张）

修订说明（v0.2 → v0.3，二轮攻击性评审）：
- P11：修正方案内部矛盾——「全量数据迁移」与「Flyway V1–V16 重放」冲突（V10/V16 含固定 id 菜单 INSERT，重放必主键冲突）；改为 Flyway 基线方式（baseline-version=16，结构+数据以旧库为准，V17+ 正常应用）
- P12：补充迁移时序——必须停服迁移（停 admin-backend → dump → 导入 → 改配置 → 启动），防切换窗口增量数据丢失
- P13：readonly 仓库事务归属——@EnableJpaRepositories 对 readonly 包显式指定 transactionManagerRef="grapeTxManager"，防 @Transactional 解析到 @Primary 错绑
- P14：配置切换补 SPRING_FLYWAY_ENABLED 恢复项
- P15：健康检查盲区说明——HealthService 无 SQL，双库连通性由 HikariCP 启动期连接兜底（initializationFailTimeout 默认 1，连不上=启动失败）
- P16：自定义双 DataSource 后 spring.datasource.hikari 自动配置失效，需手动创建 HikariDataSource 绑定参数
- 二轮确认：主项目 src/ 零引用 admin_* 表（拆库对 FastAPI 主服务无影响）；EnvironmentFile=/etc/grape/admin.env 全链路透传

## 1. 背景与目标

当前 admin 运营管理台后端（Spring Boot 2.7.18，/opt/grape/admin/backend）与 grape 主服务共享同一个 MySQL 库 `grape_test_db`（dev 环境 127.0.0.1:3306）。管理台既读写自己的 `admin_*` 表，也读写 grape 业务表（设备/引擎/凭据/技能等）。

诉求：把 admin 运营管理台自有表迁入新库 `grape_admin_db`，管理台对 admin 表的读写走新库；对 grape 业务数据的读写仍走原 `grape_test_db`。实现「库级隔离、应用层双数据源」：

```
┌──────────────────────┐        ┌─────────────────────────┐
│   admin 运营管理台     │  JPA   │  grape_admin_db（新）     │
│  (Spring Boot :8081)  │ ─────▶ │  admin_* 共 9 张表        │
│                       │        └─────────────────────────┘
│  Admin*Repository     │        ┌─────────────────────────┐
│  readonly RoRepo      │  JPA   │  grape_test_db（原）      │
│  JdbcTemplate(双)     │ ─────▶ │  agent_runtimes/... 共   │
│                       │        │  10 张 grape 业务表       │
└──────────────────────┘        └─────────────────────────┘
```

## 2. 现状分析（基于代码事实）

### 2.1 表归属清单

admin 管理台后端共访问 19 张表，归属如下：

**A 类：admin 自有表（9 张，全部 `admin_` 前缀）→ 迁往 grape_admin_db**

| 表名 | 来源迁移 | 用途 | 读写 |
|---|---|---|---|
| admin_users | V1 | 管理台用户 | RW |
| admin_roles | V1 | 角色（SUPER_ADMIN/VIEWER/OPS） | RW |
| admin_permission_menus | V1 | 菜单/权限点 | RW |
| admin_role_menu_rel | V1 | 角色-菜单关联 | RW |
| admin_user_role_rel | V1 | 用户-角色关联 | RW |
| admin_audit_log | V1 | 审计日志 | W |
| admin_ai_history | V5 | AI 会话历史 | RW |
| admin_ai_template | V6 | AI 提示词模板 | RW |
| admin_agent_alert | V15 | Agent 告警阈值 | RW |

**B 类：grape 业务表（10 张）→ 留在 grape_test_db**

| 表名 | 用途 | admin 侧读写 |
|---|---|---|
| agent_runtimes | 设备列表 | 读 + DELETE（清理） |
| agent_tasks | 任务统计/清理 | 读 + DELETE |
| agent_type | 引擎列表/启停 | 读 + UPDATE（enable/default_runtime_id） |
| managed_repos | 代码仓列表 | 读 |
| runtime_credentials | 凭据签发/吊销 | RW（INSERT/UPDATE/DELETE） |
| skill | 技能列表/上传 | RW（INSERT/UPDATE/DELETE + official 化） |
| devops_agent_session | Agent 实例/统计 | 读 |
| issue_agent_session | Issue 级 Agent 会话/统计 | 读 |
| events | 事件存档/统计 | 读 |
| user_accounts | 技能 owner 用户名/平台用户统计 | 读 |

### 2.2 访问点分布（改造清单依据）

ORM 为 Spring Data JPA + JdbcTemplate 混用：

- **JPA Repository（14 个）**：
  - 8 个 `Admin*Repository` → 只碰 A 类表（admin 库）
  - 6 个 `repository/readonly/*RoRepository`（AgentRuntimeRo/AgentTaskRo/AgentTypeRo/ManagedRepoRo/RuntimeCredentialRo/SkillRo）→ 只碰 B 类表（grape 库）；其中 SkillRoRepository 含 nativeQuery 写 skill 表（INSERT/DELETE）
- **JdbcTemplate（7 个类，约 45 处调用点，混用两类表）**：

| 类 | A 类 SQL（→ adminJdbcTemplate） | B 类 SQL（→ grapeJdbcTemplate） |
|---|---|---|
| StatsController | admin_users ×2（60/101） | agent_runtimes/agent_tasks/agent_type/runtime_credentials/managed_repos/user_accounts/events（33–107） |
| AgentService | admin_agent_alert ×2（161/242） | agent_type/devops_agent_session/issue_agent_session/agent_tasks（170/206/224/255/262/269） |
| RepoService | — | managed_repos（69） |
| RuntimeCleanupService | — | runtime_credentials/agent_runtimes/agent_tasks/agent_type（全部） |
| RuntimeCredentialService | — | runtime_credentials（44/54/62） |
| SkillService | — | user_accounts/skill（153/193/221） |
| TemplatesService | — | managed_repos（96） |

> 注：`countByColumn(table, column)` 为动态表统计，调用方传参均为硬编码常量表名（devops_agent_session/issue_agent_session），无用户输入拼接，整方法绑定 grapeJdbcTemplate。

### 2.3 事务边界（关键结论）

- **无跨库 JOIN**：全部单表 SQL，JPA 实体均单表映射 —— 双数据源可干净切分，无跨库查询。
- `@Transactional` 仅出现在 UserService / RoleService / AiTemplateService（纯 A 类表），无跨库事务。
- **跨库写已存在但均独立提交**：如签发凭据 = INSERT runtime_credentials（B 库）+ auditService.record → admin_audit_log（A 库），两写在方法内分别自动提交，当前就不是单事务。拆库后语义不变，**无需分布式事务**。

### 2.4 其他相关点

- Flyway：迁移文件 V1–V16 在 `classpath:db/migration`；admin.env 已用 `SPRING_FLYWAY_ENABLED=false` 禁用（共享库 missing 校验问题，见记忆）。
- 启动期组件：AdminSeedInitializer（种子 admin 用户/角色）、RbacPermissionLoader（菜单权限加载）→ 均走 admin 库。
- ai_hermes_query.py：AI 助手 spawn Hermes CLI，通过 `GRAPE_DB_*` 环境变量提供**只读** MySQL 访问提示（仅 SELECT 权限账号），面向 grape 业务数据 → 迁移后仍指向 grape_test_db 不变。
- 技能上传除写 skill 表（B 库）外还落盘文件系统（SKILLS_DIR），与数据库迁移无关。

## 3. 目标架构

```
Spring Boot admin-backend
├── adminDS（默认/@Primary）→ grape_admin_db
│   ├── Admin*Repository ×8
│   ├── adminJdbcTemplate（A 类 SQL）
│   ├── AdminSeedInitializer / RbacPermissionLoader
│   └── 事务管理器 adminTxManager（User/Role/AiTemplate service 的 @Transactional）
├── grapeDS → grape_test_db
│   ├── readonly/*RoRepository ×6（含 SkillRo 写 skill）
│   └── grapeJdbcTemplate（B 类 SQL）
└── Flyway：仅绑定 adminDS，V1–V16 全量应用到 grape_admin_db
```

## 4. 方案设计

### 4.1 数据库与数据迁移

**迁移时序（P12，必须停服，防切换窗口增量丢失）**：

```
① 停 admin-backend（systemctl stop admin-backend）→ 管理台短暂不可用（分钟级）
② dump 旧库 9 张 A 类表结构+数据（mysqldump grape_test_db admin_* ...）
③ 建 grape_admin_db 并导入
④ 核对：行数 / 自增计数 / information_schema 结构 diff
⑤ 改 admin.env（ADMIN_DB_* 切新库 + GRAPE_DB_* 新增 + Flyway 恢复）
⑥ 启动 admin-backend → 健康检查 → 功能回归
```

停服窗口内不产生新的 admin 数据（审计/凭据操作均不可用），无增量丢失。

1. 新建 `grape_admin_db` 库（UTF-8，与现库同字符集）。
2. 迁移 9 张 A 类表的结构 + 数据（保留自增 id、索引、唯一键；无外键依赖，顺序无关）：

```sql
-- 逐表：结构 + 数据（示例）
CREATE TABLE grape_admin_db.admin_users LIKE grape_test_db.admin_users;
INSERT INTO grape_admin_db.admin_users SELECT * FROM grape_test_db.admin_users;
-- 9 张表同上（admin_users/admin_roles/admin_permission_menus/admin_role_menu_rel/
--   admin_user_role_rel/admin_audit_log/admin_ai_history/admin_ai_template/admin_agent_alert）
```

或等价的 `mysqldump --databases` 拆分方案。迁移后核对：
1. **行数**：双库逐表 COUNT(*) 一致；
2. **自增计数**：`information_schema.tables` 的 AUTO_INCREMENT 对齐（避免主键冲突）；
3. **结构 diff（P5）**：对比双库 `information_schema.columns`（列名/类型/默认值/索引），防止旧库实际结构 vs V1–V16 迁移文件漂移（历史手动 ALTER 无迁移记录）；不一致时以旧库实际结构为准补齐新库。

3. **Flyway 基线方式（P11，解决「全量数据迁移」与「迁移文件重放」的矛盾）**：
   - 新库 `spring.flyway.baseline-on-migrate=true` + `baseline-version=16`：Flyway 将 V1–V16 标记为已应用，**不重放**任何历史迁移（含 V10/V16 的固定 id 菜单 INSERT，避免与已迁移数据主键冲突）；
   - 表结构与数据全部以旧库实际为准（与核对项 3 一致）；
   - 未来新增迁移 V17+ 在新库正常应用（基线之上）。
   - 备选（不推荐，丢失自定义数据）：先 Flyway migrate V1–V16（结构+菜单种子由迁移文件重建），再只迁业务数据表（admin_users 非种子用户、admin_audit_log、admin_ai_history、admin_ai_template、admin_agent_alert）；代价是旧库自定义角色/定制菜单丢失。
4. **grape_test_db 中的 9 张 A 类表暂不删除**，保留至稳定期（见决策点 2），用于回滚与双写校验。

### 4.2 后端双数据源（零新依赖，Spring Boot 原生）

新增 `config/DataSourceConfig.java`：

- `adminDS`：@Primary，指向 grape_admin_db（复用现有 ADMIN_DB_* 配置）
- `grapeDS`：指向 grape_test_db（新增 GRAPE_DB_* 配置）
- 两个 `EntityManagerFactory`：`adminEmf` 扫描 `entity`（**必须用 @EntityScan excludeFilters 排除 `entity.readonly` 子包的 6 个 Ro 实体**，否则两个持久化单元注册同一实体类，启动冲突，见 P4）+ `repository`；`grapeEmf` 扫描 `entity.readonly` + `repository.readonly`
- **Repository 事务归属（P13）**：`@EnableJpaRepositories` 分两段配置——admin 包（basePackages=`repository`，entityManagerFactoryRef=`adminEmf`，transactionManagerRef=`adminTxManager`）与 readonly 包（basePackages=`repository.readonly`，entityManagerFactoryRef=`grapeEmf`，transactionManagerRef=`grapeTxManager`）。否则双 TM 下 repository 的 @Transactional 默认解析到 @Primary，readonly 仓库的写操作（skill INSERT/DELETE）会错绑 adminTxManager
- 两个 `JdbcTemplate` bean：`adminJdbcTemplate` 与 `grapeJdbcTemplate`，**均不标 @Primary**；7 个使用类的注入点全部显式 @Qualifier（见 P3：漏改=启动失败 NoUniqueBeanDefinitionException，而非静默错库）
- 两个事务管理器：`adminTxManager`（@Primary，`@Transactional` 默认走它）与 `grapeTxManager`
- **DataSource 创建（P16）**：自定义双 DataSource 后 Spring Boot DataSourceAutoConfiguration back off，`spring.datasource.hikari` 自动配置不再生效 → 两个 DataSource 均手动创建 HikariDataSource（连接池参数显式设置，或 @ConfigurationProperties 绑定各自前缀），并利用 Hikari 启动期连接（initializationFailTimeout 默认 1）做双库连通 fail-fast（P15）

不引入 dynamic-datasource 等第三方依赖：仓库按包天然二分（admin vs readonly），无需运行时注解切库，依赖最少、行为最可预期（权衡见决策点 1）。

### 4.3 代码改造点清单（feat/admin-console 分支）

1. 新增 `config/DataSourceConfig.java`（双数据源/双 EMF/双 JdbcTemplate/双事务管理器；@EntityScan excludeFilters 排除 readonly 包）。
2. **JdbcTemplate 注入点改造（7 个类，~45 处 SQL 按表归属绑定）**：
   - 只碰 B 类表的类（RepoService、RuntimeCleanupService、RuntimeCredentialService、SkillService、TemplatesService）→ 构造注入改为 `@Qualifier("grapeJdbcTemplate")`
   - 混用的 StatsController、AgentService → 注入两个 JdbcTemplate（A 类 SQL 用 adminJdbcTemplate，B 类用 grapeJdbcTemplate），按 2.2 清单逐处核对
   - 每处改动后对照 2.2 清单复核，防漏改
3. `SkillRoRepository` 等 readonly 仓库：随 grapeEmf 自动绑 grape 库；`@Modifying` nativeQuery 事务边界走 grapeTxManager。
4. `AdminSeedInitializer` / `RbacPermissionLoader`：默认走 adminDS，无改动。
5. `application.yml`：新增 `grape.datasource.*` 配置段（url/user/password 走 `GRAPE_DB_*` 环境变量，默认值指向 dev grape_test_db）；注意默认值仅限 dev，prod 必须显式注入（见 P10）。
6. Flyway：`spring.flyway` 配置绑定到 adminDS（新库无历史，`baseline-on-migrate` 可关，`ignore-missing` 可移除）；grape_test_db 不再挂 admin 迁移。
   - **与代码级 FlywayMigrationStrategy 的关系（v0.2，P6）**：AdminApplication 现有 `FlywayMigrationStrategy`（validateOnMigrate(false)+migrate()）在新库启用 Flyway 后仍生效。全新库无 missing/applied-not-resolved 场景，validate 关闭无副作用；但该策略隐含「容忍迁移文件缺失」，若未来多部署共享 grape_admin_db（dev/prod 同实例）会重演共享库 missing 问题 → 约束：**grape_admin_db 仅允许单部署连接**（当前 dev 唯一），多部署接入前必须统一迁移文件版本。
7. 配置管理：GRAPE_DB_* 加入 admin.env 必填校验（缺失即启动失败），防 prod 漏配（P10）。

### 4.4 配置切换（dev）

`/etc/grape/admin.env`：

```
ADMIN_DB_URL=jdbc:mysql://127.0.0.1:3306/grape_admin_db?...   # 改为新库
ADMIN_DB_USER=grape_admin        # 建议独立账号：仅 grape_admin_db 权限（见 P9）
GRAPE_DB_URL=jdbc:mysql://127.0.0.1:3306/grape_test_db?...   # 新增，指向原库
GRAPE_DB_USER=grape_admin_readwrite  # 建议最小权限账号：grape 业务表 DML，无 admin 库权限（P9）
GRAPE_DB_PASSWORD=***
SPRING_FLYWAY_ENABLED=true      # P14：由 false 恢复为 true（配合基线方式，见 4.1）
```

prod 同构：prod 的 ADMIN_DB_URL 切到 prod grape_admin_db，新增 GRAPE_DB_* 指向 prod grape_test_db（见决策点 5）。

### 4.5 AI 助手

`GRAPE_DB_*` 环境变量（ai_hermes_query.py 透传）保持指向 grape_test_db —— AI 查询的是 grape 业务数据（设备/任务/凭据/事件统计）。边界明示（P8）：AI 助手无法查询 admin 库数据（管理台用户/角色/审计/技能管理数据）；若未来 AI 分析需覆盖管理台数据，需另配 grape_admin_db 只读账号并在 db_hint 中追加，本次不做。

## 5. 关键决策记录（已评审确认，全部按推荐项定稿）

| # | 决策点 | 结论（2026-08-13 确认） | 理由 |
|---|---|---|---|
| 1 | 双数据源实现 | **原生双 DataSource**（零新依赖，按包划分 admin/readonly） | 方法内混库场景（StatsController/AgentService）@DS 覆盖不了；事务语义清晰无「事务内切库失效」坑；fail-fast（漏改=启动失败）优于静默错库；零依赖符合项目现状 |
| 2 | 旧库 9 张 admin 表 | **保留至稳定期后清理** | 回滚零成本（配置切回即恢复）；稳定期后再评估清理 |
| 3 | 数据迁移范围 | **结构 + 全量数据**（含审计日志/AI 历史） | 审计/AI 历史有追溯价值，不丢历史记录 |
| 4 | 新库 Flyway | **基线方式**：baseline-version=16，V1–V16 不重放，V17+ 正常应用 | 与决策点 3 联动，防固定 id 菜单种子 INSERT 与已迁数据主键冲突 |
| 5 | prod 拆分节奏 | **dev 验证通过后按同方案上 prod** | 降低风险，prod 复用同一套迁移/配置清单 |
| 6 | 迁移时序 | **停服迁移**（分钟级窗口，无增量丢失） | 无增量补迁脚本复杂度，dev 窗口可接受 |

> 联动说明：决策点 3（全量数据）与 4（Flyway 基线）已配对确认；若未来改选「仅结构+种子」，Flyway 策略需同步改为全量应用。

## 6. 影响面与风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| JdbcTemplate ~45 处调用点漏改（查错库） | 高 | 两个 JdbcTemplate 均不标 @Primary + 全部显式 @Qualifier → 漏改=启动失败（fail-fast）；按 2.2 清单逐处核对；验证阶段用「grape_test_db 临时 DROP admin 表」反向证明已切走 |
| 双 EMF 实体扫描冲突（readonly 子包被双注册） | 中 | @EntityScan excludeFilters 排除 entity.readonly（P4），启动期即暴露 |
| 旧库结构 vs 迁移文件漂移 | 中 | 迁移后 information_schema 结构 diff（P5），以旧库实际结构为准 |
| 数据迁移 vs Flyway 重放冲突（菜单种子固定 id） | 高 | 基线方式 baseline-version=16，历史迁移不重放（P11）；决策点 3/4 联动 |
| 迁移窗口增量数据丢失 | 高 | 停服迁移时序（P12），窗口内无 admin 写入 |
| repository 事务错绑（readonly 写操作） | 中 | @EnableJpaRepositories 分两段指定 transactionManagerRef（P13） |
| 双库连通性未被 health 覆盖 | 低 | 依赖 Hikari 启动期连接 fail-fast（P15）；验证阶段补双库 ping |
| 跨库写语义变化 | 低 | 现状即独立提交（如签发凭据+审计），拆库后不变；文档明示约束：禁止新增跨库事务/JOIN；审计写失败→接口 500 但主操作已生效（现状已知行为，P7） |
| Flyway 版本冲突（多部署共享新库） | 低 | 约束 grape_admin_db 仅单部署连接（P6）；多部署前统一迁移文件版本 |
| dev/prod 配置遗漏 GRAPE_DB_*/Flyway 开关 | 中 | admin.env 必填校验（缺失即启动失败）；部署清单化（P10/P14） |
| Hikari 池参数失效 | 低 | 手动创建 HikariDataSource 绑定参数（P16） |
| 管理台不可用窗口 | 低 | 停服迁移仅阶段 1 有分钟级窗口；代码改造+配置切换一次部署完成 |

## 7. 实施步骤

- 阶段 1（数据库，停服）：停 admin-backend → 建 grape_admin_db → 迁移 9 表 → 行数/自增/结构 diff 核对（P5/P12）
- 阶段 2（代码，feat/admin-console）：DataSourceConfig（excludeFilters + 分两段 @EnableJpaRepositories + Hikari 手动创建）→ 7 个类的 JdbcTemplate 拆分（@Qualifier）→ yml 配置段（含 Flyway 基线参数）
- 阶段 3（部署）：改 admin.env（ADMIN_DB_* 切新库 + GRAPE_DB_* 新增 + SPRING_FLYWAY_ENABLED=true）→ deploy.sh 部署 → 健康检查
- 阶段 4（验证）：见 §8；通过后按决策点 2/5 决定清理与 prod

## 8. 验证方案

1. 管理台全功能回归：登录/RBAC/菜单渲染、运行时（设备/引擎/凭据签发吊销/清理）、技能（上传/转官方/删除）、AI 助手、统计页、审计日志。
2. 落库验证：签发凭据后分别查 `grape_admin_db.admin_audit_log` 有新审计、`grape_test_db.runtime_credentials` 有新凭据。
3. 反向验证：在 grape_test_db 临时 DROP `admin_users`，管理台登录/用户管理不受影响（证明 admin 流量已切走）；恢复该表。
4. 重启验证：admin-backend 重启后种子/RBAC 加载正常（读 grape_admin_db）。

## 9. 回滚方案

- 代码回退：git revert / 切回原提交，重新部署。
- 配置回退：admin.env 的 ADMIN_DB_URL 指回 grape_test_db（旧库 admin 表仍在，见决策点 2），无需数据回迁。
- 数据层面零风险：新库只增不改旧库（B 类表仍在原库原位）。
