# Grape 测试用例整体优化方案

> 版本：v1.0 ｜ 日期：2026-08-16 ｜ 分支：new-version2
> 范围：`tests/` 目录全部测试用例的质量、覆盖率、有效性诊断与优化
> 原则：本方案**不修改任何源码**，只输出诊断与优化计划，供后续执行。

---

## 1. 执行摘要

对 `tests/` 下 **88 个文件、837 条用例**进行了基线测量、覆盖率分析与逐类失败归因。

### 1.1 当前关键指标（实测基线）

| 指标 | 实测值 | 行业目标（参考） |
| --- | --- | --- |
| 用例总数 | 837 | — |
| 通过 / 失败 / 跳过 / 错误 | 783 / **30** / 3 / **21** | 失败+错误 = 0 |
| 套件整体可运行率 | **93.9%**（837 中 51 项异常） | ≥ 99.5% |
| 全量运行时长 | ~2.5 分钟（158s） | 单次 < 10 分钟 |
| 行覆盖率 | **65%**（12,963 stmts，4,587 未覆盖） | 核心模块 ≥ 80% |
| 默认运行中生效用例 | 837 - 21（UI 场景默认 ERROR） | 所有用例默认可跑 |
| CI 自动执行 | **无**（无 .github/workflows） | 每次提交强制门禁 |
| 确定性（随机顺序可复现） | 未启用随机排序验证 | 任意顺序稳定 |

### 1.2 结论

1. **套件当前不绿**：默认 `pytest` 运行即报 30 failed + 21 errors，无法作为产品能力看护的"回归防线"——任何一次回归都淹没在 51 项已知异常中。
2. **存在安全隐患**：`tests/test_api.py`、`tests/test_milestones.py` 是**泄漏到仓库里的调试脚本**，内含真实的 GitCode 生产 API Token（`Bearer XmMq...`），并会向 `api.gitcode.com` 生产地址发起请求。
3. **隔离性差**：49 处 `time.sleep`、19 处真实 HTTP 调用、30 处 subprocess、89 处直连共享测试库并手工 DELETE 清理——用例间存在**环境变量与数据库状态污染**，产生顺序依赖型失败。
4. **覆盖不均衡**：整体 65%，但 `runtime/client.py` 为 **0%**，`mysql_compat.py` 16%、`im/vod_handler_async.py` 29%、`api/routes/auth.py` 39%、`runtime/agent.py` 50%——最核心的 worker 客户端协议、DB 兼容层、鉴权、Agent 运行链全部处于盲区。
5. **测试与产品演进脱节**：飞书命令卡片化、issue 工作目录调整、安装脚本重写后，对应旧用例未同步更新而持续失败（stale tests）。
6. **基础设施缺失**：无 CI、无 pytest 插件（pytest-asyncio / pytest-cov / pytest-randomly / pytest-timeout 等）、无覆盖率门禁、无 flaky 追踪。

**一句话判断**：当前测试套件是"有量无质"——数量可观、覆盖了大部分 happy path，但**不可信、不稳定、不隔离、不完整、无门禁**，尚不足以承担"看护产品能力"的职责。优化重点不是"写更多用例"，而是**先让套件可信、稳定、可度量，再按风险分层补足关键盲区**。

---

## 2. 现状诊断

### 2.1 运行基线（本机实测，Python 3.14.3 / pytest 9.0.3）

```text
837 tests collected in 66.69s
30 failed, 783 passed, 3 skipped, 21 errors, 8 warnings in 158.26s
```

- 收集耗时 66s：大量用例在 import 阶段就初始化 TestClient、建库、造数据。
- 运行耗时 158s：49 处 `time.sleep`（含 sleep 数秒者）+ 真实 git/网络调用。
- 21 个 UI 场景用例因 `DEVOPS_UI_TEST` 守卫在默认运行中直接 ERROR，属于"设计性失效"。

### 2.2 失败分类与根因（51 项异常）

| 类别 | 数量 | 涉及用例 | 根因 |
| --- | --- | --- | --- |
| A. 陈旧用例（stale） | 14 | test_im_async_remediation（10）、test_issue_workdir（2）、test_runtime_install（1）、test_project_templates（2） | 功能已演进（命令卡片化 / 工作目录布局 / 安装脚本重写），旧用例未同步更新；新旧契约并存导致持续失败 |
| B. 依赖环境/配置 | 12 | test_global_bot_token_config（3）、test_api_routes::test_host_strict_mode_check（1）、test_m2_execution_mode::test_mode_getter_meta（1）、test_tasks::TestNotify（3）、test_web_chat_tooluse（3） | 依赖本地 `configs/` 真实配置、真实账号 token、真实 Agent 引擎；测试不封闭（non-hermetic） |
| C. UI 场景守卫 | 21 | ui_scenario_test.py（21） | 未设置 `DEVOPS_UI_TEST=1` 时 setup 抛错，默认运行"有名无实"；且守卫判断的是**生产库路径**，测试库隔离判断不准确 |
| D. 用例自身缺陷 | 4 | test_repos_remove_cleanup（1）、test_worker_package（3） | 插入不完整行触发 NOT NULL；sha256 与端点鉴权断言与当前实现不符 |
| E. 安全/泄漏 | 2 个文件 | test_api.py、test_milestones.py | 泄漏真实生产 Token + 向生产 API 发请求的调试脚本混入 tests/（非测试） |

#### 代表性证据

- **A 类（命令卡片化）**：`process_command_async` 已改为 `/repo /chat /task /new /where /cancel /clean /help` 卡片回复并返回空串；而 `test_im_async_remediation.py` 的 C 组仍在断言旧命令 `/bind /unbind /status /autodev /notify` 的文本回复（如 `"已绑定" in result`），全部返回 `''` 而失败。新的导航用例已存在（`test_feishu_navigation_commands.py`），旧用例未清理。
- **A 类（工作目录）**：`get_agent_workdir` 现产出 `.../org/test/issues/5/issue`，用例仍断言旧路径 `.../org/test/issue/issues/5`。
- **B 类（引擎依赖）**：`test_web_chat_tooluse` 期望回复 `"联通成功"`，实际得到 `"⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎"`——用例未真正建立可用引擎状态，且依赖 `set_chat_config` 与运行时初始化顺序。
- **B 类（远程 Agent）**：`test_tasks.py` 运行中出现 `Agent error: [Agent:issue|repo:gitcode:owner/repo] Unexpected server error`——用例触发了**真实远端 Agent**，测试库之外还依赖外部服务。
- **B 类（env 污染）**：`test_m2_execution_mode::test_mode_getter_meta` 断言 `set_execution_mode("queue_remote")` 后 `get_execution_mode()=="queue_remote"`，实测返回 `"direct"`——因 `get_execution_mode` **env 优先于 meta**，而其他用例残留 `DEVOPS_EXECUTION_MODE=direct` 环境变量导致顺序依赖失败。
- **D 类（表结构漂移）**：`test_repos_remove_cleanup` 按"测试库无 pipeline_instances 表"设计跳过逻辑，但测试库 schema 已含该表且 `trigger_type NOT NULL`，插入 `(id, repo_key)` 报完整性约束错误——**测试对 schema 的假设过期**。

### 2.3 覆盖率缺口（65% 整体，`--cov=src` 实测）

| 模块 | 覆盖率 | 语句数 | 风险说明 |
| --- | --- | --- | --- |
| runtime/client.py | **0%** | 102 | worker 客户端协议全盲区（端侧执行链核心） |
| api/run.py、api/run_webhook.py | 0% | — | 启动入口（低优先级） |
| mysql_compat.py | 16% | 410 | DB 兼容层（MySQL 路径），生产切库后风险最高 |
| im/vod_handler_async.py | 29% | — | VOD agent 异步处理 |
| api/routes/auth.py | 39% | — | OAuth 登录/回调鉴权 |
| api/routes/agents.py | 43% | — | Agent 路由 |
| im/feishu_async.py | 45% | — | 飞书 SDK 封装（分页、token、HTTP） |
| config.py | 47% | — | 配置解析（env 优先逻辑） |
| runtime/agent.py | 50% | 350 | worker 端 Agent 执行链 |
| worker_package.py | 57% | 288 | worker 打包/分发（install 端点） |
| user/db.py | 60% | 1093 | 用户/权限/托管（全库最大模块） |
| api/main.py | 60% | — | app 装配 |
| api/routes/chat.py | 63% | — | Web chat（近期事故高发区） |
| 其余模块 | 67%~100% | — | engine_select 87%、session_manager 83%、prompt_builder 86%、repogit 81% 等相对健康 |

> 说明：`--cov-report=term-missing:skip-covered` 下很多 100% 模块实为 `__init__.py` 等空壳；真实业务模块中 **0%~50% 档位合计约 8 个核心文件**，是"看护产品能力"的最大盲区。

### 2.4 结构性反模式清单（全库统计）

| # | 反模式 | 出现量 | 危害 |
| --- | --- | --- | --- |
| 1 | `time.sleep` 等待异步结果 | 49 处 | 慢、脆弱、超时随机 |
| 2 | `asyncio.run(...)` 同步式测异步 | 73 处 | 阻塞事件循环、无法做真实并发断言、与 pytest-asyncio 不兼容 |
| 3 | 真实 HTTP / subprocess / 系统调用 | 19 + 30 处 | 不封闭，网络/工具/权限不同则结果不同 |
| 4 | 模块级 `TestClient(app)` | 28 处 | import 即启动 app、读配置、建连接；用例互相污染 |
| 5 | 模块级 `os.environ` / `init_database` | 68 处 | env 泄漏、初始化重复 |
| 6 | `sys.path.insert` 样板 | 40 处 | conftest 已做，纯重复 |
| 7 | 直连 `db` / `db_lock` + 手工 DELETE 清理 | 89 处 | 共享可变状态、清理不全即污染、顺序依赖 |
| 8 | 无参化缺失 | parametrize 仅 3 处 | 边界/多事件类型只能复制粘贴 |
| 9 | 无效断言 | 如 `assert _card_text if False else True` | 恒真断言，形同虚设 |
| 10 | 字符串 `in` 断言 UI 文案 | 大量 | 与界面文案强耦合，改文案即碎 |
| 11 | 测试文件混入调试脚本 | 4 个文件（含 2 个泄漏 token） | 安全 + 无效 |
| 12 | 无 pytest 插件/无 CI/无覆盖率门禁 | — | 无法自动回归、无法度量 |

---

## 3. 总体优化目标与原则

以业界公认的最佳实践为基准：

- **测试金字塔**（Google Testing Blog / Martin Fowler）：单元测试占 70%+（快速、隔离），集成/契约测试居中，端到端少量。当前套件几乎全为"粗粒度集成/端到端"，缺少快速单元层，导致慢且脆。
- **FIRST 原则**：Fast（快）、Independent（独立）、Repeatable（可重复）、Self-validating（自校验）、Timely（及时）。
- **AAA 结构**：Arrange–Act–Assert，用例结构清晰，一个用例聚焦一个行为。
- **Given–When–Then 命名**：用例名/ docstring 用"前置–动作–期望"表达，失败时可直接定位契约。
- **Hermetic / 封闭测试**：测试环境完全自包含（mock 网络、mock 引擎、独立 DB、固定时钟与随机数）。
- **确定性**：任意执行顺序结果一致（用 `pytest-randomly` 验证）。
- **Mutation testing**：以"改坏代码用例必须红"作为有效性的终极度量（工具：mutmut）。
- **覆盖率作为手段而非目的**：先看"哪些产品能力无保护"，再补用例；用分支覆盖率+`fail_under` 门禁，而不是一味追求行号。

---

## 4. 优化方案（分优先级执行）

### 4.1 P0 止血：先让套件可信（建议 1~2 个迭代完成）

**目标**：默认 `pytest` 全绿或明确分区，消除安全泄漏，恢复"红=回归"的可信度。

1. **删除/移出调试脚本（安全）**
   - 删除 `tests/test_api.py`、`tests/test_milestones.py`（含真实生产 Token `XmMq...`）。
   - **立即轮换**该 Token（视为已泄露）；确认其未出现在 git 历史之外的其他分支/文档。
   - `tests/test_phase9.py`、`tests/test_github_normalize.py` 为"脚本式手工测试"（顶层 assert+print，无 pytest 用例）：改写为真正的 `test_*` 函数或移出 tests/，禁止 `test_*.py` 文件内出现模块级可执行副作用。

2. **陈旧用例二选一：删除或对齐新契约**
   - `test_im_async_remediation.py` C 组（旧命令 `/bind /unbind /status /autodev /notify`）：命令已删除，**用例整体删除**（新契约已由 `test_feishu_navigation_commands.py` 覆盖）；B 组（sync_repo_feishu_mapping）保留。
   - `test_issue_workdir.py`：断言更新为新布局 `{owner}/{repo}/issues/{num}/{agent}`；并加一个"布局契约"用例锁死 `get_agent_workdir` 路径规则。
   - `test_runtime_install.py::test_install_script_exists_on_disk`：按新脚本重写断言（不再断言 `git sparse-checkout`/`EnvironmentFile`），改为断言脚本当前的关键要素（安装流程、token 占位、幂等）。
   - `test_project_templates.py`：更新 push 命令序列断言为当前实现。

3. **环境依赖用例去封闭化**
   - `test_global_bot_token_config.py`：改为**不读真实 config 文件**，构造 fixture 化的 JSON 样本做纯逻辑校验；把"线上配置缺 key"这类运维校验移到部署预检脚本（如 `docs/new-version1/deploy/verify-local.py` 风格）。
   - `test_api_routes::test_host_strict_mode_check`：将"默认账号需真实 token"的前置改为 mock 账号/token，或标注为集成用例并移入 `tests/e2e/`。
   - `test_tasks::TestNotify`、`test_web_chat_tooluse`：Agent 执行路径全部 mock（`enqueue_task` / `wait_task_terminal` / `stream_agent` / worker 客户端），断言只验证编排与 DB 落库，不要求真实引擎。
   - `test_m2_execution_mode::test_mode_getter_meta`：用 `monkeypatch` 隔离 env；在 `teardown` 恢复环境变量（现用直接赋值 `os.environ[...]`，未隔离）。

4. **UI 场景套件收编**
   - `ui_scenario_test.py` 改为独立 marker（`@pytest.mark.e2e_ui`）+ `pytest.ini` 配置 `addopts = -m "not e2e_ui"`，默认运行排除、按需 `-m e2e_ui` 显式跑，**而不是靠运行时抛错**；守卫改为基于测试库（`DEVOPS_DB_PATH` 指向 test 库）而非"生产库路径"字符串判断。

**P0 完成标准**：默认 `pytest -m "not e2e_ui"` 全绿、0 异常、无任何真实网络/引擎调用；`git grep` 不再出现真实 Token。

### 4.2 P1 测试基础设施：把质量变成门禁

1. ~~**建立 CI**（GitHub Actions 或自建流水线）~~ — **暂缓（2026-08-17 决定）**：仓库托管在 GitCode、暂无 GitHub 计划，GitHub Actions 不适用。CI 延后到接入 GitCode CI 或迁移平台后再执行。本节其余项可在本地先行生效，CI 只是"自动触发"载体。
   - 届时：每次 PR / push 跑 `pytest -m "not e2e_ui" --cov=src --cov-fail-under=<阈值>`；分 Job（单元/集成/e2e_ui 带浏览器 + `DEVOPS_UI_TEST=1`）。
2. **引入 pytest 插件**（`requirements-dev.txt`）：
   - `pytest-asyncio`（`asyncio_mode = auto`，替换 73 处 `asyncio.run`，支持 async fixture）
   - `pytest-cov` + `--cov-fail-under`（分支覆盖率 `--cov-branch`）
   - `pytest-randomly`（随机化用例/模块顺序，暴露顺序依赖；**需在 4.3 隔离完成后启用**）
   - `pytest-timeout`（兜底防 hang，如 60s/用例）
   - `pytest-xdist`（`-n auto` 提速；前提是 4.3 隔离完成）
   - `pytest-rerunfailures`（过渡期标记已知 flaky，但**禁止长期依赖**）
3. **覆盖率门禁分档**（门槛值先在 `pytest.ini` 本地生效，CI 接入后强制）：
   - 第一档：总行覆盖率 ≥ 70%，核心模块（runtime/client、api/routes/auth、im/feishu_async、runtime/agent、user/db）≥ 75%。
   - 第二档（稳定后）：核心模块 ≥ 85%，启用分支覆盖率。
   - 新增功能 PR 要求"改动行覆盖率"≥ 80%（`--cov` 增量覆盖，可用 diff 工具如 `diff-cover`）。
4. **统一 conftest**：删除 40 处 `sys.path.insert`/env 样板，全部收口到 `tests/conftest.py`；提供共享 fixture（见 4.3）。

### 4.3 P1 隔离与确定性：消除共享状态

1. **数据库逐用例隔离**：
   - 每用例/每测试类使用独立临时库（fixture 内 `tmp_path` + `DEVOPS_DB_PATH` 指向新库文件），而不是所有用例共用一个 `data/test_sessions.db`。
   - 若保持单库，则封装 `repo_fixture`/`user_fixture`/`engine_fixture` 等**自动回滚（事务包裹或建删）**的 fixture，禁止用例内裸 `db_lock` + 手工 DELETE。
2. **环境变量隔离**：所有 `os.environ` 写入改用 `monkeypatch`；`teardown` 一律走 `monkeypatch`/fixture 自动恢复。
3. **网络完全封闭**：
   - 单元/集成层：把 `urllib`、`aiohttp`、`lark-oapi`、git 命令全部 mock（resp capture、fake git repo via `tmp_path`）。
   - 引入响应录播（如 `requests-mock` / `aioresponses` / VCR）保存真实响应样本做契约测试，离线可跑。
4. **时钟/随机确定性**：`datetime.utcnow()` 等时间敏感断言注入可控时钟；`random`/`uuid` 固定 seed。
5. **随机顺序验证**：CI 中用 `pytest-randomly`，本地 `make test-random` 一键验证无顺序依赖。

### 4.4 P1 分层测试策略与优先级（按产品风险）

重新组织 `tests/` 目录结构，明确分层归属：

```
tests/
  unit/       纯逻辑、无 IO（DB/网络/文件均 mock）—— 最快，占 70%
  integration/带真实 SQLite + FastAPI TestClient，但网络/引擎 mock
  e2e_ui/     真实浏览器 + 真实服务（pytest-playwright）—— 少量、慢
```

**优先级排序（按"出事概率 × 影响"给各能力分层补测）**：

| 优先级 | 产品能力 | 当前状态 | 建议动作 |
| --- | --- | --- | --- |
| P0 | Worker 客户端协议 `runtime/client.py` | **0%** | 补协议级单测（握手/心跳/任务轮询/结果上报/断线重连）；这是端侧执行链核心 |
| P0 | 鉴权 `api/routes/auth.py` | 39% | 补 OAuth 回调、token 校验、登录/退出、redirect_uri 校验分支 |
| P0 | DB 兼容层 `mysql_compat.py` | 16% | 无法起真 MySQL 时，用 SQL 语句级断言 + 轻量 MySQL（docker）集成 job |
| P1 | 飞书异步封装 `im/feishu_async.py` | 45% | 分页/重试/token 刷新/错误降级（用 aioresponses） |
| P1 | worker 端执行 `runtime/agent.py` | 50% | 执行链、超时、fallback、事件输出格式 |
| P1 | Agent 路由 `api/routes/agents.py` | 43% | 各 agent 增删查、引擎选择、状态流转 |
| P1 | 用户/托管 `user/db.py` | 60% | 权限矩阵（owner/member/join）、webhook token 兜底、软删 |
| P1 | Web chat `api/routes/chat.py` | 63% | send/stream 全事件流、queue 与 direct 双通道、工具调用 |
| P1 | Worker 打包分发 `worker_package.py` | 57% | 打包内容/校验和/鉴权/坏 token/并发拉取 |
| P2 | 配置解析 `config.py` | 47% | env 优先、缺省值、非法值分支 |

### 4.5 P2 用例设计规范（后续新增用例必须遵守）

1. **命名与结构**：`test_<行为>_when_<前置>_<期望>` 或 Given–When–Then docstring；每个用例严格 `Arrange / Act / Assert` 三段落，用空行分隔。
2. **一个用例只断言一个行为簇**：不要一个用例里既查 200 又查 DB 又查消息文本——拆成独立用例（可用 fixture 复用 Arrange）。
3. **多用参数化**：同类逻辑的多输入（事件类型、权限角色、边界值）用 `@pytest.mark.parametrize`，避免复制粘贴用例（当前仅 3 处）。
4. **边界与错误路径**：每个新接口至少包含：正常路径、空输入、非法输入、越权、资源不存在、外部服务失败、超时/重试。可参考 `test_repos_remove_cleanup.py::test_invalid_owner_repo_skipped`（路径穿越防护）这类好的样例。
5. **断言强度**：
   - 优先精确断言（返回值、状态码、DB 行内容），避免 `"xxx" in 长文案`；文案校验收敛到独立的文案/卡片契约测试。
   - 禁止恒真断言（如 `assert expr if False else True`），引入 `flake8` / `ruff` 规则（如 `S101` 提醒）或 code review 拦截。
   - 对"不应发生"的路径用 `assert_not_called` / `assert_called_once_with` 精确断言调用参数（现有部分用例已示范，可推广）。
6. **异步用例**：一律 `async def` + pytest-asyncio；等待用 `await` 而非 `time.sleep`；对"后台任务"断言其最终副作用（await 完成）或 mock 返回，不要 `_swallow` 后断言不可能发生的结果。
7. **fixture 分层**：session/class/function 范围按"不可变性"分级；共享数据用 `session`+只读，可变数据用 `function`；所有 fixture 自带清理。
8. **测试即文档**：用例 docstring 引用设计文档（`docs/new-version2/*-design.md`）与事故复盘（postmortem）编号，形成需求追踪链（现有部分已做，保持）。

### 4.6 P2 有效性增强（可选项，稳定后引入）

1. **Mutation testing**（`mutmut` 或 `pytest-mutate`）：对核心模块（runtime/client、auth、engine_select）做变异测试，目标变异击杀率 ≥ 70%，找出"测了但没保护住"的用例。
2. **契约测试**：对 webhook 归一化、飞书卡片、worker 协议做 schema 契约（`jsonschema`/OpenAPI 断言），防止字段漂移。
3. **故障注入**：为 flaky 场景（网络重试、死锁、超时降级——本仓库已有多起此类事故）引入专门故障注入 fixture。

### 4.7 P2 性能优化

- 移除 `time.sleep` 轮询 → `await asyncio.wait_for(event)` 或 `pytest-timeout` 内快速轮询（sleep 0.01 步进）。
- 收集 66s：把 `TestClient(app)` 改为 `client` fixture（session 级复用）；延迟 import 减 import 副作用。
- 用例隔离后可用 `pytest-xdist -n auto` 并行，目标全量 < 2 分钟。

### 4.8 新测试准入 Checklist（PR 合入门禁）

- [ ] 套件全绿（`pytest -m "not e2e_ui"`，0 failed / 0 errors）
- [ ] 未引入真实网络/真实引擎调用（`git grep api.gitcode.com / enqueue / sleep` 检查）
- [ ] 未在测试文件出现真实密钥/token
- [ ] 新功能覆盖 ≥ 80%（改动行）
- [ ] 用例含边界 + 错误路径，非仅 happy path
- [ ] 无 `time.sleep`、无 `asyncio.run`、无模块级副作用
- [ ] 通过 `pytest-randomly` 顺序验证
- [ ] docstring 关联设计文档/事故编号

### 4.9 度量与反馈闭环

| 指标 | 采集方式 | 目标 |
| --- | --- | --- |
| 通过率 / 失败数 | CI 汇总 | 100% |
| 运行时长 | CI 报告 | < 10 min（本地 < 3 min） |
| 行/分支覆盖率 | pytest-cov | 核心模块 ≥ 85% |
| flaky 率（rerun 次数/重跑通过数） | rerun 日志 | < 0.5% |
| 变异击杀率 | mutmut | ≥ 70%（核心模块） |
| 用例与需求追踪覆盖率 | docstring 引用检查 | 新用例 100% 有关联 |

**流程建议**：每次产品变更要求"先写/改用例再改码"（TDD）或至少"同 PR 提交用例"；CI 红灯由提交者负责当轮清零，不允许合并红套件。

### 4.10 分阶段路线图

| 阶段 | 内容 | 预计耗时 | 完成标准 |
| --- | --- | --- | --- |
| **Phase 0（安全）** | 删除泄漏脚本、轮换 token、收编调试脚本 | 0.5 天 | 无 token 泄漏、tests/ 无脚本（已实施，见附录 E；token 轮换待办） |
| **Phase 1（转绿）** | 陈旧用例删/改、环境依赖用例 mock 化、UI 守卫改 marker | 2~3 天 | 默认 `pytest` 全绿（排除 e2e_ui）（已实施，见附录 E） |
| **Phase 2（基设，不含 CI）** | requirements-dev、pytest 插件、conftest 统一、覆盖率门槛本地生效 | 2~3 天 | 插件就位、conftest 收口、`--cov-fail-under` 本地生效 |
| **Phase 2b（CI，暂缓）** | GitCode CI 或迁移后接入门禁；届时自动跑 + 覆盖率强制 | — | 每次提交自动回归（待托管平台确定） |
| **Phase 3（隔离）** | DB 逐用例隔离、env 隔离、网络封闭、随机顺序验证 | 2~3 天 | 随机顺序全绿、可并行 |
| **Phase 4（补覆盖）** | 按 4.4 优先级补 P0 盲区（runtime/client、auth、mysql_compat） | 1~2 周 | 核心模块 ≥ 75% |
| **Phase 5（提质）** | 分层重构（unit/integration/e2e）、参数化、mutation、契约 | 持续 | 核心模块 ≥ 85%，变异击杀率达标 |

---

## 5. 附录 A：失败清单与处理建议

| 用例 | 类别 | 处理建议 |
| --- | --- | --- |
| test_api_routes::test_host_strict_mode_check | B-配置 | mock 账号 token 或移 e2e |
| test_global_bot_token_config（3） | B-配置 | 改为 fixture 化 JSON 纯逻辑测试 |
| test_im_async_remediation C1~C9,C11（10） | A-陈旧 | 删除（新导航命令另有测试） |
| test_issue_workdir（2） | A-陈旧 | 对齐新目录布局 + 锁契约 |
| test_m2_execution_mode::test_mode_getter_meta | B-env | monkeypatch 隔离 env |
| test_project_templates（2） | A-陈旧 | 对齐 push 命令序列 |
| test_repos_remove_cleanup::test_clears_all_repo_tables | D-缺陷 | 插入完整行（含 trigger_type） |
| test_runtime_install::test_install_script_exists_on_disk | A-陈旧 | 按新脚本重写断言 |
| test_tasks TestNotify（3） | B-引擎 | mock 后台任务（await 完成而非 swallow） |
| test_web_chat_tooluse（3） | B-引擎 | 建立 fixture 化的引擎状态 + mock stream |
| test_worker_package（3） | D-缺陷 | 同步 payload/校验和/鉴权断言 |
| ui_scenario_test（21） | C-守卫 | marker 收编 + 测试库守卫 |

## 6. 附录 B：覆盖率缺口详表

见 §2.3 表格。优先补齐顺序：`runtime/client.py` → `api/routes/auth.py` → `mysql_compat.py` → `im/feishu_async.py` → `runtime/agent.py` → `api/routes/agents.py` → `user/db.py` → `api/routes/chat.py`。

## 7. 附录 C：反模式清单（对照表）

用于 code review 与自检，`见 §2.4`。其中最高优先消除的 5 项：
1. 模块级副作用（TestClient 实例化、init_database、env 写入）
2. `time.sleep` / `asyncio.run`
3. 共享测试库裸操作 + 手工 DELETE
4. 真实网络 / 引擎调用
5. 恒真/弱断言

## 8. 附录 D：新用例模板

```python
class TestXxxBehavior:
    """设计文档: docs/new-version2/xxx-design.md §y.z ｜ 事故复盘: docs/new-version2/xxx-postmortem.md"""

    @pytest.fixture
    def repo(self, isolated_db):
        """Arrange：独立库 + 托管仓库。"""
        ...

    def test_returns_success_when_owner(self, repo):
        """Given 仓库 owner / When 调用接口 / Then 返回 200 且落库 status=active。"""
        # Arrange
        ...
        # Act
        r = client.post(...)
        # Assert
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert db_row(repo)["status"] == "active"

    @pytest.mark.parametrize("role,expected", [("owner", 200), ("member", 403), ("guest", 403)])
    def test_permission_matrix(self, repo, role, expected):
        ...
```

---

## 附录 E：P0 实施记录（2026-08-17）

> 按 §4.1 P0 方案实施完成，目标达成：**默认 `pytest` 全绿、可重复、安全**。

### 达成基线（实测）

| 指标 | 实施前 | 实施后 |
| --- | --- | --- |
| 默认套件结果 | 30 failed + 21 errors | **811 passed / 3 skipped / 21 deselected，0 failed / 0 errors** |
| 全量运行时长 | ~158s | ~45s |
| 安全泄漏 | test_api.py / test_milestones.py 含真实 Token | 已删除（Token 需在 GitCode 后台轮换） |
| 确定性 | 依赖共享库残留状态 | **清库后全新 DB 全绿**（已实测 2 次） |

### 变更清单（全部为测试/测试配置，未改任何 src/ 业务代码）

1. **安全**：删除 `tests/test_api.py`、`tests/test_milestones.py`（含生产 Token 的调试脚本）；`test_phase9.py`、`test_github_normalize.py` 由"脚本式手工测试"改写为正式 pytest 用例。
2. **陈旧用例**：`test_im_async_remediation.py` 删除已废除命令（/bind /unbind /status /autodev /notify）的 C 组验收用例；`test_issue_workdir.py` 对齐新工作目录布局；`test_runtime_install.py` 对齐新安装脚本要素；`test_project_templates.py` 对齐 `push --force -u origin HEAD:main` 与 git 模板 user-token 回退。
3. **去封闭化**：`test_global_bot_token_config.py` 改为 fixture 化纯逻辑校验（不读真实配置文件）；`test_host_strict_mode_check` mock GitCode API（不依赖真实账号 token）；`test_m2_execution_mode` 用 `monkeypatch` 隔离执行模式 env。
4. **UI 场景收编**：`pytest.ini` 移至仓库根并配置 `addopts = -m "not e2e_ui"`；`ui_scenario_test.py`、`ui_test_playwright.py` 标记 `e2e_ui`（默认排除，`pytest -m e2e_ui` 显式运行；破坏性远端生产库守卫保留）。
5. **隔离修复（关键）**：`test_session_manager.py` 用 `importlib` 重载 `src.session_manager` 并指向临时库，却不恢复全局模块——导致后续测试文件（test_tasks/test_web_chat_tooluse/test_worker_package）拿到缺 `runtime_id`/`chat_conversations`/`agent_runtimes`/`runtime_credentials` 的旧 schema 连接而连锁失败。已加 autouse fixture 每用例后恢复模块。**这是本次 8 例顺序依赖失败的根因**。
6. **确定性**：`test_web_chat_tooluse._seed_local_engine` 改为非破坏性（保留健康本机 opencode 兜底）；`test_api_routes::test_config_put_default_backend_no_ghost` 自给自足播种健康本机引擎，保证全新 DB 也可跑通。
7. **用例缺陷**：`test_repos_remove_cleanup` 移除对可选生产表 `pipeline_instances` 的脆弱依赖；`test_worker_package` 移除已废弃的"内置 Python"断言。

### 遗留与提醒

- **必须轮换已泄漏 Token**（`XmMqZP6LJ89XpPkAL2YYm8yY`，在 GitCode 后台撤销；git 历史中仍含旧文件内容）。
- `ui_test_playwright.py` 文件名不匹配 pytest 默认收集规则（`test_*.py` / `*_test.py`），默认不被收集（现状如此，非本次引入）；其 `pytestmark` 已标记 `e2e_ui`，后续如纳入 e2e 轨可重命名为 `ui_playwright_test.py`。
- 本阶段仅做 P0 止血；§4.2~§4.5（CI、pytest 插件、DB 逐用例隔离、分层）属 P1，尚未实施。

---

## 附录 F：P1 调整说明（2026-08-17）

> 决策：仓库托管在 GitCode、暂无 GitHub 计划，**CI 暂缓**（GitHub Actions 不适用）。P1 其余项先在本地生效，CI 仅作为"自动触发"载体延后。

### 调整后的 P1 执行顺序

**P1-A 基础设施（不含 CI）**
1. `requirements-dev.txt` + pytest 插件：`pytest-asyncio` / `pytest-timeout` / `pytest-randomly` / `pytest-cov` / `pytest-xdist`
2. conftest 统一：收口 40 处 `sys.path.insert` / env 样板，提供共享 fixture
3. 覆盖率门槛本地生效：`pytest.ini` 加 `--cov` + `fail_under`（CI 接入后强制）

**P1-B 隔离与确定性（§4.3）**
4. DB 逐用例隔离（fixture 独立临时库）
5. env 隔离（monkeypatch 化）、网络封闭（mock urllib/aiohttp/lark、响应录播）
6. `pytest-randomly` 随机顺序验证（**隔离完成后才启用**）

**P1-C 补覆盖（§4.4，按风险优先级）**
7. `runtime/client.py` 0% → `auth.py` 39% → `mysql_compat.py` 16% → `feishu_async.py` 45% → `agent.py` 50% → `agents.py` 43% → `user/db.py` 60% → `chat.py` 63%

### 风险提示

- P1-B 的 DB 隔离是**大改造**（89 处直连 `db` + 手工 DELETE），会动大量测试文件，建议最后集中做。
- P1-C 补覆盖是**纯新增**，无回归风险，可随时并行。
- 建议执行顺序：P1-A → P1-C → P1-B；CI 待托管平台确定后接入（Phase 2b）。

---

## 附录 G：实施约定（2026-08-17）

> **涉及 `src/` 业务源码修改时的处理原则**：P1 及后续实施过程中，若发现需要改动业务源码
> （真实缺陷修复、测试缝、行为变更等），**一律不直接修改**。先将问题完整记录到下方
> 「待确认的源码修改」清单，交由负责人整体评估后再决定是否实施。测试侧工作不受影响、照常进行。

### 待确认的源码修改清单

| 编号 | 日期 | 触发场景（测试/模块） | 问题描述 | 建议改动点 | 状态 |
| --- | --- | --- | --- | --- | --- |
| （暂无） | — | — | — | — | 待评估 |

> 新增记录时填写一行；"状态"取：待评估 / 已确认实施 / 已否决 / 已按测试侧绕过。

---

## 附录 H：P1-A 基础设施 + P1-C 补覆盖实施记录（2026-08-17）

> 按附录 F 的调整顺序执行了 **P1-A（基础设施，不含 CI）** 与 **P1-C（补覆盖）**。
> 全程未改任何 `src/` 业务源码（无新增待确认源码修改项，附录 G 清单仍为空）。

### 达成基线（实测，默认 `pytest` 确定性顺序 + `--cov`）

| 指标 | 实施前 | 实施后 |
| --- | --- | --- |
| 默认套件结果 | 830 passed | **1025 passed / 5 skipped / 21 deselected，0 failed / 0 errors** |
| 整体行覆盖率 | 65% | **71.08%**（门禁已上调至 70%） |
| pytest 插件 | 无 | asyncio/cov/randomly/timeout/xdist/rerunfailures 已装（`requirements-dev.txt`） |

### 模块覆盖率变化（P1-C）

| 模块 | 实施前 | 实施后 |
| --- | --- | --- |
| runtime/client.py | 0% | **100%** |
| api/routes/auth.py | 39% | **100%** |
| mysql_compat.py | 16% | **93%** |
| im/feishu_async.py | 45% | **71%** |
| runtime/agent.py | 50% | **70%** |

### P1-A 变更

1. 新增 `requirements-dev.txt`（pytest + 6 插件）。
2. `pytest.ini`：加 `timeout = 120`、`asyncio_mode = auto`、`-p no:randomly`（P1-B 隔离完成前临时禁用随机顺序，防止暴露顺序依赖红）。
3. 新增 `.coveragerc`：`source = src`、`fail_under = 70`（第一档门槛，本地 `pytest --cov` 即强制）。
4. conftest 统一：移除 36 处冗余模块级 `sys.path.insert`，统一由 `tests/conftest.py` 引导（保留 conftest 权威行、test_session_manager 功能行、ui_test_playwright 子进程行）。

### P1-C 变更（全部新增测试文件，纯新增、无回归）

| 文件 | 覆盖目标 | 用例数 |
| --- | --- | --- |
| tests/test_runtime_client.py | RuntimeClient 全状态码/退避/异常/连接池 | 35 |
| tests/test_auth_routes.py | OAuth 发起/回调全流程/错误分支/白名单/资料登出 | 16 |
| tests/test_mysql_compat.py | SQL 方言翻译/配置读取/Cursor 与 DB 类(mock pymysql) | 66 |
| tests/test_feishu_async.py | seen 去重/解析函数/BotManager/HTTP 层(mock aiohttp) | 55 |
| tests/test_runtime_agent.py | 主循环/辅助函数/重注册/流桥接(POSIX 项按平台跳过) | 25 |

### 遗留

- **P1-B（隔离与确定性）尚未实施**：DB 逐用例隔离、env/网络封闭、`pytest-randomly` 启用。
- `im/feishu_async.py`(71%)、`runtime/agent.py`(70%) 距核心模块 75% 目标尚有差距（剩余为 WS 长连接主循环与 `_execute_task` 执行链，复杂度高，建议 P1-B 后补）。
- `user/db.py`(60%，1093 行) 未在本轮补覆盖（体量大，建议单独一轮）。

---

## 附录 I：P1-B 隔离与确定性实施记录（2026-08-17）

> 目标：随机顺序全绿、可并行。**部分达成**：确定性基线、跨运行隔离、确定性污染源已修复；
> 全部随机顺序无关依赖"DB 逐用例隔离"大改造（见下方遗留），故默认仍禁用随机顺序。

### 已实施（全部测试侧，未改 src/）

1. **B2 会话开始重建干净测试库**（conftest `_ensure_test_db` 删除 `data/test_sessions.db` 后 init_database）：
   消除跨运行残留脏行与用户 id 回收冲突，任何一次 `pytest` 都从确定的空库基线开始。
2. **修复确定性顺序依赖污染源**：
   - `test_tasks`：`TestTaskLifecycle::test_reassign_closes_processing_old_assignee` 用 `_uid["id"]` 写
     `gitcode_user='bob'` 的映射，因 `unified_user_mapping.user_id` 是主键直接覆盖 younger_eric 映射 →
     bob 改用独立用户；setup/teardown 清理测试用户卫生。
   - `test_api_routes::test_skills_upload_repo_scope_via_form`：改为限定 `scope+owner` 查询 + 自隔离清理
     （杜绝历史残留行/磁盘目录干扰）。
   - `test_api_routes::test_webhook_invalid_token`：自给自足预置托管行。
   - `test_m2_execution_mode` / `test_runtime_auto_mode`：引擎 `_clean` 改为清空全部远端/全局引擎
     （保留本机 credential_id=0 兜底），避免 `pick_engine` 选到其他测试残留引擎。
3. **B4 网络封闭审计**：默认套件无真实外部调用（mock 全覆盖；仅 `e2e_ui` 标记的远端场景测试默认排除）。
4. **随机顺序验证（pytest --randomly-seed=N）**：多 seed 下 `test_tasks`/`skills`/`webhook` 等确定性
   污染源已修复；seed 1/3/5 全绿。

### 达成基线（默认确定性顺序 + --cov）

| 指标 | 实施前 | 实施后 |
| --- | --- | --- |
| 默认套件 | 1025 passed | **1036 passed / 5 skipped / 21 deselected，0 失败** |
| 整体覆盖率 | 71.08% | 70.87%（门禁 70% 通过） |
| 跨运行确定性 | 依赖残留库 | 会话开始重建干净库 |

### 遗留（需后续 DB 逐用例隔离解决）

- **默认仍禁用 pytest-randomly**（pytest.ini `-p no:randomly`）：随机顺序下 `test_skills`/
  `test_m2_execution_mode`/`test_runtime_auto_mode`/`test_feishu_navigation_commands` 等仍存在
  引擎/技能/会话状态互相污染（跨 ~60 个测试文件共享 `agent_runtimes`/`runtime_credentials`/
  `chat_agent_session`/`meta`/`skill` 等表）。彻底解决需 **DB 逐用例隔离**（每个用例独立临时库 +
  模块重载链），属 P1-B 核心大改造，建议后续专项实施。
- env 逐用例 `monkeypatch` 化、时钟/随机确定性注入尚未系统性推行（随隔离改造一并做）。

---

*注：本方案所有断言均基于当前分支 `new-version2` 实测数据（2026-08-16）。执行 Phase 1 前建议先跑一次 `pytest -q --cov=src --cov-report=term` 复核基线。*
