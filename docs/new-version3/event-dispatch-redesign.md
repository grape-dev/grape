# Grape 事件调度模型重构设计 — 融合 DeepSeek Harness 插件化调度

## 一、现状分析

### 1.1 Grape 当前事件调度架构

```
                    ┌─────────────────────────────────────────────────────┐
                    │                  Grape 事件调度链路                    │
                    └─────────────────────────────────────────────────────┘

GitCode Webhook ──→ run_pipeline() ──→ 归一化 → 自循环检测 → 路由判定 → 事件存储 → 执行分派
                         │                                         │           │
                         │                                    issue_state     ├── direct 模式（本机线程池）
                         │                                    阶段矩阵         │     └── _execute_agent → stream_agent
                         │                                    静态映射         │
                         │                                    订阅匹配         ├── queue 模式（远端 worker）
                         │                                         │           │     └── enqueue_task → claim → _execute_task
                         │                                    A2A 端点          │
                         │                                    (POST /api/a2a)    │
                         │                                                         │
                    核心文件                                                      │
                    event_pipeline.py (1526行)                                    │
                    routing.py (静态映射)                                         │
                    webhook.py (A2A endpoint)                                     │
                    engine_select.py (引擎选择)                                   │
                    service.py (任务队列/claim)                                   │
                    executor.py (流式执行)                                        │
                    hermes_acp.py (ACP 协议)                                      │
                                                                                  ↓
                                                                            skill 执行
                                                                            (SKILL.md + prompt)
```

**核心数据流：**

```
1. Webhook 事件到达 → run_pipeline()
2. 归一化（normalize_event）→ 统一事件格式
3. 自循环检测 → bot 自身动作跳过
4. 路由判定：
   a. issue_opened → 恒给 vod
   b. Issue/PR 事件 → _resolve_issue_stage_agent() 按 issue_state 阶段矩阵路由
   c. 非 Issue 事件 → NON_ISSUE_EVENT_ROUTE 静态映射
   d. 订阅匹配 → _match_repo_subscription() 双路径并行
5. 事件存储 → append_event(status=running)
6. 引擎选择 → pick_engine() 按 专属→全局→本机 优先级
7. 执行分派：
   a. direct 模式 → _execute_agent() 在线程池中直接执行
   b. queue 模式 → enqueue_task() 入队，远端 worker claim
8. Agent 执行 → stream_agent() / hermes_acp → 产出事件
9. A2A 交办 → POST /api/a2a → 推进 issue_state 阶段
```

**issue_state 阶段机：**

```
vod → issue → coding → qa → sre → done
```

每个阶段的 Agent 完成后通过 A2A 调用推进到下一阶段。

### 1.2 当前架构的问题

| 问题 | 根因 | 影响 |
|------|------|------|
| 路由逻辑硬编码 | `_resolve_issue_stage_agent` 中 issue_state 阶段和 agent_type 的映射写死 | 新增 Agent 类型或自定义流程需改代码 |
| 事件类型静态映射 | `NON_ISSUE_EVENT_ROUTE` 字典硬编码 | 新增事件类型需改 routing.py |
| 执行与调度耦合 | `run_pipeline` 既做路由又做分派 | 调度逻辑 1526 行，难以维护和扩展 |
| 无事件中间件 | 事件到达后直接路由，无预处理/后处理钩子 | 无法插入审计、限流、转换等横切逻辑 |
| A2A 协议单一 | 只支持线性链式（vod→issue→coding→qa→sre） | 不支持并行 Agent、条件分支、回退 |
| 无插件化扩展 | Skill 只影响 prompt，不影响调度路由 | 新场景（如 blog-publish）需改 agent-skill-mapping.json + 代码 |

### 1.3 DeepSeek Harness 事件调度模型

DSH 的核心设计理念：**一切皆插件**，基于 Cordis 框架的时空可组合性。

```
┌─────────────────────────────────────────────────────────────────┐
│                    DSH 调度架构                                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Plugin Tree (Cordis Context)                                    │
│  ├── core/session    → ctx.sessions (append-only 事件日志)        │
│  ├── core/agent       → ctx.agents (Agent 注册表 + agent/* 事件)  │
│  ├── core/agent-loop  → ctx.agentLoop (默认驱动循环)              │
│  ├── core/tools       → ctx.tools (工具注册 + 执行管道)            │
│  ├── schedule         → 持久化定时提醒 (after/at/every)           │
│  ├── jobs             → ctx.jobs (后台作业注册表)                  │
│  ├── hooks            → Claude Code/Codex 钩子桥接                │
│  ├── workflow         → 工作流引擎 (worker-thread)                │
│  └── capability seams → fs/shell/subprocess/web/lsp/...           │
│                                                                  │
│  事件域 (3 层):                                                   │
│  1. Session events (持久) → session/event → 可重放               │
│  2. Agent events (运行时) → agent/created, agent/status, ...     │
│  3. Capability events → tools/*, fs/*, telemetry/*               │
│                                                                  │
│  Turn 流程:                                                      │
│  turn/start → claim input → agent/pre-step → step/start          │
│    → agent/request → llm/stream → assistant/chunk*               │
│    → tool/call* → tools/pre-execute → tools/execute              │
│    → tools/post-execute → tool/result* → step/end               │
│  → agent/turn-stopping → turn/end                                │
│                                                                  │
│  关键设计模式:                                                    │
│  - 瀑布式事件 (waterfall): listener 必须调 next() 委托            │
│  - 串行事件: agent/turn-stopping 无 next()                       │
│  - 会话日志即上下文: deriveMessages() 从日志投影模型历史           │
│  - 能力缝 (seam): Service Definition + Provider + Consumer       │
│  - 作用域隔离: per-agent scoped registrations                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**DSH 的 5 个关键调度概念：**

1. **Session Event Log（会话事件日志）**：append-only 的事件流，所有模型可见的信息都必须作为事件写入日志。`deriveMessages()` 从日志投影出模型历史。这保证了可重放性。

2. **Agent Registry + Agent Events**：Agent 是有生命周期的注册实体。`agent/created`、`agent/status`、`agent/pre-step` 等事件构成了运行时的扩展点。

3. **Turn/Step 模型**：
   - **Turn** = 零或多个 Step，从领取输入开始到不欠任何东西时关闭
   - **Step** = 一次模型请求 + 工具调用
   - 每个阶段都有持久事件（turn/start, step/start, assistant/message, tool/result, step/end, turn/end）

4. **Waterfall 事件**：`agent/pre-step`、`agent/request`、`tools/pre-execute` 等是瀑布式事件 — 监听器必须调用 `next()` 才能继续。这允许插件在管道任意环节拦截、修改或拒绝。

5. **Capability Seam（能力缝）**：三角色模式 — Service Definition（声明接口）+ Service Provider（实现）+ Consumer（使用，通常是模型工具）。换一个 Provider 就能改变整个执行环境（如本地→沙箱）。

---

## 二、融合设计方案

### 2.1 核心思路

**不推翻 Grape 现有架构，在其上新增一层插件化事件调度层**，吸收 DSH 的关键设计模式：

- 保留 `run_pipeline` 作为 webhook 入口（兼容现有 webhook 集成）
- 保留 `issue_state` 阶段机（兼容现有 Issue 工作流）
- 保留 `A2A` 协议（兼容现有 Agent 间通信）
- 保留 `pick_engine` + `enqueue_task` + `direct/queue` 双模式（兼容现有引擎体系）

**新增：**
- 事件总线（Event Bus）— 模拟 DSH 的 session/agent/capability 三层事件域
- 插件注册表（Plugin Registry）— 模拟 DSH 的 Cordis 插件树
- 事件中间件链（Middleware Chain）— 模拟 DSH 的 waterfall 事件
- 能力缝（Capability Seam）— 模拟 DSH 的 Service Definition/Provider/Consumer
- Turn/Step 执行模型 — 模拟 DSH 的结构化执行生命周期

### 2.2 架构分层

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Grape 融合调度架构                                   │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  Layer 4: 入口层 (保留)                                                │
│  ├── GitCode Webhook → run_pipeline()                                 │
│  ├── A2A Endpoint → POST /api/a2a                                     │
│  └── Chat API → /api/chat/send                                        │
│                                                                       │
│  Layer 3: 事件总线层 (新增) ── 吸收 DSH session/agent events            │
│  ├── EventBus (pub/sub)                                               │
│  │   ├── event/normalize  → 归一化后通知                               │
│  │   ├── event/route      → 路由判定后通知                             │
│  │   ├── event/dispatch   → 分派前通知 (waterfall: 可拦截/修改)        │
│  │   ├── event/execute    → 执行开始/结束通知                          │
│  │   └── event/complete   → 完成后通知 (持久)                          │
│  ├── Middleware Chain (waterfall)                                     │
│  │   ├── 审计中间件 (记录所有事件)                                     │
│  │   ├── 限流中间件 (per-repo 并发控制)                                │
│  │   ├── 转换中间件 (事件格式适配)                                     │
│  │   └── 自定义中间件 (用户扩展)                                       │
│  └── Event Log (append-only, 可重放)                                   │
│                                                                       │
│  Layer 2: 调度核心层 (重构) ── 吸收 DSH agent registry + turn/step      │
│  ├── PluginRegistry (插件注册表)                                       │
│  │   ├── RoutePlugin (替代 _resolve_issue_stage_agent + 静态映射)      │
│  │   ├── DispatchPlugin (替代 direct/queue 选择逻辑)                   │
│  │   ├── EnginePlugin (替代 pick_engine)                               │
│  │   └── SkillPlugin (替代 agent-skill-mapping.json)                   │
│  ├── AgentRegistry (Agent 注册表)                                      │
│  │   ├── register(agent_type, config)                                 │
│  │   ├── lifecycle: created → active → idle → disposed                │
│  │   └── events: agent/created, agent/status, agent/handoff           │
│  ├── TurnExecutor (结构化执行)                                         │
│  │   ├── turn/start → step/start → stream → tool/call → step/end      │
│  │   │                                      → turn/end                 │
│  │   └── 每个阶段都有事件 (可被中间件拦截)                              │
│  └── IssueStateMachine (保留 issue_state，重构为可配置)                 │
│      ├── 状态: vod → issue → coding → qa → sre → done                 │
│      ├── 转换规则可配置 (JSON, 不再硬编码)                              │
│      └── 支持自定义流程 (并行/分支/回退)                                │
│                                                                       │
│  Layer 1: 执行层 (保留 + 适配)                                         │
│  ├── ExecutorAdapter (统一接口)                                        │
│  │   ├── HermesACPBackend (现有 hermes_acp.py)                        │
│  │   ├── OpencodeBackend (现有 executor.py stream_agent)              │
│  │   └── FutureBackend (未来扩展)                                     │
│  ├── EngineSelector (现有 engine_select.py, 接口化)                    │
│  ├── TaskQueue (现有 service.py, 接口化)                               │
│  └── CapabilitySeam (新增)                                             │
│      ├── GitCodeAPI (Service Definition + Provider)                   │
│      ├── CloudAPI (华为云 SDK)                                        │
│      ├── FileSystem (工作目录访问)                                     │
│      └── A2A (Agent 间通信)                                            │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.3 事件总线设计

模拟 DSH 的三层事件域，在 Grape 中新增事件总线：

```python
# src/api/event_bus.py (新增)

class EventDomain(enum.Enum):
    PIPELINE = "pipeline"    # 对应 DSH session events（持久）
    AGENT = "agent"          # 对应 DSH agent events（运行时）
    CAPABILITY = "capability" # 对应 DSH capability events

class GrapeEvent:
    """统一事件模型"""
    domain: EventDomain
    type: str              # event/normalize, event/route, agent/created, ...
    seq: int               # 事件序号
    repo_key: str
    issue_id: str
    agent_type: str
    payload: dict
    timestamp: str
    # 持久域事件自动写入 event_log

class EventBus:
    """事件总线 — pub/sub + waterfall 中间件链"""

    def __init__(self):
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._middleware: list[Middleware] = []
        self._log: EventLog = EventLog()  # append-only

    def on(self, event_type: str, listener: Callable, priority: int = 0):
        """注册监听器（模拟 DSH ctx.on()）"""
        self._listeners[event_type].append((priority, listener))

    def use(self, middleware: Middleware):
        """注册中间件（模拟 DSH waterfall）"""
        self._middleware.append(middleware)

    async def emit(self, event: GrapeEvent):
        """发射事件 → 中间件链 → 监听器"""
        # 中间件链 (waterfall)
        for mw in self._middleware:
            result = await mw(event)
            if result is False:  # 拦截
                return
        # 持久域事件写入日志
        if event.domain == EventDomain.PIPELINE:
            self._log.append(event)
        # 通知监听器
        for _, listener in sorted(self._listeners.get(event.type, [])):
            await listener(event)

    def replay(self, since: int = 0) -> list[GrapeEvent]:
        """从日志重放事件（模拟 DSH session log replay）"""
        return self._log.query(since=since)
```

**事件类型清单：**

```
Pipeline 域 (持久):
  event/normalize    — 事件归一化完成
  event/route        — 路由判定完成
  event/dispatch     — 执行分派前 (waterfall: 可拦截/修改)
  event/execute      — Agent 执行开始
  event/complete     — Agent 执行完成
  event/fail         — Agent 执行失败
  event/skip         — 事件被跳过

Agent 域 (运行时):
  agent/created      — Agent 实例创建
  agent/active       — Agent 开始执行
  agent/idle         — Agent 空闲
  agent/handoff      — A2A 交办 (替代直接调 POST /api/a2a)
  agent/terminated   — Agent 被终止

Capability 域:
  tools/pre-execute  — 工具执行前 (waterfall)
  tools/execute      — 工具执行中
  tools/post-execute — 工具执行后
  api/pre-call       — GitCode API 调用前 (waterfall)
  api/post-call      — GitCode API 调用后
```

### 2.4 插件注册表设计

模拟 DSH 的 Cordis 插件树，在 Grape 中新增插件注册表：

```python
# src/api/plugin_registry.py (新增)

class PluginSpec(BaseModel):
    """插件规格声明"""
    name: str                      # 插件名
    version: str
    events: list[str]              # 监听的事件类型
    priority: int = 0              # 加载优先级
    config_schema: dict = {}       # 配置项 schema
    capability: str = ""           # 提供的能力缝名

class GrapePlugin(ABC):
    """插件基类 — 模拟 DSH Cordis plugin"""

    @abstractmethod
    def spec(self) -> PluginSpec: ...

    @abstractmethod
    async def apply(self, ctx: PluginContext):
        """注册事件监听器/中间件/能力缝"""
        ...

class PluginRegistry:
    """插件注册表 — 模拟 DSH Context 插件树"""

    def __init__(self, event_bus: EventBus):
        self._plugins: dict[str, GrapePlugin] = {}
        self._capabilities: dict[str, list[Provider]] = defaultdict(list)
        self._event_bus = event_bus

    def register(self, plugin: GrapePlugin, config: dict = None):
        """注册插件"""
        spec = plugin.spec()
        ctx = PluginContext(
            event_bus=self._event_bus,
            config=config or {},
            capabilities=self._capabilities
        )
        plugin.apply(ctx)
        self._plugins[spec.name] = plugin

    def get_capability(self, name: str) -> Provider | None:
        """获取能力缝的当前 Provider"""
        providers = self._capabilities.get(name, [])
        return providers[0] if providers else None
```

**内置插件：**

| 插件名 | 替代的现有代码 | 说明 |
|--------|---------------|------|
| `routing-plugin` | `_resolve_issue_stage_agent` + `NON_ISSUE_EVENT_ROUTE` | 事件路由，可配置规则 |
| `engine-plugin` | `pick_engine` + `engine_select.py` | 引擎选择，可配置策略 |
| `dispatch-plugin` | direct/queue 选择逻辑 | 执行分派，可配置模式 |
| `skill-plugin` | `agent-skill-mapping.json` | Skill 映射，可配置场景 |
| `audit-plugin` | (无) | 审计日志（新增） |
| `throttle-plugin` | (无) | 并发限流（新增） |
| `issue-state-plugin` | `issue_state` 表 + `set_issue_stage` | Issue 生命周期，可配置阶段 |

### 2.5 路由插件化设计

将 `_resolve_issue_stage_agent` 和 `NON_ISSUE_EVENT_ROUTE` 重构为可配置的路由插件：

```python
# src/api/plugins/routing_plugin.py (新增)

class RoutingPlugin(GrapePlugin):
    """路由插件 — 替代 _resolve_issue_stage_agent + 静态映射"""

    def spec(self) -> PluginSpec:
        return PluginSpec(
            name="routing",
            version="1.0.0",
            events=["event/normalize"],
            priority=10,  # 路由是第一个中间件
        )

    async def apply(self, ctx: PluginContext):
        config = ctx.config

        @ctx.event_bus.on("event/normalize")
        async def route(event: GrapeEvent):
            event_type = event.payload.get("type")
            repo_key = event.repo_key

            # 1. 路由规则链（可配置，替代硬编码）
            route_rules = config.get("rules", DEFAULT_ROUTE_RULES)
            for rule in route_rules:
                if self._match(rule, event_type, event.payload):
                    event.agent_type = rule["agent"]
                    event.metadata["route_reason"] = rule.get("reason", "rule_match")
                    break

            # 2. issue_state 阶段矩阵（可配置，替代硬编码）
            if event.issue_id and event.agent_type:
                stage = await self._get_issue_stage(repo_key, event.issue_id)
                event.agent_type = self._resolve_stage_agent(
                    stage, event_type, route_rules
                )

            # 3. 发射路由完成事件
            await ctx.event_bus.emit(GrapeEvent(
                domain=EventDomain.PIPELINE,
                type="event/route",
                seq=event.seq,
                repo_key=repo_key,
                agent_type=event.agent_type,
                payload=event.payload,
            ))
```

**路由规则配置（JSON，替代硬编码）：**

```json
// configs/route-rules.json (新增)
{
  "rules": [
    {
      "match": {"event_type": "issue_opened"},
      "agent": "vod",
      "reason": "issue_opened恒给vod"
    },
    {
      "match": {"event_type": "issue_comment", "issue_stage": "vod"},
      "agent": "vod",
      "reason": "VOD阶段评论给VOD"
    },
    {
      "match": {"event_type": "issue_comment", "issue_stage": ["issue","coding","qa","sre"]},
      "agent": "issue",
      "reason": "非VOD阶段评论给IssueAgent"
    },
    {
      "match": {"event_type": "pull_request_merged", "issue_stage": "coding"},
      "agent": "qa",
      "reason": "PR合入在coding阶段→QA审查"
    },
    {
      "match": {"event_type": "pull_request_merged", "issue_stage": "qa"},
      "agent": "sre",
      "reason": "PR合入在qa阶段→SRE部署"
    },
    {
      "match": {"event_type": "build_failure"},
      "agent": "sre",
      "reason": "构建失败→SRE"
    },
    {
      "match": {"event_type": "vod_signal"},
      "agent": "vod",
      "reason": "VOD信号→VOD"
    }
  ],
  "default_agent": "",
  "issue_stages": ["vod", "issue", "coding", "qa", "sre", "done"],
  "stage_transitions": {
    "vod": {"next": "issue", "via": "a2a"},
    "issue": {"next": "coding", "via": "a2a"},
    "coding": {"next": "qa", "via": "a2a"},
    "qa": {"next": "sre", "via": "a2a"},
    "sre": {"next": "done", "via": "complete"}
  }
}
```

### 2.6 Turn/Step 执行模型

模拟 DSH 的 Turn/Step 结构化执行，为 Grape 的 Agent 执行增加生命周期事件：

```python
# src/api/turn_executor.py (新增)

class TurnExecutor:
    """结构化执行 — 模拟 DSH turn/step 模型"""

    def __init__(self, event_bus: EventBus, executor_adapter: ExecutorAdapter):
        self._bus = event_bus
        self._adapter = executor_adapter

    async def execute_turn(self, task: TaskContext) -> TurnResult:
        """执行一个 Turn = 零或多个 Step"""
        turn_id = generate_id()

        # turn/start (持久)
        await self._bus.emit(GrapeEvent(
            domain=EventDomain.PIPELINE,
            type="turn/start",
            seq=task.event_seq,
            turn_id=turn_id,
            agent_type=task.agent_type,
            repo_key=task.repo_key,
            issue_id=task.issue_id,
        ))

        steps = []
        try:
            # 流式执行，每个工具调用是一个 step
            async for event in self._adapter.stream_execute(task.prompt, task.cwd, task.env):
                if event.type == "tool_use":
                    # step/start
                    step_id = generate_id()
                    await self._bus.emit(GrapeEvent(
                        domain=EventDomain.AGENT,
                        type="step/start",
                        turn_id=turn_id,
                        step_id=step_id,
                        payload={"tool": event.tool, "input": event.input},
                    ))

                    # tools/pre-execute (waterfall: 中间件可拦截)
                    proceed = await self._bus.emit_waterfall(
                        "tools/pre-execute",
                        GrapeEvent(domain=EventDomain.CAPABILITY,
                                   type="tools/pre-execute",
                                   step_id=step_id,
                                   payload={"tool": event.tool, "input": event.input})
                    )
                    if not proceed:
                        continue  # 被中间件拦截

                    # tools/execute
                    result = await self._adapter.execute_tool(event.tool, event.input)
                    await self._bus.emit(GrapeEvent(
                        domain=EventDomain.CAPABILITY,
                        type="tools/execute",
                        step_id=step_id,
                        payload=result,
                    ))

                    # step/end
                    await self._bus.emit(GrapeEvent(
                        domain=EventDomain.AGENT,
                        type="step/end",
                        step_id=step_id,
                        payload=result,
                    ))
                    steps.append(result)

                elif event.type == "text":
                    # assistant message chunk
                    await self._bus.emit(GrapeEvent(
                        domain=EventDomain.PIPELINE,
                        type="assistant/chunk",
                        turn_id=turn_id,
                        payload={"text": event.data},
                    ))

                elif event.type == "done":
                    break

        except Exception as e:
            await self._bus.emit(GrapeEvent(
                domain=EventDomain.PIPELINE,
                type="turn/fail",
                turn_id=turn_id,
                payload={"error": str(e)},
            ))
            return TurnResult(success=False, error=str(e))

        # turn/end (持久)
        await self._bus.emit(GrapeEvent(
            domain=EventDomain.PIPELINE,
            type="turn/end",
            turn_id=turn_id,
            payload={"steps": len(steps), "result": task.result},
        ))

        return TurnResult(success=True, steps=steps)
```

### 2.7 A2A 升级为事件驱动

将 A2A 从直接 HTTP 调用升级为事件驱动：

```python
# 现状：Agent 执行完后直接调 POST /api/a2a
# 新方案：Agent 发出 agent/handoff 事件，由调度核心决定下一步

@ctx.event_bus.on("agent/handoff")
async def on_handoff(event: GrapeEvent):
    """A2A 事件处理 — 推进 issue_state + 触发下一个 Agent"""
    target_agent = event.payload.get("target_agent")
    issue_id = event.issue_id
    repo_key = event.repo_key

    # 1. 推进 issue_state
    stage_config = get_stage_config()
    current_stage = await get_issue_stage(repo_key, issue_id)
    transition = stage_config["stage_transitions"].get(current_stage)
    if transition and transition["via"] == "a2a":
        next_stage = transition["next"]
        await set_issue_stage(repo_key, issue_id, next_stage, target_agent)

    # 2. 发射 dispatch 事件 (waterfall: 中间件可决定是否分派)
    await ctx.event_bus.emit(GrapeEvent(
        domain=EventDomain.PIPELINE,
        type="event/dispatch",
        repo_key=repo_key,
        issue_id=issue_id,
        agent_type=target_agent,
        payload=event.payload,
    ))
```

### 2.8 能力缝设计

模拟 DSH 的 Capability Seam，将 Grape 中的外部依赖抽象为可替换的能力缝：

```python
# src/api/capability.py (新增)

class CapabilitySeam(ABC):
    """能力缝基类 — 模拟 DSH Service Definition"""
    name: str

    @abstractmethod
    async def execute(self, action: str, params: dict) -> dict: ...

class GitCodeAPIProvider(CapabilitySeam):
    """GitCode API Provider — 现有 webhook.py + user/db.py 中的 API 调用"""
    name = "gitcode_api"

    async def execute(self, action: str, params: dict) -> dict:
        if action == "create_issue":
            return await self._create_issue(params)
        elif action == "add_comment":
            return await self._add_comment(params)
        elif action == "add_label":
            return await self._add_label(params)
        # ...

class CloudAPIProvider(CapabilitySeam):
    """华为云 API Provider — 现有 iac skills 中的 hcloud 调用"""
    name = "cloud_api"

class FileSystemProvider(CapabilitySeam):
    """文件系统 Provider — 工作目录访问"""
    name = "filesystem"

class A2AProvider(CapabilitySeam):
    """A2A Provider — Agent 间通信"""
    name = "a2a"

# 注册：插件可以替换任意 Provider
# 例如：测试时替换 GitCodeAPIProvider 为 MockProvider
```

---

## 三、兼容性设计

### 3.1 双模式运行

```python
# src/api/event_pipeline.py (重构)

# 全局开关：是否启用插件化调度
_PLUGIN_MODE = os.environ.get("GRAPE_PLUGIN_DISPATCH", "false") == "true"

def run_pipeline(raw_body, headers, account="", repo_key_override=""):
    if _PLUGIN_MODE:
        return _run_pipeline_plugin(raw_body, headers, account, repo_key_override)
    else:
        return _run_pipeline_legacy(raw_body, headers, account, repo_key_override)

def _run_pipeline_legacy(raw_body, headers, account, repo_key_override):
    """现有逻辑，完全保留"""
    # ... 现有 1526 行代码不变 ...

def _run_pipeline_plugin(raw_body, headers, account, repo_key_override):
    """插件化调度"""
    # 1. 归一化 (复用现有 normalize_event)
    evt = normalize_event(raw_body, event_header, source)

    # 2. 发射 normalize 事件 → 路由插件处理
    event = GrapeEvent(
        domain=EventDomain.PIPELINE,
        type="event/normalize",
        payload=evt,
        repo_key=repo_key,
    )
    await event_bus.emit(event)

    # 3. 路由插件已设置 event.agent_type
    # 4. 发射 dispatch 事件 → 分派插件处理
    await event_bus.emit(GrapeEvent(
        domain=EventDomain.PIPELINE,
        type="event/dispatch",
        agent_type=event.agent_type,
        # ...
    ))

    # 5. 分派插件选择执行模式 (direct/queue)
    # 6. TurnExecutor 执行
    # 7. 完成后发射 complete 事件
```

### 3.2 渐进迁移路径

```
Phase 1: 引入事件总线 (不改变现有逻辑)
  ├── 新增 event_bus.py, plugin_registry.py
  ├── 在 run_pipeline 关键节点发射事件 (旁路, 不影响执行)
  └── 现有代码完全不变

Phase 2: 路由插件化 (可配置替代硬编码)
  ├── 新增 routing_plugin.py + route-rules.json
  ├── _PLUGIN_MODE 开关切换
  └── 现有 _resolve_issue_stage_agent 保留为 fallback

Phase 3: Turn/Step 执行模型 (结构化执行)
  ├── 新增 turn_executor.py
  ├── 在 executor.py 和 hermes_acp.py 适配 Turn/Step 事件
  └── task_messages 表兼容 (事件日志自动同步到 task_messages)

Phase 4: 能力缝抽象 (可替换 Provider)
  ├── GitCodeAPI / CloudAPI / FileSystem 抽象为 CapabilitySeam
  └── 现有 executor 中的直接 API 调用改为通过能力缝

Phase 5: 完全插件化 (移除 legacy)
  ├── 删除 _run_pipeline_legacy
  ├── 删除 _resolve_issue_stage_agent
  └── 所有逻辑由插件驱动
```

### 3.3 数据兼容

| 现有表/文件 | 改动 | 兼容方式 |
|------------|------|---------|
| `events` 表 | 不变 | 事件总线写入 events 表（同一格式） |
| `issue_state` 表 | 不变 | issue_state 插件读写同一表 |
| `agent_tasks` 表 | 不变 | dispatch 插件写入同一表 |
| `task_messages` 表 | 不变 | Turn/Step 事件自动同步到 task_messages |
| `agent-skill-mapping.json` | 不变 | skill 插件读取同一文件 |
| `configs/route-rules.json` | 新增 | 路由插件配置，缺失时回退到硬编码 |

---

## 四、DSH vs Grape 对比总结

| 维度 | DSH | Grape 现状 | Grape 融合后 |
|------|-----|-----------|-------------|
| 架构模式 | 插件树 (Cordis) | 单体管道 (run_pipeline) | 双模式（插件 + legacy fallback） |
| 事件系统 | 3 层事件域 + waterfall | 无事件系统 | 3 层事件域 + waterfall 中间件 |
| 路由机制 | 插件注册 | 硬编码阶段矩阵 + 静态映射 | 可配置路由规则 (JSON) |
| 执行模型 | Turn/Step 结构化 | 直接 stream_agent | Turn/Step + 保留 stream_agent |
| Agent 通信 | agent/* 事件 | A2A HTTP 调用 | agent/handoff 事件 + 保留 A2A API |
| 能力抽象 | Capability Seam | 直接调用 | Capability Seam + 保留直接调用 |
| 扩展方式 | 加插件 | 改代码 | 加插件 或 改代码 |
| 会话日志 | append-only event log | events 表 | event log + events 表同步 |
| 可重放性 | 完全可重放 | 不可重放 | 持久域事件可重放 |

---

## 五、改动清单

### 5.1 新增文件

| 文件 | 说明 | Phase |
|------|------|-------|
| `src/api/event_bus.py` | 事件总线 + 中间件链 | 1 |
| `src/api/plugin_registry.py` | 插件注册表 | 1 |
| `src/api/turn_executor.py` | Turn/Step 执行模型 | 3 |
| `src/api/capability.py` | 能力缝基类 | 4 |
| `src/api/plugins/routing_plugin.py` | 路由插件 | 2 |
| `src/api/plugins/dispatch_plugin.py` | 分派插件 | 2 |
| `src/api/plugins/engine_plugin.py` | 引擎选择插件 | 2 |
| `src/api/plugins/skill_plugin.py` | Skill 映射插件 | 2 |
| `src/api/plugins/audit_plugin.py` | 审计插件 | 1 |
| `src/api/plugins/throttle_plugin.py` | 限流插件 | 1 |
| `src/api/plugins/issue_state_plugin.py` | Issue 状态机插件 | 2 |
| `configs/route-rules.json` | 路由规则配置 | 2 |

### 5.2 修改文件

| 文件 | 改动 | Phase |
|------|------|-------|
| `src/api/event_pipeline.py` | 新增 `_PLUGIN_MODE` 开关 + `_run_pipeline_plugin` | 2 |
| `src/api/main.py` | 启动时初始化 EventBus + PluginRegistry | 1 |
| `src/agent/executor.py` | 适配 Turn/Step 事件发射 | 3 |
| `src/agent/hermes_acp.py` | 适配 Turn/Step 事件发射 | 3 |
| `src/api/routes/webhook.py` | A2A 端点发射 agent/handoff 事件 | 3 |

### 5.3 不改的文件

| 文件 | 原因 |
|------|------|
| `src/api/event_store.py` | 事件存储格式不变 |
| `src/api/session_store.py` | issue_state 表结构不变 |
| `src/runtime/service.py` | 任务队列/claim 逻辑不变 |
| `src/runtime/engine_select.py` | 引擎选择逻辑不变（插件化只是包装） |
| `src/runtime/agent.py` | worker 端代码不变 |
| `configs/agent-skill-mapping.json` | skill 映射格式不变 |
| 所有 skill SKILL.md | Agent 技能不变 |

---

## 六、关键设计决策

### 6.1 为什么不直接用 Cordis？

- Grape 是 Python 后端，Cordis 是 TypeScript 框架
- Cordis 的时空可组合性依赖 fiber 模型，Python 没有原生对应
- Grape 的核心价值在 DevOps Agent 工作流，不需要通用 Agent 框架的全部能力
- 融合方案吸收 DSH 的设计模式（事件域、waterfall、能力缝、Turn/Step），用 Python 原生实现

### 6.2 为什么保留 legacy 模式？

- 现有 1500+ 行 event_pipeline.py 经过大量实战检验
- 渐进迁移降低风险：每个 Phase 可独立上线、独立回滚
- 双模式运行期间可以 A/B 对比验证插件化调度的正确性

### 6.3 为什么路由规则用 JSON 配置？

- DSH 用 Cordis 插件配置（YAML），Grape 已有 `agent-skill-mapping.json` 先例
- JSON 配置可以被插件热加载（不需重启服务）
- 现有 `NON_ISSUE_EVENT_ROUTE` 字典本身就是配置数据的硬编码版本
- 未来可以扩展为 DB 存储（每仓库自定义路由规则）

### 6.4 为什么 Turn/Step 不替代 stream_agent？

- stream_agent 已经适配了 hermes ACP + opencode 两种引擎
- Turn/Step 是在 stream_agent 之上增加事件发射层（装饰器模式）
- 现有的 task_messages 表、SSE 流、前端实时日志全部兼容
- Turn/Step 事件自动同步到 task_messages，前端无需改动
