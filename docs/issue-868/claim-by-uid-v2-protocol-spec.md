# claim-by-uid 协议 v2 设计 Spec（Issue #868 设计交付；Issue #1011 实现落地 + 勘误）

> 状态：已实现（Issue #1011：v2 claim 系端点 / heartbeat-v2 / 上报 v2 / capabilities
> uid_claim 能力位 / worker v2-first 回退，端点形态见 §2.1 勘误后口径）。
> 背景：worker 认领（claim）链路 v1 以自增 `runtime_id` 寻址；`runtime_id` 跨刷新/重建会漂移
> （#646 已为持久层引入 `engine_uid` UUIDv5 稳定键）。v2 的目标：上行寻址从 id 迁移到 uid，
> 消除 worker 侧因引擎行重建导致的认领黑洞。

## 1. 目标与非目标

**目标**
- worker → 服务端的 claim/heartbeat/task-result 上行支持按 `engine_uid` 寻址。
- 新旧 worker 混跑兼容：双端点并存期，uid 失效可回退 `runtime_id`，不丢任务。
- 协议演进可灰度、可回滚：任何时刻旧协议仍可用。

**非目标（本期明确不做）**
- 删除 id 寻址端点（阶段 5，依赖双读稳定 + 兼容期窗口判定）。
- 前端 uid 消费（前端仓另立 issue）。
- bot_lease ON DUPLICATE 方言 SQL 缺口（另行立 issue 跟踪）。

## 2. 协议形态（v2）

### 2.1 双端点并存（兼容期核心设计）

> **勘误（Issue #1011）**：原表所列 `POST /api/runtime/worker/claim`（既有）为臆造路径，
> 代码中不存在。真实既有 v1 端点为 `POST /api/runtime/runtimes/{runtime_id}/tasks/claim`
> （src/api/routes/runtime.py，路径参数 `runtime_id: int` = 自增 id 寻址，即 v1 的
> 漂移根源）及 claim-by-tag / claim-bound / claim-by-tag-bound / claim-capabilities
> 共 5 个变体。v2 已按真实路径形态实现（Issue #1011，与下表一致）：

| 端点 | 寻址键 | 状态 | 说明 |
|---|---|---|---|
| `POST /api/runtime/runtimes/{runtime_id}/tasks/claim`（既有 v1） | `runtime_id` | 长期保留 | 旧 worker（存量版本）唯一路径 |
| `POST /api/runtime/runtimes/uid/{engine_uid}/tasks/claim`（v2，已实现） | `engine_uid`（路径）+ `runtime_id`（body 可选兜底锚） | 新增 | v2 worker 首选 |
| `POST /api/runtime/runtimes/uid/{engine_uid}/tasks/claim-by-tag/{tag}`（v2 变体，已实现） | 同上 | 新增 | tag 匹配 + scope 隔离（服务层与 v1 共用） |
| `POST /api/runtime/runtimes/uid/{engine_uid}/tasks/claim-bound`（v2 变体，已实现） | 同上 | 新增 | expected 原子校验；归属校验 uid 口径 |
| `POST /api/runtime/runtimes/uid/{engine_uid}/tasks/claim-by-tag-bound/{tag}`（v2 变体，已实现） | 同上 | 新增 | 同上 |
| `GET /api/runtime/runtimes/uid/{engine_uid}/tasks/claim-capabilities`（v2 只读，已实现） | 同上 | 新增 | 能力位含 `uid_claim`（worker v2 探测锚点） |
| `POST /api/runtime/heartbeat-v2`（已实现） | body `engine_uids[]` + `runtime_ids[]` | 新增 | uid 优先解析、失效回退 ids、全失效不造行 |
| `POST /api/runtime/tasks/{task_id}/complete-v2` / `fail-v2` / `progress-v2`（已实现） | body `engine_uid` | 新增 | 按 uid 校验任务归属（不一致 403） |

- 心跳 / 任务结果上报同样提供 v2 端点（上表），v1 端点原样保留。
- **不做协议切换**：v2 端点上线后 v1 保持可用，直到阶段 5 显式下线（需先统计 v1 流量归零）。
- lookup / recover-orphans / tags 等其余 runtimes/{id} 路径端点**本 issue 不 v2 化**
  （非认领热路径，阶段 5 随 id 读路径清理统一处置，豁免清单）。
- **404 语义二分（Issue #1011 补，worker 回退逻辑前提）**：v2 端点 404 须区分
  ①端点不存在（`{"detail":"Not Found"}` 空白 404/405 = 旧服务端，worker 切换 v1
  并记忆）②uid 失效（body 错误码 `UNKNOWN_ENGINE_UID` = uid 漂移，回退 v1 id claim）。

### 2.2 请求字段（v2 claim 示例）

```json
{
  "runtime_id": 3,
  "expected_task_id": "",
  "expected_event_seq": 0,
  "supports_obs_url": true,
  "prefer_fallback": false
}
```

- `engine_uid`：路径参数，36 位小写连字符 UUID（服务端校验 uid→行解析，限本凭据）。
- `runtime_id`：body 可选，作为 uid 失效时的兜底锚与漂移检测输入。
- `expected_task_id` / `expected_event_seq`：成对可选（bound 语义，与 v1 同）。

### 2.3 服务端解析语义（uid 失效回退 runtime_id）

```
resolve(engine_uid, runtime_id):
  1. 按 engine_uid 查 agent_runtimes → 命中 → 使用该行（并顺手校正 runtime_id 缓存）
  2. 未命中（uid 失效：引擎行被删除/合并）：
     a. runtime_id 非 0 且命中且该行 engine_uid 为空（未迁移） → 回退用该行
     b. 否则 → 404 UNKNOWN_ENGINE_UID，附带建议（重新注册获取新 uid）
```

- 判定依据复用 #646 双读 seam：`get_runtime_id_by_engine_uid` / `get_engine_uid_by_runtime_id`。
- **回退只在并存期**：uid 失效 + id 也失效 = 引擎确实不存在，不静默造行（防 -cred0 孤儿，见 runtime-admin-two-view-design §6.2）。
- 引擎删除路径（吊销/devstage 删环境/刷新过期）已经 `_rebind_runtime_references`
  显式清理引用并出对账报告（#868 T10 已扩展到会话表 uid 轨道），uid 失效面收敛为
  「删除后 worker 未及时重连」这一种，回退语义可覆盖。

### 2.4 worker 侧行为（注册时锚定 uid）

- worker 注册/心跳响应中服务端**双带返回** `engine_uid` + `runtime_id`（#646 T4 已实现）；
  worker 持久化 uid 作为后续 claim 的寻址键。
- uid 失效（404 UNKNOWN_ENGINE_UID）→ worker 降级 v1 端点按 `runtime_id` claim；
  两者都失效 → 重新注册（register 端点本就幂等，重复注册 uid 不漂移，#646 T4 保证）。

### 2.5 守卫测试（Issue #1011 已实现：tests/atomic/test_issue_1011_claim_v2.py）

- `test_claim_v2_resolves_by_uid`：uid 命中 → 按该引擎认领。
- `test_claim_v2_uid_stale_falls_back_to_runtime_id`：uid 失效 + id 命中未迁移行 → 回退成功。
- `test_claim_v2_both_stale_404`：双失效 → 404，不造行。
- `test_claim_v1_still_works_during_dual_period`：并存期 v1 端点行为不变。
- `test_repeated_register_keeps_uid`：重复注册 uid 不漂移（已有 #646 S4 覆盖，纳入回归集）。
- **`test_rename_template_keeps_engine_uid`（Issue #868 交叉预警追加，已实现于
  `tests/atomic/test_issue_868_engine_uid_rename_guard.py`）**：模板/Provider 改名 →
  引擎行 UPDATE name → uid 不变 → claim 不断链。防未来"优化"回 name 派生的门禁。

### 2.6 派生源锚定与改名跟随（Issue #868 交叉预警修正，已实现）

**修正**：`derive_engine_uid` 的 uuid5 键原含 `name` 段，与 i5
（feat/devbox-sandbox-integration5）云引擎四段名
`engine_type:tpl_slug:provider:provider_id` 的「改名跟随」机制冲突——模板/Provider
改名后 name 随 refresh 刷新，uid 漂移击穿本节 §2.3 的「uid 失效面收敛」假设。

- **云引擎 uid 表示（provider_uid + template_uid 成对在场，§2.7 升格形态）**：
  uuid5 键 = `credential_uid|engine_type|scope|owner_id|host_name|provider_uid|
  template_uid`——**name 与自增 id 均不参与**；锚由 worker 注册 payload 的
  runtime 条目透传（`engine_type`/`provider_uid`/`template_uid`，id 分量可省）。
- **云引擎 id 表示（过渡，老 worker 仅透传 id 锚）**：uuid5 键 =
  `credential_uid|engine_type|scope|owner_id|host_name|provider_id|template_id`
  （别名 `engine`/`template_db_id`，见 `cloud_anchor_from_runtime_info`）；
  uid 分量缺一不成对时不切换（防两轮漂移）。
- **普通引擎（无锚）**：沿用 legacy 公式（含 name，name 本身稳定=锚）——存量 uid
  逐字节零漂移（回归守卫测试锁定）。
- **§2.3 收敛恢复**：改名不再制造 uid 失效；失效面回归「引擎删除后 worker 未及时
  重连」一种，v2 回退语义成立。

**i5 合流迁移清单（必做项）**：
1. 存量云引擎行 uid 按旧键（含四段名）派生 → 合流时调用
   `rebase_engine_uids_by_anchor(c, anchor_rows)`（src/user/db.py）按新锚重算回填，
   池/能力/任务/消息/会话引用轨道整体改绑，对账报告落日志（不静默丢弃）；
2. 注册路径双读兼容已在位：锚在场 + uid-first 未命中 → legacy 行命中即改绑
   （`rebase_engine_uid_references` + 结构化事件 `engine_uid_anchor_rebased`）；
   i5 升格透传 uid 后（id 公式 → uid 公式）同样经该路径一次性改绑收敛；
3. 守卫测试 `test_issue_868_engine_uid_rename_guard.py` 纳入 CI（改回 name 派生即红）。

### 2.7 种子分量 id→uid 升格（Issue #868 评论 198288127 / QA 复核 198288768，已实现）

**动因**：id 表示种子的 provider_id/template_id 为自增主键——多主控各持独立库聚合
（id 只在单库唯一，撞号同名不同物）、部分恢复/旧快照搬回新库（AUTOINCREMENT 断裂，
快照旧 id 错配别行）、表重建（delete-重插，id 全量换值）三场景下种子漂移，恰是
#646 用 uuid5 派生要根治的同病根源。升格与 credential_uid 完全同构（机制零新增）。

- **i5 侧（契约，feat/devbox-sandbox-integration5 工作线）**：
  `sandbox_provider_config` 加 `provider_uid`、`sandbox_templates` 加
  `template_uid`（UUIDv4，创建时一次性生成、终身不变）；**backfill 沿旧行值按旧
  id 映射拷贝、不新发 UUID**（表重建后 uid 照样不漂移）；worker 注册 payload
  runtime 条目透传两 uid。i5 身份锚定 UPSERT 五元组**继续用 provider_id**
  （行内定位语义，跨行/跨库/跨重建的派生种子才要求 uid 表示）。
- **grape 侧（本 PR 已实现）**：`cloud_anchor_from_runtime_info` 返回五元组
  （含 provider_uid/template_uid，uid 归一小写规范形）；`derive_engine_uid`
  uid 分量成对在场 → uid 公式（id/name 不参与），仅 id 锚 → id 公式（T13 过渡
  行为逐字节不变），无锚 → legacy；`rebase_engine_uids_by_anchor` anchor_rows
  接受 uid 分量，**旧行 uid 从行内读取（旧 uid→新 uid 映射式回填，禁止按
  runtime_id 重派 JOIN——id/uid 两公式产物同源 rid 不可区分，重派会静默漏回填）**。
- **时机**：升格在 claim-by-uid v2 端点实现之前完成——v2 worker 将持久化 uid，
  若先按 id 版落种子，v2 上线后会经历两轮全量重派生。
- **守卫**：`tests/atomic/test_issue_868_seed_uid_upgrade.py`——表重建/库恢复
  id 换值 uid 不变、部分 uid 不切换、id 公式过渡零漂移、legacy 零漂移、映射式
  回填改绑全轨道不留孤儿、回填后按新 uid 寻址命中。

## 3. 阶段推进与退出条件

| 阶段 | 动作 | 退出条件 |
|---|---|---|
| P0（本期） | 设计成文 + 守卫测试设计 | 本 Spec 合入 |
| P1 | v2 端点实现 + 双端点并存 + 混跑测试 | CI 全绿，灰度 worker 切 v2 |
| P2 | 全量 worker 升级到 v2 | v1 claim 流量归零（观测 ≥2 个发布周期） |
| P3（=阶段 5 的一部分） | 下线 v1 claim 端点 | 前端/worker 双侧 uid 消费就绪 |

## 4. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 旧 worker 长期不升级，v1 无法下线 | P2 观测窗口 + 强制升级公告；v1 端点保留到阶段 5 |
| uid 失效风暴（批量删除引擎） | 回退 runtime_id + 重新注册幂等；对账报告（#868）可见孤儿面 |
| 双端点双倍维护成本 | 服务端解析统一走 `resolve_engine_refs` 单实现，端点仅薄壳 |
