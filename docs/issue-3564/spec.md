# Issue #3564 — 场景绑定机器支持优先级（高优先级引擎不可接取时才降级）

## Objective
场景节点绑定的每台机器（引擎实例）可设优先级（1..9，1 最高）。派发/认领时，只要池内存在
更高优先级且**当前可接取**的机器，就由它接；全部不可接取才降级给低优先级机器。
未配置优先级的场景/任务与改前逐字节一致。

## Scope
### In Scope
- 场景 node 旁挂 `engine_priorities`（实例绑定模式；`{str(engine_id): 1..9}`），UI 引擎绑定弹窗内编辑。
- 入队解析场景绑定时产出优先级映射，写入新列 `agent_tasks.engine_priorities`（JSON；空=旧行为）。
- `claim_by_tag` 守卫链末尾（FIFO 跳过段之前）增加优先级闸；判定与认领同一 `db_lock` 临界区，
  认领仍为原子 `UPDATE ... WHERE id=? AND status='queued'`。
- 新增 `claim guard [priority]` 决策日志（跳过原因、本机 p、可接取的高优引擎）。
### Out of Scope
- 修改 legacy `pick_engine` / scope 派生档位。
- 机器全局优先级属性（方案 B）、引擎管理页全局优先级编辑。
- 未实例绑定的场景（无 `engine_runtime_ids`）不产生优先级语义。

## Behavior
1. 取值 1..9（1 最高），UI 高/中/低快捷档 = 1/5/9；未配置机器按最低档 9 处理
   （常量唯一真源 `pipeline_config.ENGINE_PRIORITY_MIN/MAX/DEFAULT`）。
2. 认领判定尺子：高优引擎「物理可达（在线/健康/探活/tag/scope）+ 健康分达标
   （`health_scorer.is_healthy`，与 claim_by_tag 低分拦截同尺子）+ 未满载」才算可接取；
   满槽/离线/心跳过期/无 tag/角色或 scope 不符/健康分低 → 低优可接。
3. 判定范围限定在任务 `pool_engine_ids` 与优先级映射的**交集**内（入队池已按健康过滤，
   被排除出池的高优引擎不参与判定）；`pool_engine_ids` 为空（全量广播）维持旧语义
   ——遍历全部映射键。
4. 饿死兜底：任务 queued 超过 `_STALE_QUEUED_THRESHOLD`（默认 120s）后忽略优先级，
   低优引擎可接；日志 `claim guard [priority]: bypass ... ignore priority`。
5. 未配置 `engine_priorities`：优先级闸整体旁路，无决策日志，行为与改前一致。
6. 保存校验：优先级键必须属于节点 `engine_runtime_ids`，值为 1..9；非法配置 400。
   校验/读取/复制清洗/任务侧归一化收敛为单一 helper
   `pipeline_config.normalize_engine_priorities(raw, allowed_ids, strict)`。
7. INSERT 契约：`_create_direct_task` / `_create_failed_task` / `enqueue_task` 行构造
   均显式携带 `engine_priorities` 列值（tests/contracts 静态守卫 + DB 回归看护）。

## Acceptance
- [x] 高优在线空闲 → 低优认领被跳过；高优满载/离线/心跳过期/无 tag/scope 不匹配/健康分低 → 低优可接。
- [x] 未配置优先级 → 行为与改前逐字节一致（回归用例）。
- [x] sqlite 内存库驱动真实 `claim_by_tag` 用例 35 条（决策日志/超时兜底/同档不跳过/
      健康分闸/池交集/空池旧语义/归一化 helper strict 与非 strict/签名契约/
      uid 绑定清空优先级与保存侧 400 兜底）。
- [x] 优先级解析只保留绑定引擎、非法值丢弃；未配置返回空。
- [x] 前端引擎绑定弹窗仅勾选后出现优先级控件（高/中/低），zh/en 文案齐全。
- [x] `claim guard [priority]` 决策日志可复现「为什么这台机器没接这个任务」。
- [x] 加列走 `register_migration` + `_ensure_column`（`202611240011`），不改建表基线语义。
- [x] 不改动 legacy `pick_engine`。
- [x] direct/failed 快照含 `engine_priorities` 占位（INSERT 契约静态守卫 + 落库回归）。

## Risks
| Risk | Mitigation |
|------|------------|
| 高优可达但从不出手 → 低优饿死 | queued 超时忽略优先级 + 决策日志留痕 |
| 任务起跑增加一个轮询周期 | 判定仅在高优可接取时跳过，未配置场景零开销 |
| 加列迁移 | `register_migration` + `_ensure_column`（strict），SQLite/MySQL 双兼容 |
