# Issue #470 — 写死运行配置抽取（超时家族 + DevStage）

> 目标分支：`weekly-09` | 开发分支：`feat/issue-470` | 优先级 P2 | 2026-10-26
> Spec 来源：Issue 评论 196043970（issue agent 深度可开发性分析，评级 ready）+ 用户补充「不仅放到配置文件里，还要放到小管理台里」

## 1. 需求

把 grape 中写死的运行配置抽到配置文件 + 小管理台（admin 配置中心），做到不改代码即可按环境生效：

**① RUNTIME_TASK_TIMEOUT 超时家族（默认 3600s）**
- `src/runtime/agent.py:2340` worker 任务看门狗 `_env_float("RUNTIME_TASK_TIMEOUT", 3600)`
- `src/agent/hermes_acp.py:46` / `src/agent/hwcloud_acp.py:55` 引擎内循环全局超时 `os.environ.get("RUNTIME_TASK_TIMEOUT", "3600")`
- 派生阈值：`src/observability/__init__.py:462` `_STUCK_MINUTES=22`、`src/api/routes/dashboard.py:134`（22min 口径）、`src/runtime/cleanup.py:23`（6h 孤儿判定 `SWEEPER_LONG_RUNNING_MINUTES`）

**② DevStage（AI DevSpace）配置 `src/api/routes/devstage.py`**
- 模板 ID `4ef59a2b4d3d4909aacd80f6007c00b2`（模块级常量 + create_environment 内重复写死两处）→ 收敛单一定义
- `flavor_id=23ebdfe3b2d34cc4b34a1aaf03d27c54`、`cores=3`、`memory=6`、`arch="ARM"`（现有 `GRAPE_DEVSTAGE_*` env 覆盖保留）

## 2. 关键约束（验收标准）

- [ ] 超时家族默认值统一收敛，改配置文件即可生效，env 覆盖保留
- [ ] DevStage 模板 ID / flavor_id / cores / memory / arch 可经配置文件 + 管理台调整，默认行为不变（3c6g、现有模板、ARM）
- [ ] 管理台可查看并修改上述配置，修改后按优先级生效：env 覆盖 > 管理台值 > 配置文件默认值
- [ ] 单测覆盖：配置读取、非法值回退、默认值兼容、优先级
- [ ] 现有测试套件通过，无行为回归

## 3. 事实澄清（考察结论）

- worker 侧（`runtime/agent.py`、`hermes_acp`、`hwcloud_acp`）跑在远端机器，**不共享服务端 configs/ 目录**；但 `env_extra`（任务派发时服务端写入 agent_tasks.env_extra JSON 列）会经 claim 下发到 worker，`_execute_task` 里 `env.update` 后经 `stream_agent(env_extra)` 传给 ACP 后端的 `stream_execute(env=...)`——#378 静默超时差异化已用此链路（服务端 setdefault 注入、底层 env 优先、os.environ 次之）。RUNTIME_TASK_TIMEOUT 走同链路即可让「管理台改超时 → 新任务生效」。
- worker 看门狗 `agent.py:2340` 在 worker 进程内读 env；ACP 适配器 `_global_timeout_for(max_exec_seconds)` 也是 env 读。两处改为「任务级 env 优先（`_execute_task` 注入）→ os.environ → 默认」，与服务端配置下发对齐。
- 派生阈值属于服务端进程：stuck 22min / 孤儿 6h 直读新配置（管理台 meta 或配置文件）。
- 管理台已有配置中心（`/api/admin/config` GET/PUT，DB meta 表，脱敏读、增量写、集群共享即时生效）；`chat_stream_timeout` 先例（server-config.json 动态读 + env 回退）证明「配置文件 + env 兜底」模式。
- DevStage 现有 `devstage_base_url` meta 先例（`_resolve_base_url`：env > meta > 默认）。
