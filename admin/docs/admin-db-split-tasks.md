# admin 数据库拆分——开发任务清单

来源：admin/docs/admin-db-split-design.md（v1.0 定稿，6 决策点已确认）
分支：feat/admin-console
状态：待确认后执行

## 改动范围约束（硬性）
- 所有代码/文档改动均限定在 admin/ 目录内（admin/backend、admin/frontend、admin/docs、admin/deploy）
- 主项目（src/、docs/ 等 admin 之外）一律不碰——拆分对主项目零引用（已确认），主项目侧无任何改动
- 超出 admin/ 的操作仅限部署/运维行为（数据库命令、/etc/grape/admin.env），非代码改动

依赖链：阶段 1（数据库）→ 阶段 2（代码）→ 阶段 3（部署）→ 阶段 4（验证）→ 阶段 5（收尾，稳定期后）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
阶段 1：数据库迁移（停服窗口，一次性）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

T1.1 停服
  执行：systemctl stop admin-backend
  验收：curl 127.0.0.1:8081 无响应；记录停服时间

T1.2 导出旧库 9 张 admin 表（结构+数据）
  命令：mysqldump -h127.0.0.1 -uroot -p*** grape_test_db \
        admin_users admin_roles admin_permission_menus admin_role_menu_rel \
        admin_user_role_rel admin_audit_log admin_ai_history admin_ai_template \
        admin_agent_alert > /opt/grape/data/backups/admin_tables_dump_$(date +%Y%m%d_%H%M%S).sql
  验收：dump 文件生成，9 张表齐全，含 CREATE TABLE + INSERT 数据

T1.3 建新库并导入
  命令：mysql -uroot -p*** -e "CREATE DATABASE grape_admin_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
        mysql -uroot -p*** grape_admin_db < dump文件
  验收：grape_admin_db 存在，SHOW TABLES = 9 张

T1.4 双库核对
  核对项：
  a) 行数：逐表 SELECT COUNT(*) 对比双库一致
  b) 自增：information_schema.tables 的 AUTO_INCREMENT 对齐
  c) 结构 diff：information_schema.columns 列名/类型/默认值/索引一致
  验收：三项全部一致；不一致时以旧库实际结构为准补齐

T1.5 旧库业务表完好性确认
  执行：SELECT COUNT(*) FROM grape_test_db.agent_runtimes/agent_tasks/runtime_credentials（抽查）
  验收：grape 业务表数据未被误动（dump 只读操作，应天然满足，确认即可）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
阶段 2：代码改造（feat/admin-console，核心）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

T2.1 新增 config/DataSourceConfig.java（设计文档 §4.2）
  内容：
  a) adminDS：@Primary，HikariDataSource 手动创建，绑 ADMIN_DB_URL/USER/PASSWORD
     （P16：手动创建，配置前缀如 spring.datasource.admin 或直接 @Value 绑定）
  b) grapeDS：HikariDataSource 手动创建，绑 GRAPE_DB_URL/USER/PASSWORD
  c) adminEmf：扫描 entity（@EntityScan excludeFilters 排除 entity.readonly 的 6 个 Ro 实体，P4）
  d) grapeEmf：扫描 entity.readonly
  e) @EnableJpaRepositories 两段（P13）：
     - basePackages=repository → entityManagerFactoryRef=adminEmf, transactionManagerRef=adminTxManager
     - basePackages=repository.readonly → entityManagerFactoryRef=grapeEmf, transactionManagerRef=grapeTxManager
  f) adminJdbcTemplate + grapeJdbcTemplate（均不标 @Primary，P3）
  g) adminTxManager（@Primary）+ grapeTxManager
  验收：mvn -B compile 通过

T2.2 application.yml 配置（设计文档 §4.3/4.4）
  内容：
  a) 新增 grape.datasource.* 段（url/user/password，${GRAPE_DB_URL:默认dev} 等）
  b) Flyway 基线参数：baseline-on-migrate=true, baseline-version=16（P11）
     （baseline-on-migrate 需在启用 Flyway 后生效；SPRING_FLYWAY_ENABLED 由 admin.env 控制）
  c) 现有 admin 数据源配置指向 ADMIN_DB_* 不变
  验收：配置项与 DataSourceConfig 绑定一致

T2.3 JdbcTemplate 注入点改造（设计文档 §2.2 清单，7 个类 ~45 处）
  a) 纯 grape 类（5 个）：构造参数加 @Qualifier("grapeJdbcTemplate")
     - RepoService / RuntimeCleanupService / RuntimeCredentialService / SkillService / TemplatesService
  b) 混用类（2 个）：注入两个 JdbcTemplate，按表归属拆分 SQL
     - StatsController：admin_users 2 处用 adminJdbcTemplate；其余 grape 表用 grapeJdbcTemplate
     - AgentService：admin_agent_alert 2 处（161/242）用 adminJdbcTemplate；其余用 grapeJdbcTemplate
  验收：mvn -B compile 通过（漏改 @Qualifier 必然 NoUniqueBeanDefinitionException = fail-fast 拦截）

T2.4 本地冒烟启动
  执行：本地临时起服务（或 CI 环境），确认：
  a) 启动成功（双 DataSource 初始化、Flyway baseline 完成）
  b) 日志无异常（Hikari 双池连接成功，P15）
  c) curl /api/admin/health 返回 200
  验收：三项通过；注意本地环境需能连 127.0.0.1（或临时改配置指向可连库）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
阶段 3：部署（dev）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

T3.1 修改 /etc/grape/admin.env（设计文档 §4.4）
  变更：
  a) ADMIN_DB_URL → jdbc:mysql://127.0.0.1:3306/grape_admin_db?...
  b) 新增 GRAPE_DB_URL/USER/PASSWORD → grape_test_db
  c) SPRING_FLYWAY_ENABLED=true（P14，配合基线方式）
  验收：环境变量核对无误（密码脱敏核对）

T3.2 部署
  执行：cd /opt/grape/admin/deploy && bash deploy.sh
  验收：deploy.sh 5 步全部通过，健康检查 200

T3.3 启动确认
  执行：journalctl -u admin-backend 或 /opt/grape-admin/logs/backend.log 检查
  验收：
  a) Flyway：Creating Schema History table / Successfully validated（baseline 16）
  b) 双数据源：两个 HikariPool 启动成功
  c) 无异常堆栈

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
阶段 4：验证（设计文档 §8）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

T4.1 管理台全功能回归
  覆盖：登录/RBAC/菜单渲染、运行时（设备/引擎/凭据签发/吊销/清理）、技能（上传/转官方/删除）、AI 助手、统计页、审计日志
  验收：功能与拆分前一致

T4.2 落库验证（双库分流正确性）
  执行：签发一个凭据后：
  a) grape_admin_db.admin_audit_log 出现新审计记录（admin 库生效）
  b) grape_test_db.runtime_credentials 出现新凭据（grape 库生效）
  验收：a、b 同时满足 = 双数据源分流正确

T4.3 反向验证（证明 admin 流量已切走）
  执行：grape_test_db 临时 DROP TABLE admin_users → 管理台登录/用户管理正常 → 恢复表
  验收：管理台功能不受影响；恢复后数据完好

T4.4 重启验证
  执行：systemctl restart admin-backend
  验收：种子初始化（admin 用户/角色）与 RBAC 菜单加载正常（读 grape_admin_db）

T4.5 主项目影响验证
  执行：抽查 FastAPI 主服务（grape-dev）关键接口正常（登录/仓库/任务）
  验收：主服务不受影响（主项目零引用 admin 表，应天然满足）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
阶段 5：收尾（稳定期后，待用户确认执行）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

T5.1 稳定期观察（建议 1 周）：无回滚需求后，评估清理 grape_test_db 的 9 张 admin 表
  前置：需用户明确确认；清理前先 DROP 到备份库（保留 dump 文件）
  验收：清理后管理台功能仍正常

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
git 提交策略（Grape 开发流程硬性约定）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 所有改动在 feat/admin-console 分支
- 提交粒度：设计文档/数据库脚本（阶段1相关脚本）一个提交；DataSourceConfig+yml 一个提交；
  JdbcTemplate 改造一个提交；部署配置清单一个提交
- 提交前 git branch --show-current 确认分支；合并 dev 前先给影响分析
