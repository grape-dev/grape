# Plan：Issue #4375 实现方案

## 设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| events 读取通道 | `session_manager.db` 直查 / runtime public 只读口 | public 只读口 | 直查在分库拓扑解析为进程库，属跨域误读；public 口是域边界唯一读通道 |
| 回滚需要的列 | 复用 `event_payload(seq)` / 新增具名查询 | 新增 `event_routing_payload` | `event_payload` 只返回 payload，回滚还需 `repo_key`/`issue_id` 定位 `issue` 行 |
| 缺 DSN 行为 | 回落进程主库 / 显式失败 | 显式 `DomainUnavailable` | 静默回落 = 1146 复现，且与「数据不存在」不可区分（同 #3826 结论） |
| 回滚失败日志级别 | WARNING / ERROR | ERROR + 连接事实串 | 现状 5 条/小时被吞，巡检查不到断链；事实串可分流「旧进程」/「打错库」/「缺 DSN」 |
| 是否直调 `event_store` 内部实现 | 直调 / 走 public 口 | public 口 | 守住域边界，避免 mgmt 进程 import runtime 内部实现 |

## 实现

### 1. `src/domains/runtime/public/__init__.py`

- `_ANY_QUERIES["event_routing_payload"] = "SELECT repo_key, issue_id, payload FROM events WHERE seq=? LIMIT 1"`
  （列序与解包顺序逐字一致；SQL 全仓只此一份，由 `test_event_routing_read_has_single_definition_in_src` 棘轮看护）。
- `event_routing_payload(seq) -> dict | None`：经 `read_one_any` 走白名单；payload 解析为 dict，
  空/非法 JSON/数组 → `None`，交由调用方按「不处理」短路。
- `_any_cursor` 改为 `with db_registry.cursor_for("runtime", write=write) as c: yield c`。
  `route_of` 是内核唯一路由判定：`domain`（跨域且 DSN 在）/ `local`（单库拓扑或本域进程）/
  其它 → `DomainUnavailable`。`write=True` 的提交语义由 `cursor_for` 保持。
- `__all__` 补登 `event_routing_payload`。

### 2. `src/domains/runtime/pipeline/event_pipeline.py::_rollback_auto_advance_stage`

```text
seq 空 / 非数字        → return（不发起读、不抛）
evt = rt_public.event_routing_payload(seq) → None → return
payload 非 dict / 无 stage_guard / from 空 / to 空 / from == to / repo 或 issue 空 → return
get_issue_stage(...).stage != guard.to → return（幂等：已被其它路径推进）
set_issue_stage(repo, issue, guard.from, guard.from) → INFO
异常 → ERROR: seq + issue + stage_guard + db_registry.conn_facts("runtime")
```

`issue`/`stage_guard` 提示变量在解析过程中逐步填充，读行失败时退化为 `-`，不因提示字段缺失掩盖原始异常。

### 3. `src/api/skill_display.py`

代码不改（已走 `runtime_public`）。行为随 `_any_cursor` 收口生效：三库有 DSN → 读 runtime 库；
无 DSN → `DomainUnavailable` 被既有 `except` 记为 WARNING 并回退 `skill_name`（回退优先级语义不变）。

## 影响面（机械检索）

- `grep -rn "_any_cursor" src/ tests/`：仅 `src/domains/runtime/public/__init__.py` 定义，
  调用点全部在本模块（具名查询原语族）与经 `read_one_any`/`read_all_any` 的外部调用方；
  外部仅 `src/api/patrol.py` 经 `runtime_public.read_all_any` 间接使用。
- `grep -rn "SELECT repo_key, issue_id, payload FROM events" src/`：改动前 1 处
  （event_pipeline 直查），改动后 1 处（public 具名查询）。
- `grep -rn "getsource" tests/`：无对 `_rollback_auto_advance_stage` / `_any_cursor` 的既有源级守卫；
  本单新增两条棘轮，指向新实现。
- `tests/api/test_runtime_public_lib.py` 的单库回落口径（`_any_cursor → session_manager.db`）保持不变
  （`route_of` 在 `lib_enabled()=False` 时返回 `local`）。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 游标原语为 runtime public 全部具名查询共用 | Med | 单库/测试 `local` 分支由 `route_of` 保留；跑 `tests/api + tests/runtime` 全量回归 |
| 三库缺 DSN 时由「静默读错库」改为「显式失败」 | Med | 这是本单目标语义（同 #3826/#3771 口径）；调用方各自既有降级分支（返回 None/空集）承接 |
| ERROR 日志量上涨 | Low | 每条失败事件仅一条，带 seq 可定位，不循环刷屏 |
| 回滚与其它路径并发推进 stage | Med | 保留「当前 stage ≠ guard.to 即不动」幂等判定，不改语义 |

## 验证

```bash
python3 -m pytest tests/runtime/test_issue_4375_auto_advance_rollback.py -q
python3 -m pytest tests/api tests/runtime -q          # 全量回归
python3 -m pytest tests/runtime/test_repogit.py tests/runtime/test_issue_3482_workspace_order.py \
                 tests/runtime/test_issue_2245_git_writable.py -q
make lint && python3 scripts/check-arch.py
```