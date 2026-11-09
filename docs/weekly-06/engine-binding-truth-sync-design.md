# 引擎绑定真实性同步：离线提示 + 8h 自动解绑 + 失败原因落库 设计方案

> 状态：待评审（v2.1，v2 基础上补两表协同一节：autopilot 任务绑定提示联动）
> 生成日期：2026-10-09
> 分支：feat/autopilot-kill-sync-i18n
> 关联事件：9336（"刘菲的专属开发流水线"场景绑定引擎全离线导致任务反复失败）
> 关联历史 Issue：#249（前端过滤离线引擎徽章显示）、#259（方案A：绑定引擎全灭→事件 failed 不广播）、#250（删除 runtime 时清理场景悬空绑定）

---

## 1. 背景与问题

事件 9336 排障发现的**信息不一致**问题：前端与后端对"场景绑定了哪些引擎"持有两套事实。

**现状链路**：

1. 场景 `nodes_json` 里 coding/qa/sre 节点绑定 `engine_runtime_ids=[2629]`（build-005 的 opencode）。
2. build-005 的 runtime-agent 令牌失效 → 注册 401 → 崩溃循环 → 引擎长期离线。
3. **后端**（`engine_select.py:get_scene_pool_engine_ids`，Issue #259 方案A）：派发时发现绑定的引擎全部离线 → 返回 `None` → 调用点不入队、事件置 `failed`。行为正确（fail fast），但部分调用点是裸 `update_event_status(seq, "failed")`，**无原因落库**，排障需要人工倒查。
4. **前端**（`dashboard.js:_filterAvailableEngineIds`，Issue #249）：把离线引擎从展示中滤掉，`[2629]` 滤空后渲染绿色"广播"徽章。**纯化妆**——不回写数据、不影响后端。同类过滤在 `scene-market.js:1144/1503` 还有两处。

**结果**：界面说"会广播"，后端说"绑定的引擎全死，拒绝干活"。用户以为任务会发到在线机器；实际每次派发都撞死在同一台离线引擎上。

**根因**：#249 用"隐藏"代替"如实呈现"；而数据库里的绑定长期悬空无人清理（#250 只处理物理删除，不处理长期离线）。

## 2. 方案目标

1. 前端显示与后端数据**一致**：绑定失效就是红色提示，不再显示假"广播"。
2. 长期离线的引擎，后端**真实清理**场景绑定（DB 内容真实改变，不是表面工程）。
3. 系统改动**留痕 + 告知**：自动解绑有审计记录，用户打开场景能看到"哪些引擎被系统移除了"。
4. 事件失败**带人话原因**：点开失败事件即见"绑定的 3 台引擎（build-005 等）全部离线"，不用倒查日志。

## 3. 已确认决策（与主人对齐）

| # | 决策 | 说明 |
|---|------|------|
| 1 | 短时离线**不删**绑定，只前端红灯 | 网络抖动/滚动重启是瞬态，秒级删绑会把"专属引擎"静默变成"全广播"。失败不兜底原则 |
| 2 | 离线超过 **8 小时**判定真死，后端真删绑定 | 心跳周期 15s，8h 无恢复 = 连续 ~1920 个心跳周期全丢，机器不会自己回来了（主人拍板，v1 的 24h 过久）。阈值可配：环境变量 `ENGINE_DETACH_AFTER_SECONDS`，默认 28800 |
| 3 | 引擎复活**不自动绑回** | 诚实行为：用户显式重绑。场景编辑页展示被移除清单 |
| 4 | 徽章三态 | 有在线绑定→"N 台引擎"；绑定部分离线→"5 台引擎 · 4 离线"（红）；绑定全灭→红色失效提示；未配引擎→绿色"广播" |
| 5 | 失败原因写入事件 `result` 字段 | 复用现有字段与既有展示通路，不加列 |

## 4. 设计

### 4.1 阶段一：短时离线（< 8h）——只提示，不动数据

纯前端改动，DB 不变：

- `dashboard.js` `_filterAvailableEngineIds` 改为不过滤，返回 `{total, offline}` 计数，徽章按决策 4 渲染三态。
- `scene-market.js` 两处同样处理。
- 三处渲染点抽成一个共享 helper（挂 `window._engineBadge`，dashboard.js 定义、scene-market.js 复用），避免三份拷贝。
- 新增 i18n key（中文文案，注意 t(key) 找不到返回 key 本身的陷阱）。

### 4.2 阶段二：离线超 8h——后端真删绑定

**触发点**：挂在 `main.py` 的 reap 循环（60s 周期，`reap_stale_runtimes` 内已有 `_check_stale_queued_tasks` 先例）——新增 `_detach_dead_engine_bindings()`，与既有检查并列调用。

**判定**：引擎心跳失联（`last_seen_at` 距今 > 100s，与 `_RUNTIME_STALE_SECONDS` 同尺）**且持续超过 8h**（`last_seen_at` 距今 > `ENGINE_DETACH_AFTER_SECONDS`，且期间无任何心跳更新）。

**动作**（对每台超时引擎）：

1. 遍历所有 `devops_pipeline_scene.nodes_json`，从各 node 的 `engine_runtime_ids` 移除该 runtime_id——复用 `_cleanup_scene_engine_runtime_ids`（settings.py:956）的逻辑，抽成共享函数移到 `engine_select.py`。
2. `agent_runtimes` 该行写 `detached_from_scenes_at` 时间戳（新列，`_ensure_column` 幂等迁移）——作为"已处理"标记，重复执行自动跳过，天然幂等。
3. 审计表 `scene_engine_detach_log` 插记录：`(scene_row_id, repo_key, scene_id, node_tag, runtime_id, runtime_name, detached_at, reason)`。
4. 逐场景独立 try/except（同 #250 模式），单场景写失败不阻塞整体。

**不删的**：
- `agent_runtimes` 行本身保留（status=offline，避免历史任务 runtime_id 悬空，与 #250 物理删除语义区分）。
- **`autopilot_tasks.engine_runtime_ids` 不清**（详见 §5.2 任务级绑定划界）。

### 4.3 阶段三：引擎复活——不自动绑回，显式提示

场景详情/编辑接口（`get_scene_market_by_id` / `agents.py` 场景 node 读取处）返回时联查 `scene_engine_detach_log`，带上 `detached_engines: [{name, tag, at}]`。

前端编辑页渲染提示条："以下引擎因离线超过 8 小时被系统移除：build-005（coding/qa/sre），如需使用请重新绑定。"

用户重绑后，该场景的 detach_log 标记 `rebound_at`（不再提示）。

### 4.4 失败原因落库（v2 修订：真实缺口比 v1 评估的小）

**排查发现：主派发路径已写原因**。`event_pipeline.py:1753` 主入口的 None 分支走 `_fail_event(seq, "场景绑定的引擎全部离线…")`——它内部用 `update_event_complete(result=reason)` 写 `events.result`，同步更新 `agent_tasks.errors`。autopilot 任务级（`scheduler.py:374`）也带 error。**前端 observability.js 已展示 `result_text`（611/1571 行）**——所以"失败原因可见"的通路已存在，真正缺的只是没接入这条通路的调用点。

**裸 failed 无原因的调用点（真实缺口清单）**：

| 调用点 | 现状 | 改法 |
|---|---|---|
| `event_pipeline.py:21` `_fail_scene_all_offline` helper（被 PR 订阅 4 处调用：771/789/873/891） | 只 log + 裸 `update_event_status(seq,"failed")` | helper 内部改走 `_fail_event` 同款逻辑（写 `events.result` + `agent_tasks.errors`），4 处调用点自动受益，一处改动 |
| `webhook.py:750` A2A 入口 | 裸 `update_event_status(seq,"failed")`，error 只在 HTTP 响应里 | 改走统一 helper |
| `event_pipeline.py:3600` vod 入口 | **实施时逐点核对**（本次未展开读） | 按 helper 模式接入 |
| `pipeline_executor.py:272` emit 入口 | 返回 `{"error": ...}` 给调用方 | 调用方是否落库实施时核对 |

文案统一：`"绑定的 {N} 台引擎（{引擎名列表}）全部离线，任务无法派发（scene={scene_id}, agent={agent_type}）"`——引擎名通过 `engine_name()` 反查，与事件 9336 排障诉求对齐。

**`update_event_status` 本身不动**（终态保护逻辑保持原样），改动收敛在 helper 和 2-3 个调用点。

### 4.5 数据模型变更汇总

| 变更 | 类型 | 迁移方式 |
|------|------|----------|
| `agent_runtimes.detached_from_scenes_at TEXT DEFAULT ''` | 加列 | `_ensure_column` 幂等 |
| `scene_engine_detach_log` 新表 | 建表 | `CREATE TABLE IF NOT EXISTS`（DDL 加在 db_setup.py，走 mysql_compat 翻译） |

无需手动改库；服务重启迁移自动执行（GET_LOCK 保护双主不撞车）。

## 5. 影响面评估（v2 新增：任务派发流转模块专项分析）

### 5.1 派发流转四层拆解与影响

**第一层：派发入口（get_scene_pool_engine_ids 的 8 个调用点）**

plugin 模式下 `pick_engine` 恒返回 0（广播），`get_scene_pool_engine_ids` 决定 `agent_tasks.pool_engine_ids`：健康引擎名单 / 空=全广播 / None=不入队 fail fast。本方案**不改这个函数**，只改 None 分支后的原因落库（§4.4）。派发时全灭依旧 fail fast——8h 内死绑定照样拒绝执行，语义不变。

**第二层：认领层（claim_by_tag，service.py:1183）——v1 未评估的关键事实**

系统里**已存在两条"绑定失效→放行广播"的既有通路**：

1. `claim_by_tag`（service.py:1300-1305）：worker 认领时发现 `pool_engine_ids` 里引擎全不可达（离线/无 tag/scope 不匹配）→ **当场清空该 task 的 pool_engine_ids，放行任意引擎认领**。
2. `_check_stale_queued_tasks`（service.py:2430，reap 循环 60s 跑）：任务 queued 超 120s 且池内引擎既不健康又不可达 → **清空 pool_engine_ids 放行**。

**结论：9336 式"永远 failed"只发生在派发那一刻引擎已全灭的场景**（不入队，claim 层兜底没机会介入）。若引擎在派发后才死，120s 后系统本来就会放开广播执行。所以本方案 8h 解绑引入的"绑定清空后任务广播"**不是新语义**，而是把场景配置层与既有 task 层兜底行为对齐——风险等级显著低于 v1 评估。真正变化只有一条：**之前 8h+ 永远 fail fast 的场景，8h 后开始能跑（广播）**，且伴随三重留痕告知用户。

**第三层：重派层（events.py:510-585）**

plugin 模式重派读 `agent_tasks.pool_engine_ids` 限定范围，`_pool_eids` 为空时退 scope 过滤广播——与第二层一致的既有兜底，本方案不动它。重派查询从 task 读名单而非场景，8h 解绑改的是场景侧，重派拿到的 pool 本来就是健康过滤后的快照，无交互。

**第四层：autopilot 调度（scheduler.py:355-375）——v1 遗漏的第二个绑定存储**

`autopilot_tasks.engine_runtime_ids` 是**任务级**绑定（"这次定时巡检要在哪台机器跑"），派发时同样走健康过滤 + 全灭 failed（带 error 原因）。**划界：8h detach 不清它**——任务绑定随任务生命周期存在，是每次执行的意图声明而非长期悬空配置；引擎离线时该次执行跳过并报错，是 autopilot 的正确行为。只有场景级绑定（`devops_pipeline_scene.nodes_json`）属于本方案清理范围。此划界写入 §4.2"不删的"。

### 5.1b 两表协同（v2.1 新增）：场景解绑 ↔ 巡检任务提示联动

**问题**：引擎绑定存在两张表（`devops_pipeline_scene.nodes_json` 场景级 + `autopilot_tasks.engine_runtime_ids` 任务级），用户是同一批人。场景侧 detach 引擎后弹了提示条，而巡检任务的绑定还写着同一台死引擎——用户在场景页看到"build-005 已被系统移除"，转头巡检任务还在那台机器上反复撞（scheduler 报"任务绑定的引擎全部离线"）。**同一个引擎，两个页面，两种说法**，提示口径割裂。

**设计原则：不删任务绑定，但统一提示口径**——死一台引擎，用户在两个页面看到同一句人话。

具体改动：

1. **后端**：detach 执行时（§4.2 第 1-3 步之后），顺带反查 `autopilot_tasks.engine_runtime_ids` 仍包含该 runtime_id 的任务，写入审计表时在 `scene_engine_detach_log` 增记一条 `affected_autopilot_tasks`（JSON：任务 id/name 列表）。巡检任务列表/详情接口（autopilot.py 查询处）返回时联查审计表，对绑定引擎已被 detach 的任务附带 `engine_detached: {runtime_name, detached_at, affected: [...]}` 提示字段。
2. **前端**：巡检任务列表/编辑页渲染同款提示条："绑定的引擎 build-005 已离线（超 8 小时）被系统从场景移除，任务绑定仍保留，请自行调整或重新绑定。"样式与场景编辑页提示条同一套（复用 §4.3 的提示条组件）。
3. **解除时机**：与场景侧一致——用户重绑该任务的引擎后，审计条目标记 `rebound_at`，提示消失。

**为何不直接同步删除两表**：任务绑定是用户对"单次执行"的意图声明，系统替用户改执行目标违背"失败不兜底"原则；且 autopilot 任务可能绑定的是"仅这台机器有权限跑"的引擎，静默改绑会把任务发到无权限机器上执行。提示 + 用户自行决策是诚实路径。

**代价**：约 30 行（autopilot.py 查询 + 前端提示条），无新表无新列（`affected_autopilot_tasks` 复用审计表字段）。

### 5.2 代码改动面（v2.1 修订）

| 文件 | 改动 | 规模 |
|------|------|------|
| `src/runtime/engine_select.py` | 抽 `_cleanup_scene_engine_runtime_ids` 为共享函数 + `_detach_dead_engine_bindings` | ~120 行 |
| `src/runtime/service.py` | reap 循环内接入 detach 检查（1 处调用） | ~8 行 |
| `src/api/db_setup.py` | 加列 + 新表 DDL | ~15 行 |
| `src/api/event_pipeline.py` | `_fail_scene_all_offline` 增强（写 result+errors）+ vod 入口核对接入 | ~20 行 |
| `src/api/routes/webhook.py` | A2A 裸 failed 改走统一 helper | ~8 行 |
| `src/api/routes/settings.py` | 原地函数改为调用共享函数（行为不变） | ~5 行 |
| `src/api/routes/agents.py` / `user/db.py` | 详情/编辑返回带 `detached_engines` | ~25 行 |
| `src/api/routes/autopilot.py` | 任务列表/详情返回带 `engine_detached` 提示字段（§5.1b 两表协同） | ~15 行 |
| `src/api/static/js/pages/dashboard.js` | 三态徽章 + 共享 helper + 编辑页提示条 | ~60 行 |
| `src/api/static/js/pages/scene-market.js` | 两处徽章改用共享 helper | ~15 行 |
| `src/api/static/js/pages/issues.js` | 失败事件补 result 展示（observability.js 已有，不动） | ~10 行 |
| `src/api/static/js/pages/autopilot.js` | 巡检任务提示条（§5.1b，与场景页同款组件） | ~15 行 |

合计约 **316 行**（后端 ~216 + 前端 ~100）。比 v1 估算略降：`event_store.py` 不改（§4.4 修订）、observability.js 不改（已有展示）；v2.1 加回两表协同 ~30 行。

### 5.3 运行时影响

- **DB**：`agent_runtimes` 行数两位数，加列瞬时完成；`scene_engine_detach_log` 低频只增。
- **性能**：detach 检查每 60s 一轮（随 reap），扫 `agent_runtimes` 离线行（个位数）× 关联场景（三位数），单次 < 50ms；`detached_from_scenes_at` 已写的直接跳过。
- **派发语义净变化（唯一一条）**：离线超 8h 的死绑定从"永远 fail fast"变为"解绑后广播可跑"。由 §5.1 第二层分析，这语义在 task 层早已存在，本方案只是把配置层对齐并补上留痕告知。
- **双主部署**：两台 master scp + 重启；detach 有幂等标记（`detached_from_scenes_at` 非空即跳过），双主并发不重复清理、不重复插审计。
- **13 台 worker**：不涉及，不动。
- **前端兼容**：老事件无 result → 不显示原因行；新徽章照抄现有 pill 结构。

### 5.4 不改的东西（明确划界）

- `get_scene_pool_engine_ids` / `pick_engine` 的 fail fast 与选择逻辑**不动**（#259/#134 语义保持）。
- `claim_by_tag` / `_check_stale_queued_tasks` 的两条既有放行通路**不动**。
- Issue #250 的删除清理路径不动，改共享函数后行为等价。
- `autopilot_tasks.engine_runtime_ids` **不清**（§5.1 第四层划界，§5.1b 只做提示联动不删数据）。
- 编辑模式下的候选列表逻辑（agents.py:259 回显多选）不动——编辑时仍显示全部绑定（含离线），查看模式如实显示三态。

### 5.5 风险与对策（v2 修订）

| 风险 | 对策 |
|------|------|
| detach 写库中途失败（部分场景清了部分没清） | 逐场景独立提交 + 幂等（重复执行结果一致）；`detached_from_scenes_at` 只在全部场景处理完后写，失败下轮重试 |
| 误杀短断线机器（master 间时钟漂移等） | 8h 阈值 >> 心跳周期 15s 与漂移量；可调 env 或置 0 禁用。对比：task 层兜底只用 120s，8h 已极保守 |
| 用户没看到提示条就疑惑"绑定怎么没了" | 事件失败原因 + 编辑页提示条 + 审计表三重留痕；提示条在重绑前持续显示 |
| 8h 后任务广播到"不该接"的机器 | §5.1 已论证：该通路 task 层早已存在（120s 兜底），非新语义；且场景编辑页有明确提示 |
| 两表提示口径不一致（v2.1 新增） | §5.1b：巡检页同款提示条 + 审计表 `affected_autopilot_tasks` 字段，同一引擎两页面同一句人话 |

## 6. 验证计划

1. 单测：detach 判定阈值边界（<8h 不删 / >8h 删 / 期间恢复心跳不删）、幂等重跑、审计落表、`_fail_scene_all_offline` 增强后 result+errors 双写。
2. 本地模拟：测试库把一台引擎 `last_seen_at` 改旧 8h+ → 跑一轮 detach → 验证 `nodes_json` 绑定被清、审计有记录、详情返回 `detached_engines`、**autopilot_tasks 绑定原样保留但列表带 `engine_detached` 提示**。
3. 前端：查看模式三态徽章截图对比；失败事件点开见人话原因；巡检任务页提示条与场景页口径一致。
4. 回归：`tests/test_scene_preset.py` + 引擎选择/claim 相关既有测试全绿；`claim_by_tag` 与 `_check_stale_queued_tasks` 行为不变（只读单测）。

## 7. 实施顺序

1. 后端：共享函数抽取 → DB 迁移 → `_detach_dead_engine_bindings` 接入 reap → `_fail_scene_all_offline` 增强 + webhook/vod 调用点 → autopilot 提示字段（§5.1b）。
2. 本地重启验证（含本地模拟链路）。
3. 前端：三态徽章共享 helper → 两页接入 → 编辑页提示条 → issues.js 失败原因展示 → autopilot.js 提示条。
4. 全量回归 + 交付（分两轮提交）。

## 8. 工作量预估

后端 ~216 行、前端 ~100 行；一轮会话内完成后端 + 自测，前端第二交付。

## 附：版本修订记录

**v1 → v2**：

1. 阈值 24h → 8h（主人拍板：8h 无心跳=不会恢复）。
2. 新增 §5.1 任务派发流转四层影响分析（派发入口/认领/重派/autopilot），发现 claim 层 120s 与 stale-queued 两条既有"放行广播"通路，"解绑后广播"非新语义，风险降级。
3. §4.4 重写：主派发路径与 observability 前端已有原因通路，真实缺口收敛为 `_fail_scene_all_offline` helper（4 处受益）+ webhook A2A + vod 入口；`update_event_status` 与 `event_store.py` 不再改动。
4. 新增划界：`autopilot_tasks.engine_runtime_ids` 不在清理范围（任务级意图声明 vs 场景级长期配置）。
5. 触发点从"_server_metrics_loop 或 reap"细化为 reap 循环内并列调用（有 `_check_stale_queued_tasks` 先例）。

**v2 → v2.1**：

1. 新增 §5.1b 两表协同：主人指出绑定存两张表（场景级 + autopilot 任务级），场景解绑与巡检任务提示口径割裂。方案：不删任务绑定，但巡检页渲染同款提示条（复用审计表 `affected_autopilot_tasks` 字段），死一台引擎两个页面同一句人话。
2. §5.2 改动面加 autopilot.py / autopilot.js 两行（~30 行），合计 286 → 316 行。
3. §5.5 风险表补"两表提示口径不一致"条目；§6 验证计划补巡检页提示口径校验项。
