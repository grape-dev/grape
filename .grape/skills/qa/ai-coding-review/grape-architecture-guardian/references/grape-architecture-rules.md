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

**规则**：`src/worker_package.py` 的 MANIFEST 列表必须包含：
- `src/auth/`（整个目录）
- `src/token_service.py`
- `src/token_resolver.py`
- `src/api/time_utils.py`
- `src/api/event_store.py`
- `src/api/session_store.py`
- `src/api/knowledge.py`
- `src/api/routing.py`
- `src/agent/`（整个目录）
- `src/runtime/`（整个目录）

**背景**：曾因漏列 `time_utils` 导致 worker 包缺文件启动即崩（ModuleNotFoundError，
2026-08-17）。漏列 `token_service.py` / `token_resolver.py` 导致 worker token 校验失败；
漏列 `src/auth/` 导致组织隔离失效。

**禁止**：不要整目录引入 `src/api/main.py`、`src/api/run.py` 等服务端专用文件。

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

## 8. 7 Gate 适用矩阵与评论首行格式

### 8.1 Gate 适用矩阵

| Gate | 名称 | 适用仓库 |
|-----|------|---------|
| 1 | Grape 核心架构约束 | 所有仓库 |
| 2 | 组织隔离与安全 | grape 主仓必查，其他按需 |
| 3 | 前端规范 | grape-admin 前端、grape-website 必查 |
| 4 | Git 与提交规范 | 所有仓库 |
| 5 | 测试规范 | grape / grape-cli / grape-admin / grape-test 必查 |
| 6 | 数值证据 | 所有仓库 |
| 7 | 架构适配性 | grape 主仓必查，其他按需 |

先运行 `scripts/repo_inventory.py` 确认技术栈，再按上表选择适用 Gate；不适用的 Gate 在报告中标注「不适用」。

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

### grape 主仓
```
src/
  api/          # FastAPI 路由 + 事件流水线
    routes/     # API 端点
    static/     # 前端静态文件
  runtime/      # Agent 运行时（claim/schedule/execute）
  auth/         # 认证 + 组织隔离
  agent/        # Agent 后端类型（hermes/opencode/codex/codearts）
  admin/        # 管理接口
  im/           # IM 集成
  observability/ # 可观测性
  storage/      # 存储后端（local/OBS）
  user/         # 用户管理
  vcs/          # VCS 适配
  config.py     # 全局配置
  lock.py       # 分布式锁
  session_manager.py  # DB 连接管理
  worker_package.py   # Worker 打包
tests/
  conftest.py   # 测试隔离（强制 SQLite + 清除 MySQL 参数）
```

### grape-admin
```
backend/  # Spring Boot (Java 11)
frontend/ # Vue3 + Vite
```

### grape-cli
```
src/
  cli.py       # CLI 入口
  commands/    # 命令实现
  contracts.py # 契约定义
  client.py    # API 客户端
```
