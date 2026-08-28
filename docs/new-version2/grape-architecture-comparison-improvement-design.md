# Grape 架构对比与持续改进设计文档

> 状态：草案 | 日期：2026-08-28 | 分支：wise  
> 对标项目：DeepSeek Harness (`/home/grape/deepseek-harness/`)

---

## 一、项目概况对比

| 维度 | Grape | DeepSeek Harness |
|------|-------|-----------------|
| **定位** | Multi-Agent DevOps 工作流平台 | 插件化 Agent 框架（harness） |
| **后端语言** | Python 3.12（95 文件，32749 行） | TypeScript（1412 源文件） |
| **前端** | 17 个 JS 文件（6893 行），无构建 | TypeScript + Vite，有构建流程 |
| **测试** | 104 文件（大量预先存在的失败） | 689 测试文件，5 层体系，100% per-file 覆盖率门禁 |
| **模块化** | 单体 monolith | 227 个 npm 包（pnpm workspace） |
| **文档** | 137 个 .md | 219 个 .md + 1146 个双语 .zh.md |
| **工程工具链** | 无 CI / 无 lint / 无 pre-commit | 41 个 verify 脚本 / lefthook / oxlint / jscpd / knip |
| **事后复盘** | 无 | 4 篇 postmortem（双语） |
| **Agent Notes** | 无 | 1498 个 |
| **国际化** | 无 | 全量双语（中英文自动同步） |
| **Docker** | Dockerfile + docker-compose.yml | 无（通过 pnpm 管理依赖） |
| **CI** | 无 | GitLab CI（Python SDK 发布） |

---

## 二、架构设计对比

### 2.1 DeepSeek Harness 架构亮点

#### 全插件架构（Cordis 框架）

所有功能都是插件——模型适配器、工具注册表、会话日志、agent loop 本身——可从配置替换。无特权核心，扩展=挂载插件。

```
Profile (命名组合)
  └─ Bundle (分发格式，patchable)
       └─ Plugin (贡献 services + events + effects)
            └─ ctx.key (共享上下文)
```

- `dsh-base` 是每个 profile 的第一层（模型适配器、工具、持久化、sandbox、策略）
- `dsh-web-app` 添加浏览器应用；`dsh-headless` 添加一次性运行器
- 每层可 patch 上层：`dsh --profile web --dump-config` 查看实际插件树

#### 三层事件体系

| 事件类型 | 持久化 | 用途 | 示例 |
|----------|--------|------|------|
| Session events | 是（追加日志） | 必须在 reload 后存活的事实 | user/message, assistant/message, tool/result |
| Agent events | 否（实时） | 观察/拦截 in-flight 工作 | agent/pre-step, agent/turn-stopping |
| Capability events | 否（策略） | 挂载策略/适配器到 seam | fs/*, tools/*, telemetry/* |

#### 5 层测试体系

| 层级 | 命令 | 特点 |
|------|------|------|
| Unit | `pnpm run test` | vitest，优先边界/错误路径/并发竞争 |
| Coverage gate | `pnpm run test:coverage` | per-file 100% 门禁 |
| Real-API e2e | `pnpm run test:e2e` | 无 key 自动跳过 |
| Snapshot | `pnpm run test:snapshot` | 无 key 的期望输出比对 |
| Web browser snapshot | `pnpm run test:web` | Chromium 浏览器截图比对 |

测试哲学：
- 优先用真实实现而非 mock
- 验证世界而非自报告（重新执行命令/重读文件，而非检查 agent 输出关键词）
- 每个注册有 HMR 安全测试（dispose 后验证清理）
- e2e 测试拥有自己的资源（在 test 中创建 harness，afterEach 中 dispose）

#### 防御性编程规范

7 类已上线 bug 的防范规则：

1. **正交结果独立报告**——一个结果可以是多件事（超时 AND exit 0），各自独立暴露
2. **公共契约双向遵守**——实现侧归一化后通过公共 API 返回
3. **异步状态 ≠ 同步状态**——background job 完成与 turn boundary 竞争
4. **Dispose 必须达到静止**——不只是请求 kill，要 await children 退出
5. **回调异常在 dispatcher 中隔离**——一个坏 subscriber 不应破坏核心生命周期
6. **不信任输出接触环境**——子进程环境清洗 *KEY*/*SECRET*/*TOKEN*/*PASSWORD*
7. **Unlink link-shaped paths**——符号链接路径安全处理

### 2.2 Grape 当前架构

#### 单体 FastAPI

核心文件行数：

| 文件 | 行数 | 职责数 | 问题 |
|------|------|--------|------|
| `src/user/db.py` | 1888 | 用户/仓库/凭据/Skill/Meta 管理 | 5+ 职责混合 |
| `src/api/db_setup.py` | 1684 | 全部 DDL + 迁移 | 20+ 表定义在一个文件 |
| `src/api/event_pipeline.py` | 1637 | 路由/skill 查找/A2A 派发/agent 执行/结果评估 | 5+ 职责混合 |
| `src/runtime/service.py` | 1372 | 任务管理/引擎注册/worker 通信 | 4+ 职责 |
| `src/im/feishu_async.py` | 1281 | 飞书 IM 集成 | 单文件过大 |
| `src/runtime/agent.py` | 1183 | agent 执行/引擎管理/超时/清理 | 4+ 职责 |

#### 模块间耦合

```
event_pipeline.py
  ├── import session_store (get_issue_stage, set_issue_stage, get_issue_scene...)
  ├── import pipeline_config (get_node_by_tag, get_pipeline_config)
  ├── import prompt_builder (build_devops_agent_prompt)
  ├── import event_store (append_event, update_event_complete, evaluate_agent_output)
  ├── import engine_select (pick_engine, engine_name)
  ├── import runtime.service (enqueue_task, runtime_execution_kind)
  └── import session_manager (db, db_lock)  ← 直接使用全局 DB 连接
```

#### 已有的架构亮点

Grape 也有一些值得肯定的设计：

1. **Issue 级场景识别**——VOD 根据项目知识基线 + 成员文档 + 需求特征判定开发场景，后续节点按场景的流水线 skill 执行
2. **自定义场景支持**——成员上传 `pipelines-user.json` 定义自定义场景，服务端从 member 文件动态加载
3. **双数据库兼容**——`mysql_compat.py` 通过 PRAGMA 拦截转译 SQL，支持 SQLite→MySQL 平滑迁移
4. **Plugin 模式**——`GRAPE_PIPELINE_MODE=plugin` 通过 `pipelines.json` 配置流水线节点
5. **事件管道雏形**——`event_bus.py`（187 行）+ `pipeline_executor.py`（239 行）已有发布/订阅框架
6. **可观测性**——dev 分支引入了 `src/observability/`（prometheus_client + 结构化 JSON 日志）
7. **JWT 认证**——dev 分支引入了 `jwt_utils.py`（121 行），支持 token 认证
8. **Docker 部署**——Dockerfile + docker-compose.yml，含 healthcheck
9. **雷达动画**——Pipeline 运行中节点卡片有雷达扫描效果（细节体验）

---

## 三、Grape 需要持续优化和改进的清单

### P0 — 紧急（影响可用性）

#### 1. 修复测试套件

**现状**：104 个测试文件，但大量预先存在的失败：
- `test_event_conversation`：`jwt_utils` import 错误（dev 引入 JWT 但 setup_module 未同步）
- `test_agent_backend`：`agent_key` 列不存在（schema 不匹配）
- `test_agent_executor`：CodeArts runtime 参数构建失败

**目标**：全部测试通过（0 failed），为 CI 铺路。

**方案**：
- 修复 `test_event_conversation.py` 的 `setup_module`：使用新的 JWT token 生成方式
- 修复 `test_agent_backend.py`：更新 schema 或跳过已废弃的测试
- 逐个排查 `test_agent_executor.py` 的 CodeArts 相关失败

#### 2. 引入 CI Pipeline

**现状**：无 `.github/workflows/`，无 `.gitlab-ci.yml`。

**方案**：创建 `.github/workflows/ci.yml`：

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: python -m pytest tests/ -q --timeout=30
      - run: python -c "import py_compile; import glob; [py_compile.compile(f, doraise=True) for f in glob.glob('src/**/*.py', recursive=True)]"
```

#### 3. 拆分超大文件

**`event_pipeline.py`（1637 行）拆分方案**：

```
src/api/
  event_pipeline.py        → 保留 run_pipeline() 入口（~200 行）
  event_router.py           → _resolve_issue_stage_agent() 路由判定
  event_skill_resolver.py   → resolve_skill_from_pipeline() skill 查找
  event_a2a.py              → A2A 派发逻辑
  event_executor.py         → _execute_agent_inner() agent 执行
  event_evaluator.py        → 结果评估 + 状态更新
```

**`user/db.py`（1888 行）拆分方案**：

```
src/user/
  db.py                     → 保留 init_db() + 连接管理
  user_repo.py              → 用户 CRUD + token 管理
  managed_repo.py           → 仓库托管 + scene 管理
  skill_db.py               → Skill CRUD
  meta_store.py             → meta 表 KV 存储
```

---

### P1 — 高优先级（影响可维护性）

#### 4. 引入 Python Lint 和类型检查

**现状**：无任何 lint 配置。

**方案**：

```bash
# 安装
pip install ruff mypy

# ruff 配置（pyproject.toml）
[tool.ruff]
line-length = 120
target-version = "py312"
[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM"]
ignore = ["E501"]  # 先忽略行长度

# mypy 配置（逐步收紧）
[tool.mypy]
python_version = "3.12"
ignore_missing_imports = true
check_untyped_defs = false  # 第一阶段：只检查已标注的函数
```

逐步推进策略：先在 `src/api/` 新代码上启用，每次 PR 不引入新的 lint 违规。

#### 5. 前端工程化

**现状**：17 个 JS 文件通过 `<script>` 标签引入，手动管理 `?v=N` 版本号。

**方案**：引入 Vite + TypeScript

```
frontend/
  src/
    api.ts          → API 调用封装（替代 api.js）
    app.ts          → 应用入口
    pages/
      pipeline.ts   → Pipeline 页面
      events.ts     → Event Detail 页面
      tasks.ts      → Task Center 页面
      ...
  vite.config.ts
  tsconfig.json
```

收益：
- 版本号管理自动化（Vite content hash）
- 类型安全（API 返回结构有 TypeScript interface 定义）
- 代码压缩 + Tree shaking
- 模块化 import/export（消除全局函数污染）

#### 6. DB 层抽象

**现状**：`db.cursor()` + 手写 SQL 散落在 10+ 文件中；SQLite/MySQL 双兼容靠 `mysql_compat.py`（644 行 PRAGMA 拦截）。

**方案**：引入 Repository 模式

```python
# src/repositories/event_repository.py
class EventRepository:
    def __init__(self, db, db_lock):
        self._db = db
        self._lock = db_lock

    def append_event(self, repo_key, event_type, ...) -> int:
        with self._lock:
            c = self._db.cursor()
            c.execute("INSERT INTO events ...", ...)
            self._db.commit()
            return c.lastrowid

    def get_event(self, seq: int) -> dict | None:
        ...
```

长期目标：迁移到 SQLAlchemy ORM，彻底消除 SQLite/MySQL 双兼容的 PRAGMA 拦截层。

#### 7. 覆盖率门禁

**方案**：

```bash
pip install pytest-cov

# pytest.ini 追加
[pytest]
addopts = --cov=src --cov-report=term-missing --cov-fail-under=40
```

先覆盖核心模块（目标 60%+）：
- `event_store.py`（已有 7 个测试）
- `pipeline_config.py`
- `session_store.py`
- `event_pipeline.py` 的 `resolve_skill_from_pipeline`

Harness 的理念借鉴："未覆盖的行往往是死代码，不是缺失的测试"——先清理死代码再补测试。

#### 8. 配置管理统一

**现状**：配置散落在 7+ 处：
- `configs/*.json`（14 个 JSON 配置文件）
- 环境变量（`GRAPE_PIPELINE_MODE`、`RUNTIME_TASK_TIMEOUT` 等）
- DB `meta` 表（`pipeline_mode`、`onboarded:{repo_key}` 等）
- `runtime.env`（worker 进程环境变量）
- `configs/db-backend.json`（数据库连接配置，含明文密码）

**方案**：使用 Pydantic Settings

```python
# src/config.py（重构）
from pydantic_settings import BaseSettings

class GrapeSettings(BaseSettings):
    # 数据库
    db_backend: str = "sqlite"  # sqlite | mysql
    mysql_host: str = ""
    mysql_port: int = 3306
    # 运行时
    pipeline_mode: str = "legacy"  # legacy | plugin
    runtime_task_timeout: float = 1200
    # 服务
    server_port: int = 38080
    server_host: str = "0.0.0.0"

    class Config:
        env_prefix = "GRAPE_"
        env_file = ".env"
```

---

### P2 — 中优先级（影响可扩展性）

#### 9. 事件总线完善

**现状**：`event_bus.py`（187 行）已有发布/订阅框架，但 `event_pipeline.py` 的大部分路由仍走直接函数调用。

**方案**：参考 Harness 的三层事件体系，逐步迁移：

| 事件类型 | Grape 对应 | 持久化 | 示例 |
|----------|-----------|--------|------|
| 持久化事件 | events 表 + issue_state | 是 | issue_opened, a2a_event, pull_request_merged |
| 实时事件 | task_messages + SSE | 否 | tool_use, reasoning, text, step_start |
| 能力事件 | 无（新增） | 否 | engine_select/*, skill_resolve/*, prompt_build/* |

迁移步骤：
1. 将 `run_pipeline()` 中的路由判定逻辑发布为 `pipeline/route` 事件
2. 将 skill 查找逻辑发布为 `pipeline/skill_resolve` 事件
3. 将 agent 执行结果评估发布为 `pipeline/evaluate` 事件
4. 订阅者可以在不修改 `event_pipeline.py` 的情况下添加新的路由规则或 skill 解析逻辑

#### 10. API 输入校验

**现状**：很多端点直接 `body.get()` 无类型检查。

**方案**：定义 Pydantic RequestModel

```python
# 现状
@router.post("/api/a2a")
async def a2a_endpoint(request: Request):
    body = await request.json()
    agent_name = body.get("agent", "")
    if not agent_name:
        raise HTTPException(400, ...)

# 改进
class A2ARequest(BaseModel):
    agent: str  # 必填，自动校验非空
    payload: A2APayload  # 嵌套模型

@router.post("/api/a2a")
async def a2a_endpoint(body: A2ARequest, user: dict = Depends(get_current_user)):
    # FastAPI 自动校验 body，生成 OpenAPI 文档
    ...
```

#### 11. 错误处理规范化

**现状**：三种不同风格的错误处理散落各处：
- `try/except + log.warning`（静默降级）
- `raise HTTPException(status_code=500, detail=...)`（直接抛出）
- `return {"error": "..."}`（返回值中带错误）

**方案**：

```python
# src/api/error_handler.py
class GrapeError(Exception):
    def __init__(self, code: str, message: str, status: int = 500, detail: dict = None):
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}

@app.exception_handler(GrapeError)
async def grape_error_handler(request, exc):
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": exc.message, "detail": exc.detail}
    )
```

#### 12. 日志全量结构化

**现状**：wise 分支仍用 `log.info(f"...")` 混合格式；dev 分支已引入 observability 的结构化 JSON 日志。

**方案**：全量迁移到结构化日志

```python
# 现状
log.info(f"Pipeline: start, raw_type={event_header}, source={source}, repo={evt['repository']}")

# 改进
log.info("Pipeline start", extra={
    "event_type": event_header,
    "source": source,
    "repo": evt["repository"],
    "component": "pipeline",
})
```

#### 13. Postmortem 文化

**方案**：在 `docs/postmortem/` 下记录每次线上事故：

```markdown
# 0001-exit-code-negative-false-failure.md

## 概述
Agent 被 SIGTERM 终止（exit_code=-15）但实际已完成业务产出，被误判为 failed。

## 根因
`new_status = "completed" if exit_code == 0 else "failed"` 未区分信号终止和正常失败。

## 影响
4 个历史事件被误标为 failed（100228/100230/100232/100238）。

## 预防措施
1. exit_code < 0 且 has_output=True 时标为 completed
2. evaluate_agent_output 增加 PR #N 文本引用识别（截断时 JSON 不可解析的兜底）
3. 测试覆盖信号终止场景
```

---

### P3 — 长期改进

#### 14. 插件化架构

参考 Harness 的 Cordis 模式，将以下设计为可替换插件：

| 插件点 | 当前 | 目标 |
|--------|------|------|
| 引擎适配器 | 硬编码 hermes_acp / opencode | 引擎插件接口，注册/卸载 |
| 存储后端 | mysql_compat.py PRAGMA 拦截 | 存储插件接口（SQLite/MySQL/PostgreSQL） |
| 认证方式 | JWT（硬编码） | 认证插件接口（JWT/OAuth/SSO） |
| IM 集成 | 飞书 + 钉钉硬编码 | IM 插件接口 |
| Skill 系统 | configs/pipelines.json + 文件系统 | Skill 插件接口 + 目录/仓库 provider |

#### 15. 国际化

前端 UI 文案支持中英文切换。方案：
- 提取所有 UI 文案到 `i18n/zh.json` + `i18n/en.json`
- 前端加 `lang` 切换按钮
- 参考 Harness 的 `*.i18n.yaml` 自动同步机制

#### 16. E2E 测试

引入 Playwright 做关键路径的端到端测试：

```python
# tests/e2e/test_pipeline_flow.py
def test_create_task_to_pipeline():
    """登录 → 创建任务 → Pipeline 页面验证 → Event Detail"""
    page.goto("/login")
    page.fill("[name=username]", "songspace")
    page.fill("[name=password]", "grape123")
    page.click("button[type=submit]")
    page.click("text=Task Center")
    page.click("text=+ 新建任务")
    page.fill("#tcCreateTitle", "E2E test task")
    page.click("text=创建任务")
    page.click("text=Pipeline")
    assert page.is_visible("text=E2E test task")
```

#### 17. 文档体系完善

补充以下文档：

| 文档 | 内容 | 参考 |
|------|------|------|
| `docs/architecture.md` | 整体架构图 + 模块关系 + 数据流 | Harness docs/architecture.md |
| `docs/defensive-patterns.md` | 防御性编程规范 | Harness docs/defensive-patterns.md |
| `docs/testing.md` | 测试策略 + 测试层级 + 命名规范 | Harness docs/testing.md |
| `docs/development.md` | 开发流程 + 代码规范 + PR 模板 | Harness docs/development.md |
| `docs/glossary.md` | 术语表（VOD/Issue Agent/A2A/Scene 等） | Harness docs/glossary.md |
| `docs/postmortem/README.md` | 事后复盘模板 + 规范 | Harness docs/postmortem/README.md |

#### 18. 水平扩展

**现状**：单进程 uvicorn，session 在内存中。

**方案**：
- 已有 lease 机制（`src/im/bot_lease.py`）——完善 session 共享（Redis）
- 将 `report_messages` 的 SSE fanout 从内存改为 Redis Pub/Sub
- `active_tasks` / `active_sessions` 从进程内 dict 改为 Redis
- 支持 `--workers N` 多 worker 模式

---

## 四、改进优先级与路线图

```
2026 Q3（P0 紧急）
├── 修复测试套件（全绿）
├── 引入 CI pipeline（pytest + 语法检查）
└── 拆分 event_pipeline.py（6 个子模块）

2026 Q4（P1 高优先级）
├── 引入 ruff + mypy（渐进式）
├── 前端 Vite + TypeScript 化
├── DB 层 Repository 模式
├── 覆盖率门禁 40%→60%
└── 配置管理 Pydantic Settings

2027 Q1（P2 中优先级）
├── 事件总线完善（三层事件体系）
├── API Pydantic 校验全量化
├── 错误处理规范化
├── 日志全量结构化
└── Postmortem 文化

2027 Q2+（P3 长期）
├── 插件化架构（引擎/存储/认证/IM 可替换）
├── 国际化（中英文切换）
├── E2E 测试（Playwright）
├── 文档体系完善
└── 水平扩展（Redis + 多 worker）
```

---

## 五、总结

Grape 在功能层面已经很完善——VOD 自动场景识别、多 Agent 流水线、远程 worker 引擎、知识库管理、飞书集成——但在工程基础设施层面与 DeepSeek Harness 有显著差距：

1. **测试**：Harness 有 5 层 100% 覆盖率门禁，Grape 测试大量失败且无覆盖率
2. **工程工具链**：Harness 有 41 个 verify 脚本 + lefthook + oxlint + jscpd + knip，Grape 无任何 lint/CI
3. **模块化**：Harness 有 227 个独立包，Grape 是单体 monolith 且有多个 1500+ 行的超大文件
4. **文档**：Harness 有双语文档 + postmortem + defensive patterns + 1498 agent notes，Grape 缺少系统性文档
5. **防御性编程**：Harness 有 7 类 bug 防范规则文档化，Grape 靠经验积累（无沉淀）

改进的核心原则：**先修可用性（测试+CI），再提可维护性（lint+拆分），最后追可扩展性（插件化+水平扩展）**。每一步都应有明确的验收标准，避免过度设计。
