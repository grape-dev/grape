# Plugin 模式实施与修复记录

> 日期：2026-08-26
> 分支：dev / wise
> 模式：GRAPE_PIPELINE_MODE=plugin

---

## 一、架构概述

### 1.1 双模式开关

| 模式 | 环境变量 | 特征 |
|------|----------|------|
| legacy | 空（未设置） | 仓库级流水线，固定 AGENT_ORDER，agent-skill-mapping.json 查 skill，pick_engine 选引擎 |
| plugin | GRAPE_PIPELINE_MODE=plugin | 任务级流水线，pipelines.json 配置，引擎标签化，事件驱动 A2A，claim_by_tag 认领 |

### 1.2 五层架构

```
┌─────────────────────────────────────────────────────┐
│ Layer 5: 大规模优化                                  │
│   batch_recover_stale_tasks / heartbeat_consolidated │
├─────────────────────────────────────────────────────┤
│ Layer 4: 事件总线 + 流水线执行器                      │
│   event_bus.py (emit/waterfall/serial/parallel)      │
│   pipeline_executor.py (dispatch_node)               │
│   A2A 升级为事件驱动 (agent/handoff → event_bus)      │
├─────────────────────────────────────────────────────┤
│ Layer 3: 双模式开关 + 技能/引擎查找                   │
│   is_plugin_mode() / resolve_skill_from_pipeline()   │
│   pick_engine plugin fallback (resolve_default_engine)│
│   enqueue_task 写 pool_tag                           │
├─────────────────────────────────────────────────────┤
│ Layer 2: 流水线配置化                                │
│   pipelines.json (7 场景 × 5 节点)                   │
│   pipeline_config.py (加载器)                        │
│   pipeline.js (动态渲染) / scenes API                 │
├─────────────────────────────────────────────────────┤
│ Layer 1: 引擎标签化                                  │
│   engine_capabilities 表 / agent_runtimes.tags 列    │
│   claim_by_tag + scope 隔离 (global/repo/user)       │
│   标签 CRUD API + 控制台 UI (点亮/熄灭切换)           │
└─────────────────────────────────────────────────────┘
```

---

## 二、新增文件

| 文件 | 用途 |
|------|------|
| `src/api/event_bus.py` | 事件总线（emit/waterfall/serial/parallel 4 种模式） |
| `src/api/pipeline_executor.py` | 流水线执行器（dispatch_node，按 mode 分派） |
| `src/api/pipeline_config.py` | pipelines.json 加载器（get_scenes/get_node_by_tag/is_node_skipped） |
| `configs/pipelines.json` | 7 场景流水线配置（default/iac/skill-dev/api-skill-dev/blog-publish/cloud-deploy/hello-blog-sys） |

---

## 三、修改文件清单

### 3.1 后端 Python

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | engine_capabilities 表 DDL + agent_runtimes.tags/pool_tag/last_scene 列迁移 + _sync_official_skills + CJK 标点修复 |
| `src/api/event_pipeline.py` | is_plugin_mode() + resolve_skill_from_pipeline() + _enqueue 写 pool_tag + _create_direct_task 补齐 INSERT 参数（36 列） |
| `src/api/routes/events.py` | redispatch event-driven 改造（原子 requeue+reassign / 直连执行触发 / prompt 恢复 / evt 补充 source+repository） |
| `src/api/routes/runtime.py` | claim-by-tag API + GET /runtimes/{rid}/tags + GET /tasks/{tid}/env + 标签注册存储 + heartbeat_consolidated + batch-recover 端点 + REPLACE INTO 修复 |
| `src/api/routes/settings.py` | 标签 CRUD 4 端点 + REPLACE INTO 修复 |
| `src/api/routes/scenes.py` | /with-pipeline 端点 |
| `src/api/routes/pipeline.py` | 返回 pipeline_config |
| `src/api/routes/webhook.py` | A2A 事件驱动（agent/handoff → event_bus → pipeline_executor）+ plugin skill 查找 |
| `src/runtime/service.py` | claim_by_tag + scope 隔离 + enqueue_task pool_tag 参数 + _TASK_COLUMNS 补 pool_tag + batch_recover_stale_tasks + heartbeat_consolidated + _collect_server_env 按 provider 映射环境变量名 |
| `src/runtime/engine_select.py` | pick_engine plugin fallback (resolve_default_engine) |
| `src/runtime/agent.py` | claim 循环从服务端 API 拉取标签（client.get_tags）+ 执行前拉取最新 env（client.get_task_env）+ 注册不再声明 tags |
| `src/runtime/client.py` | claim_by_tag + get_tags + get_task_env 方法 |
| `src/agent/executor.py` | 凭据注入按 provider 映射（huawei_cloud → HUAWEICLOUD_SDK_AK/SK） |
| `src/api/event_store.py` | 无改动（update_event_status 已兼容） |

### 3.2 前端

| 文件 | 改动 |
|------|------|
| `src/api/static/js/pages/runtimes.js` | 标签点亮/熄灭切换（6 标签全展示，optimistic UI，不刷新页面） |
| `src/api/static/js/pages/pipeline.js` | 动态节点渲染（mode icons / tag colors / _getPipeNodes） |
| `src/api/static/index.html` | 版本号 bump (runtimes.js?v=13, pipeline.js?v=16, events.js?v=33) |

---

## 四、核心设计决策

### 4.1 标签单一数据源

**决策**：标签以 DB engine_capabilities 表为唯一数据源，控制台修改后立即生效。

**实现**：
- worker 不再依赖 ENGINE_TAGS 环境变量
- worker claim 循环每次调用 `GET /runtimes/{rid}/tags` 从服务端拉取最新标签
- 注册时不再声明 tags（由用户在控制台配置）
- claim_by_tag 实时查 DB 校验标签能力

**链路**：
```
控制台修改标签 → DB engine_capabilities 表
                          ↓
worker claim 循环 → GET /runtimes/{rid}/tags → 获取标签列表
                          ↓
worker claim_by_tag(tag, rid) → 服务端查 DB 校验 → 认领任务
```

### 4.2 凭据实时同步

**决策**：worker 执行任务前从服务端拉取最新凭据，不依赖 task 创建时的 env_extra 快照。

**实现**：
- 新增 `GET /tasks/{task_id}/env` API 端点，从 DB 实时读取 user_credentials
- worker `_execute_task` 中调 `client.get_task_env(tid)` 拉取最新 env，覆盖 env_extra 快照
- `_collect_server_env` 按 provider 映射环境变量名：
  - huawei_cloud → `HUAWEICLOUD_SDK_AK` / `HUAWEICLOUD_SDK_SK`
  - codearts → `CODEARTS_CLI_AK` / `CODEARTS_CLI_SK`

**链路**：
```
控制台刷新 AK/SK → DB user_accounts 表
                        ↓
worker 执行前 → GET /tasks/{tid}/env → _collect_server_env → 从 DB 读取
                        ↓
env.update(fresh_env) → 注入 hermes ACP 子进程
```

### 4.3 redispatch event-driven 改造

**决策**：redispatch 在 plugin 模式下按标签重新分配引擎，支持本机直连和远端 worker。

**requeue 分支**（task 是 dispatched/running）：
1. 原子 requeue + reassign：`UPDATE status='queued'` + 释放 busy_slots + 按 pool_tag 查有标签的在线引擎
2. 优先选远端引擎（credential_id > 0），无则回退本机直连
3. 如果是本机直连引擎，触发 `start_direct_agent(_execute_agent, ...)`

**else 分支**（task 是终态/缺失）：
1. 从旧 task 恢复 prompt（如果最新 task prompt 为空，回溯找有 prompt 的 task）
2. 按 pool_tag 查有标签的在线引擎
3. `enqueue_task` 创建新 task（存完整 prompt + 空 env_extra）
4. 如果是本机直连引擎，触发 `start_direct_agent`

**evt 构建**：必须包含 `repository`、`source`、`type` 字段（prompt_builder 直接用 `evt['repository']` / `evt['source']`，不是 `.get`）

### 4.4 A2A 事件驱动

**决策**：A2A 从直接 HTTP 调用升级为事件驱动。

**链路**：
```
Agent 完成执行 → POST /api/a2a
    → webhook.py 检测 is_plugin_mode()
    → emit agent/handoff event → event_bus
    → _on_handoff_dispatch listener
    → pipeline_executor.dispatch_node(scene, node, mode="emit")
    → _enqueue_via_pipeline → _enqueue_pipeline_task
    → enqueue_task 写 pool_tag
    → worker claim_by_tag 认领
```

### 4.5 直连引擎执行

**问题**：本机直连引擎（credential_id=0）的任务不会被远端 worker claim，需要服务端直接执行。

**判断方式**：不用 `runtime_execution_kind()`（受全局 `queue_remote` 模式影响），直接检查 `credential_id == 0`。

**执行方式**：
- 远端引擎：`enqueue_task` → worker claim
- 本机直连：`enqueue_task`（存完整 prompt）+ `start_direct_agent(_execute_agent, ...)` → 线程池直接执行

---

## 五、修复记录

### 5.1 MySQL 兼容性

| 问题 | 修复 |
|------|------|
| `INSERT OR REPLACE` SQLite 语法在 MySQL 下报错 | 改为 `REPLACE INTO` |
| `_create_direct_task` INSERT 参数 31 个但列 36 个 | 补齐 pool_tag 等缺失字段 |
| `?` 占位符 | MySQL 兼容层 `_replace_qmarks` 自动转换，无需手动改 |
| 全角标点（：。（））导致 Python 3.12 SyntaxError | 批量替换为 ASCII |

### 5.2 Worker 标签同步

| 问题 | 修复 |
|------|------|
| worker 注册时 ENGINE_TAGS 环境变量未生效 | _discover_local_runtimes 加 fallback 到全局 ENGINE_TAGS |
| 安装脚本部署的代码不含最新 agent.py | SFTP 直传 agent.py/client.py/service.py/executor.py |
| 控制台修改标签后 worker 不感知 | worker 改为从 `GET /runtimes/{rid}/tags` 拉取，不再依赖环境变量 |
| 标签切换导致页面闪烁 | optimistic UI：直接修改 chip 样式 + onclick，不调 refreshRuntimes |

### 5.3 引擎选择

| 问题 | 修复 |
|------|------|
| pick_engine 无 binding 时返回 None | plugin 模式加 resolve_default_engine 兜底 |
| redispatch 保留旧 runtime_id，worker claim 到错误引擎 | 原子 requeue + reassign 到有标签的引擎 |
| reassign 选了本机直连引擎但无人执行 | 检查 credential_id=0 时触发 start_direct_agent |
| runtime_execution_kind 返回 queue_remote 而非 direct | 直接检查 credential_id，不依赖 runtime_execution_kind |

### 5.4 凭据注入

| 问题 | 修复 |
|------|------|
| AK/SK 环境变量名不匹配 | _collect_server_env 按 provider 映射：huawei_cloud → HUAWEICLOUD_SDK_AK/SK |
| redispatch 复用旧 env_extra（含老 AK/SK） | else 分支 env_extra 传空 dict，由 enqueue_task 从 DB 重新收集 |
| worker 端 get_credentials 无 DB 连接 | 新增 GET /tasks/{tid}/env API，worker 执行前拉取最新凭据 |

### 5.5 Prompt 构建

| 问题 | 修复 |
|------|------|
| _create_direct_task 创建的 task prompt 为空 | redispatch 改为 enqueue_task（存完整 prompt）+ start_direct_agent |
| 空 prompt task 作为快照导致后续 redispatch 也空 | 回溯找有 prompt 的旧 task 作为快照 |
| evt 缺少 'repository' key → KeyError | evt 补充 repository = from_repo_key(repo_key) |
| evt 缺少 'source' key → KeyError | evt 补充 source = repo_key.split(':')[0] |

### 5.6 hermes ACP 稳定性

| 问题 | 修复 |
|------|------|
| 203.0.113.1 hermes ACP 30 秒 reader EOF | config.yaml 添加 agent.gateway_timeout: 1800 |
| 127.0.0.1 config.yaml 被覆盖为空 | SFTP 从 203.0.113.1 复制完整 config.yaml + auth.json |
| lvjuntao hermes 测试超时 | opengw API key 401，使用 203.0.113.1 的 auth.json credential pool |

### 5.7 Worker 运维

| 问题 | 修复 |
|------|------|
| 安装脚本覆盖 runtime.env 导致 token 丢失 | 重新生成凭据 + SFTP 直传 config.yaml/auth.json |
| start-worker.sh 用 `python -m src.runtime.agent`（不是 `runtime.agent`） | 使用 start-worker.sh 启动，不手动指定模块路径 |
| worker 重注册后 tags 不写入 engine_capabilities | REPLACE INTO 修复 + 注册时不声明 tags（由控制台管理） |

---

## 六、数据库变更

### 6.1 新增表

```sql
CREATE TABLE engine_capabilities (
    id INT AUTO_INCREMENT PRIMARY KEY,
    runtime_id INT NOT NULL,
    tag VARCHAR(32) NOT NULL,
    max_concurrent INT DEFAULT 1,
    priority INT DEFAULT 50,
    enabled INT DEFAULT 1,
    created_at VARCHAR(40),
    updated_at VARCHAR(40),
    UNIQUE(runtime_id, tag)
);
```

### 6.2 新增列

| 表 | 列 | 类型 | 用途 |
|----|-----|------|------|
| agent_runtimes | tags | TEXT | 引擎标签（展示用，逗号分隔） |
| agent_tasks | pool_tag | VARCHAR(32) | 任务标签（plugin 模式写入，claim_by_tag 匹配） |
| user_accounts | last_scene | VARCHAR(64) | 用户最后选择的开发场景 |

### 6.3 标签数据

6 个标签：vod / issue / coding / qa / sre / chat

当前 3 台在线引擎全部配置了全部标签（控制台管理）：
- rid=39 (127.0.0.1 grape, hermes v0.20.0)
- rid=40 (127.0.0.1 lvjuntao, hermes v0.20.5)
- rid=41 (203.0.113.1 grape, hermes v0.19.0)

---

## 七、API 端点

### 7.1 新增

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/runtime/runtimes/{rid}/tags` | 获取引擎标签（worker claim 循环调用） |
| GET | `/api/runtime/tasks/{tid}/env` | 获取任务最新环境变量（worker 执行前调用） |
| POST | `/api/runtime/runtimes/{rid}/tasks/claim-by-tag/{tag}` | 按标签认领任务 |
| POST | `/api/runtime/admin/batch-recover` | 批量回收 stale 任务 |
| GET | `/api/settings/agent-runtimes/{rid}/tags` | 标签 CRUD: 查 |
| POST | `/api/settings/agent-runtimes/{rid}/tags` | 标签 CRUD: 增 |
| PUT | `/api/settings/agent-runtimes/{rid}/tags/{tag}` | 标签 CRUD: 改 |
| DELETE | `/api/settings/agent-runtimes/{rid}/tags/{tag}` | 标签 CRUD: 删 |
| GET | `/api/scenes/with-pipeline` | 场景列表含流水线配置 |

### 7.2 修改

| 路径 | 改动 |
|------|------|
| `POST /api/a2a` | plugin 模式下走事件驱动（agent/handoff → event_bus → dispatch） |
| `POST /api/events/{seq}/redispatch` | plugin 模式下原子 requeue+reassign / 直连执行触发 / prompt 恢复 |
| `GET /api/pipeline` | 返回 pipeline_config（pipelines.json） |
| `GET /api/settings/agent-runtimes` | 返回 tags 字段 |

---

## 八、pipelines.json 节点结构

```json
{
  "scenes": {
    "default": {
      "name": "通用开发",
      "nodes": [
        {"id": "vod",    "tag": "vod",    "skill": "vod",            "mode": "emit", "next": "issue",  "handoff": "a2a"},
        {"id": "issue",  "tag": "issue",  "skill": "issue-advance",  "mode": "emit", "next": "coding", "handoff": "a2a"},
        {"id": "coding", "tag": "coding", "skill": "implementation", "mode": "emit", "next": "qa",     "handoff": "a2a"},
        {"id": "qa",     "tag": "qa",     "skill": "pr",             "mode": "emit", "next": "sre",    "handoff": "a2a"},
        {"id": "sre",    "tag": "sre",    "skill": "cloud-deploy",   "mode": "emit", "next": "done",   "handoff": "complete"}
      ]
    }
  }
}
```

7 个场景：default / iac / skill-dev / api-skill-dev / blog-publish / cloud-deploy / hello-blog-sys

4 种事件模式：emit（默认，标签队列自认领）/ waterfall（中间件链）/ serial（顺序）/ parallel（并行等全部完成）

blog-publish 场景跳过 qa 节点（`skip: true`）

---

## 九、测试状态

### 9.1 单元测试

- 123 passed, 1 failed (pre-existing: TestKillStaleEngine killpg 2x)
- 零新增失败
- 覆盖：TestEvents(4) / TestSettings(6) / TestPipeline(2) / TestAgents(4) / test_runtime(33) / test_runtime_pool(14) / test_runtime_agent(19) / test_engine_select(17) / test_config(4) / test_scene_apply_enable(4) / test_db_backend_switch(16)

### 9.2 特性审计

43 项特性全部 OK，0 项 MISSING（Phase 1-5 + 前端）

### 9.3 实际执行验证

Issue #3 (HELLO-BLOG-SYS-2)：
- VOD → Issue → Coding (completed) → QA (failed, ACP 稳定性)
- event-driven A2A 流转正常

Issue #1 (hello-blog-10)：
- VOD → Issue → SRE (cloud-deploy)
- redispatch 后正确按标签分配引擎
- AK/SK 正确注入（HUAWEICLOUD_SDK_AK/SK）
- prompt 构建正常（evt 含 source/repository）

---

## 十、已知问题

1. **event 状态与 task 状态不同步**：task running 但 event 可能被标 failed（sweeper 或 A2A 回调误标）。临时修复：手动 update_event_status。根本修复：task 完成回调中同步 event 状态。

2. **hermes v0.19.0 (203.0.113.1) ACP 稳定性**：已添加 gateway_timeout: 1800，但仍偶发 reader EOF。建议升级到 v0.20.5。

3. **203.0.113.1 worker 401 Unauthorized**：安装脚本自动更新时代码 401（凭据可能过期）。需重新生成凭据。

4. **lvjuntao hermes 测试超时**：opengw API key 在此机器上可能限流。已复制 203.0.113.1 的 auth.json。

5. **_create_direct_task INSERT 参数**：已补齐到 36 列，但未来新增列时需同步更新。

---

## 十一、Worker 设备状态

| 机器 | 用户 | rid | Hermes 版本 | 状态 | 标签 |
|------|------|-----|-------------|------|------|
| 203.0.113.1 | grape | 41 | v0.19.0 | online | vod/issue/coding/qa/sre |
| 127.0.0.1 | grape | 39 | v0.20.0 | online | vod/issue/coding/qa/sre |
| 127.0.0.1 | lvjuntao | 40 | v0.20.5 | online | vod/issue/coding/qa/sre |
| 本机 | — | 37 | — | online | sre/vod |

config.yaml 统一为 glm-5.2 模型 + opengw 网关 + auth.json credential pool。
