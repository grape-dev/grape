# Grape 架构规则完整清单

> 来源：grape-dev 组织 8 个项目的生产实践、事故复盘和工程约定。
> 每条规则附有历史背景，用于审查时判断偏离的严重性。

## 1. 部署约束

### 1.1 uvicorn 单 worker

**规则**：服务端必须以 `--workers 1`（或不带 `--workers`）启动。

**背景**：多 worker 会导致 `db_lock`（threading.RLock）跨进程无效，多个进程同时写同一
数据库行导致数据损坏。定时任务（autopilot scheduler、observability rollup、
active user tracker）会在每个 worker 中重复启动，产生重复事件和锁竞争。

**检查方式**：搜索启动脚本、部署配置、文档中的 `--workers` 参数。

### 1.2 Worker MANIFEST 完整性

**规则**：`src/worker_package.py` 的 MANIFEST 必须包含 `src/auth/`、`src/token_service.py`、`src/token_resolver.py`、`src/api/time_utils.py`、`src/api/event_store.py`、`src/api/session_store.py`、`src/api/knowledge.py`、`src/api/routing.py`、`src/agent/`、`src/runtime/`。

**背景**：曾因漏列 `time_utils` 启动即崩（2026-08-17）；漏列 token 服务/解析器导致校验失败；漏列 `src/auth/` 组织隔离失效。**禁止**整目录引入 `src/api/main.py`、`src/api/run.py` 等服务端专用文件。

### 1.3 分布式锁

**规则**：跨进程互斥必须走 `src/lock.py`（distributed_locks 表 + db_lock），不能裸用
`threading.Lock` 处理跨进程共享数据。

**背景**：`db_lock`（threading.RLock）只保证单进程内线程安全；跨进程互斥靠
`distributed_locks` 表的 PRIMARY KEY 约束。锁 TTL=60 秒，过期自动释放。

## 2. 组织隔离

### 2.1 org_id 对齐

**规则**：
- `change_credential_scope` 必须用 `require_org_member` 获取 `org_id`
- `user scope` 切换 `org_id` 必须对齐操作者组织
- `_build_runtime_devices` 必须传 `crepo_key`
- repo scope 设备只在创建它的组织可见

**背景**：曾出现跨组织数据泄露事故。组织隔离是 Grape 多租户安全的核心。

### 2.2 场景 org_id 一致性

**规则**：场景的 `org_id` 必须与仓库的 `org_id` 一致。

**背景**：架构守护场景创建时 org_id=0（个人空间），但仓库归属组织（org_id 非 0），导致任务
以 org_id=0 入队，远端 worker scope=user 匹配不到，任务永远 queued（2026-10-22 事故）。

### 2.3 任务 user_id 正确性

**规则**：事件触发的任务必须正确设置 `user_id`（仓库 owner 的 grape_accounts.id），
不能为 0。

**背景**：PR 事件触发任务时 user_id=0，导致 scope=user 的 worker 按 `t.user_id=?`
过滤时匹配不到，任务无法被认领。

## 3. 测试隔离

### 3.1 conftest.py 强制覆盖

**规则**：
- `os.environ["DEVOPS_DB_BACKEND"] = "sqlite"` — 必须用 `os.environ[]=` 强制覆盖
- 不能用 `os.environ.setdefault()`（.env 已设置 mysql 时 setdefault 无效）
- 必须清除 `DEVOPS_DB_HOST`、`DEVOPS_DB_PORT`、`DEVOPS_DB_USER`、
  `DEVOPS_DB_PASSWORD`、`DEVOPS_DB_NAME`（防止 .env 指向生产 MySQL 时测试连生产库）

**背景**：曾发生测试连接生产 MySQL 并清空数据的事故。

### 3.2 测试 DELETE 安全

**规则**：
- DELETE 语句必须带 WHERE 子句
- 只删测试数据（`test_%`、`gitcode:org/%` 等）
- f-string 动态 `DELETE FROM {table}` 必须有表名白名单校验
- 53+ 文件已修复此问题，不能破坏防护

**背景**：动态表名 DELETE 曾导致误删非测试数据。

## 4. 前端规范

### 4.1 版本号 bump

**规则**：前端 JS 文件改动后，必须 bump `index.html` 中 `?v=N` 版本号。

**背景**：浏览器缓存旧版 JS 导致功能异常且难以排查。已知 app.js v170、issues.js v113、
runtimes.js v59 等版本号。

### 4.2 showToast 闭包

**规则**：`showToast` 定义在 `app.js` 闭包内。在 `app.js` 之前加载的 JS 文件必须用
`app.showToast()`，不能直接调用 `showToast()`。

### 4.3 _grapeConfirm

**规则**：`_grapeConfirm` 只接受纯文本 message（260px 宽度），不支持 HTML。不能使用
原生 `confirm()`。表单弹窗必须自定义 DOM。

### 4.4 API 路径约定

**规则**：`api.js` 中 `BASE='/api'`，API 调用路径不含 `/api/` 前缀。

### 4.5 搜索双触发

**规则**：搜索使用 `onkeyup` + `onchange` 双触发。

### 4.6 组织名称多语言

**规则**：个人组织名称按语言区分：
- zh: "XX的个人空间"
- en: "XX's Workspace"

### 4.7 苹果设计风格

**规则**：前端 UI 遵循苹果设计风格（0.5px border、rounded-lg、SF Pro 字体）。

## 5. Git 规范

### 5.1 提交信息

**规则**：
- 全英文
- Conventional Commits 格式：`type(scope): subject`
- 保留 `(Closes #N)` / `(Issue #N)` 关联后缀

### 5.2 分支

**规则**：主工作基线分支以任务注入的 `$GRAPE_WORK_BRANCH` 为准（如 `weekly-xx` 迭代基线）；建 PR 的 base 一律用 `$GRAPE_WORK_BRANCH`，不硬编码。

## 6. 文档与安全规范

### 6.1 QA 合规

**规则**：文档中不出现：
- 内网 IP
- 内部 DB ID
- 绝对路径
- 部署拓扑细节
- 公司名或个人名（用通用术语）

**注意**：代码注释/变量名不受此限制。

### 6.2 凭据过滤

**规则**：
- `configs/credential-filter.json` 定义敏感子串（credential/token/secret/password/private_key/access_key）和扩展名（.pem/.key/.env/.p12/.pfx）
- `scripts/check-secrets.py` 做提交前密钥扫描
- `scripts/check-doc-redaction.py` 做提交前文档脱敏扫描
- 运行时密钥仅通过环境变量注入，绝不提交

### 6.3 覆盖率门槛

**规则**：grape 主仓覆盖率 ≥70%（`.coveragerc` fail_under=70，当前 71.08%）。PR 不应降低覆盖率。

### 6.4 双 DB 后端

**规则**：`src/mysql_compat.py` 模拟 sqlite3 接口翻译为 MySQL，26 个模块零改动。开关优先级：env > configs/db-backend.json > 默认 sqlite。PR 不能破坏 MySQL/SQLite 双后端兼容性。

## 7. 已知配置数值注册表

完整注册表见 `references/known-config-values.md`（每个数值附来源/文件/偏离判定）。注册表数值不重复质疑，只验证未偏离；不在注册表中的数值走数值证据门禁五源体系（见 SKILL.md 规则 1）。

### 8.1 Gate 适用矩阵

|| Gate | 名称 | 适用仓库 | 2.0 规则类别 |
|-----|------|---------|-------------|
| 1 | Grape 核心架构约束 | 所有仓库 | B1（SQL 双模式） |
| 2 | 组织隔离与安全 | grape 主仓必查，其他按需 | C1-C3（可信约束） |
| 3 | 前端规范 | grape-admin 前端、grape-website 必查 | D1-D2（UI 架构约束） |
| 4 | Git 与提交规范 | 所有仓库 | — |
| 5 | 测试规范 | grape / grape-cli / grape-admin / grape-test 必查 | — |
| 6 | 数值证据 | 所有仓库 | B2（魔鬼数字，脚本复用） |
| 7 | 架构适配性 | grape 主仓必查，其他按需 | A1-A9（设计架构约束） |

先运行 `scripts/repo_inventory.py` 确认技术栈，再按上表选择适用 Gate 与 2.0 规则类别；不适用的在报告中标注「不适用」。

### 8.2 评论首行格式（强制）

```
[QA Agent] PR #{N} grape架构守护审查报告（head sha: {sha8}｜模式 {X}（{基线描述}）｜执行位置 {worker/build}｜{重启状态}，被中断他人任务 {N} 个）
```

| 字段 | 取值 | 示例 |
|------|------|------|
| `{N}` | PR 编号 | 598 |
| `{sha8}` | head commit SHA 前 8 位 | 059d76e9 |
| 模式 `{X}` | A=生产基线（main）/ B=开发基线（dev）/ C=迭代基线（weekly-xx）/ D=功能分支（feat-xx） | C |
| `{基线描述}` | base 分支名 + 基线类型 | weekly-08 迭代基线 |
| `{worker/build}` | 执行此审查的 worker 名称或 build 编号 | build-0007 |
| `{重启状态}` | 是否执行了服务重启 | 未执行重启 / 已执行重启 |
| 被中断他人任务 | 审查过程中是否中断了其他正在执行的任务及其数量 | 0 个 |

**特殊情况**：复审在首行末尾追加 `｜复审`；A2A 触发追加 `｜A2A 触发`；上下文不足降级执行追加 `｜降级执行`。首行之后空一行，再按 `templates/pr-review-report.md` 输出报告正文。

## 9. 项目目录结构约定

grape 主仓 `src/`（api/runtime/auth/agent/admin/im/observability/storage/user/vcs + config/lock/session_manager/worker_package）、`tests/conftest.py`；grape-admin `backend/`(Spring Boot) + `frontend/`(Vue3)；grape-cli `src/`（cli/commands/contracts/client）。

## 10. 架构守护规则 2.0（4 级判定体系 + 16 条规则）

> Issue #467 升级：与既有 7 Gate（§1-§7）双轨融合判定。本节规则适用于「新增/变更」相关改动；存量遗留（白名单/基线已存在）不溯及。

### 10.1 4 级判定体系与合入建议

| 级别 | 定义 | 合入建议 |
|------|------|----------|
| **BLOCKED** | 必须遵循的强制约束，不遵从影响系统功能 | ❌ 不建议合入，打回整改 |
| **Critical** | 关键规则，不遵从影响持续维护 | ❌ 不建议合入，打回整改 |
| **Medium** | 一般影响 | ⚠️ 人工判断是否合入（置 pending-review 审核点） |
| **Informational** | 提示类观察 | ✅ 不影响合入，记录备查 |

审查结论按 4 级别归类统计（如 `0 BLOCKED / N Critical / N Medium / N Informational`），总体结论映射见 SKILL.md 硬约束。

### 10.2 设计架构约束（A 类，Gate 7 架构适配性）

- **A1【Critical】新增开放 API 遵循 REST 规范且与现有风格一致**：资源命名/HTTP 方法/状态码与 `src/api/routes/` 既有端点风格对照。证据：路由定义文件+行号。
- **A2【Medium】新增开放 API 需增加对应 grape cli 开放**：新 API 端点在 grape-cli 契约/命令侧有对应开放能力（grape-cli 仓库对照）。证据：CLI 契约或命令缺失清单。
- **A3【Critical】新增接口性能不低于 50tps**：数值证据类——需压测数据（`measured`）或性能设计说明（`verified: doc`），无证据标 `INSUFFICIENT DATA TO VERIFY`，禁止虚假保证。
- **A4【Critical】新增接口时延 <200ms**：同 A3 数值证据门禁（五源体系）。
- **A5【BLOCKED】用户文件上传和共享需支持 OBS 存储**：新增用户文件功能须走 `src/storage/`（local/OBS 双后端），不得绕过存储抽象直写本地盘。证据：存储调用点文件+行号。
- **A6【Critical】新增数据库表名规则**：主控系统表 `grape_t_xxx`、运营管理台表 `grape_admin_t_xxx`（xxx 英文语义）；存量表名白名单见 `scripts/grape_constraint_check.py`。证据：CREATE TABLE 语句位置。
- **A7【Medium】大表设计需支持好分区**：预估大表（消息/日志/事件类）需分区或归档策略说明。证据：表设计文档或 DDL。
- **A8【Medium】新增大型特性优先模块化设计和接入**：新特性按 `src/` 模块边界独立成模块接入，不在既有模块内膨胀。证据：变更文件清单的模块分布。
- **A9【BLOCKED】新增特性鉴权机制符合 grape 权限机制**：走 `src/auth/`（require_org_member 等），不得自建鉴权旁路。证据：鉴权调用点文件+行号。

### 10.3 代码架构约束（B 类，Gate 1/6）

- **B1【BLOCKED】新增 SQL 需同时支持 sqlite 和 mysql 两种模式**：不得使用 `src/mysql_compat.py` 翻译层不覆盖的单边方言（如 `ON CONFLICT` 仅 sqlite / `ON DUPLICATE KEY` 仅 mysql 未经翻译层）。证据：SQL 语句文件+行号。
- **B2【Critical】代码不允许魔鬼数字**：复用 `scripts/magic_number_audit.py` 扫描（Gate 6 同源），无来源配置数值按数值证据门禁五源体系处理。证据：脚本输出+数值定位。

### 10.4 可信约束（C 类，Gate 2）

- **C1【Medium】新增用户隐私数据存储加密**：不可逆（口令类）用 PBKDF2，可逆用 AES-GCM-256（对齐 `src/auth/credential_crypto.py`）。证据：加密实现文件+行号；明文存储即不符合。
- **C2【Critical】外部接口调用需记录接口日志**：新增外部 HTTP/SDK 调用须有请求日志（对齐 `api_latency_log` / 观测机制）。证据：调用点与日志记录代码。
- **C3【Critical】系统非查询类操作需记录审计日志**：新增写操作（创建/更新/删除）须落 `audit_logs` 或等价审计机制。证据：操作路由与审计写入点。

### 10.5 UI 架构约束（D 类，Gate 3）

- **D1【BLOCKED】需支持国际化（必须中英双语）**：前端新增用户可见文案走 `locales/zh|en` 语言包 key 化（`i18n.js` 机制），不得硬编码文案。证据：JS 变更行 + zh/en key 对照（`scripts/check-i18n-keys.py` 机制）。
- **D2【BLOCKED】需支持两种风格（Grape Default 和 Liquid Glass）**：前端新增 UI 元素经 `data-theme` 令牌（`tokens.css`/`apple-theme.css`）适配双主题，不得硬编码单主题样式。证据：CSS/HTML 变更行。

### 10.6 证据要求（2.0 规则通用）

- 每条「不符合」给出证据定位（文件+行号/commit sha），沿用 `templates/finding.md` 契约。
- A3/A4/A5 等数值/证据类规则一律走数值证据门禁标签：`verified: code/config/doc` / `measured` / `INSUFFICIENT DATA`，禁止虚假保证。
- 静态可检项（A6/B1/B2/D1）由 `scripts/grape_constraint_check.py` 扩展检查（输出为候选，需复核）。
- Medium 级不遵从 → 报告标注「人工判断是否合入」并按 runbook Step 4b 置 pending-review。
