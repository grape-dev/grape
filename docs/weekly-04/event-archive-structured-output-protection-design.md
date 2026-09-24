# Event archive 结构化产出保护（修复「处理记录/答复结论为空」）

> 状态：设计方案（v2）+ ✅ 代码修复已实施（2026-09-24）｜分支 weekly-04-lyz
> 已落地：`_locate_by_seq` 精确查（消 limit=200 漏命中）+ `finalize_event_archive` 定位失败 WARNING +
> 非结构化收尾不降级结构化段 + `tests/test_event_archive_locate.py`（7 用例）
> 待定：集群侧 workspace/event.json 跨节点可达性实证（§7，本机为单机 SQLite 非集群）
> 关联：集群事件 `Seq #7892`（vod，hello-calculator-05/#4）详情页「处理记录/答复结论」为空；
> 对照 `Seq #7890`（同 skill vod-cli，不同 worker）正常；#219（tasks complete 结构化回填/双信号）
> 复用：`src/runtime/service.py`（complete_task / finalize_task）、`src/api/event_archive.py`、`src/api/event_store.py`

---

## 1. 现象

集群（weekly-04，已含 #219）事件详情页：
- `Seq #7892`（agent=vod，skill=vod-cli，node=opencode-wo-0001）：**Agent 处理记录空白、答复结论无结构化**。
- `Seq #7890`（agent=vod，skill=vod-cli，node=opencode-wo-0002）：**结构化结论正常**。
- `#7891/#7893`（issue）同样空/None。

## 2. 集群实证（MySQL + `GET /api/events/detail`）

| seq | agent | skill | worker | events.result | agent_logs | archive.output |
|-----|-------|-------|--------|---------------|------------|----------------|
| 7890 | vod | vod-cli | opencode-wo-0002 | 有（结论句） | `[]` | **结构化**（actions/conclusion 有值） |
| 7892 | vod | vod-cli | opencode-wo-0001 | 有（243 字结论） | `[]` | **空模板** `{actions:[],conclusion:"",...}` |
| 7891 | issue | issue-advance | codearts-wo-0002 | 有（142 字） | `[]` | 空模板 |
| 7893 | issue | issue-cli | hermes-wo-0001 | 有 | `[]` | None（详情命中整文件未命中段） |

- `agent_logs` 全部 `[]`；
- `agent_tasks.result_text` 全部为结构化 JSON（显式 `grape tasks complete` 已落库）；
- `events.result` 均有结论文本（结构化 write 的结论曾写入并允许同态覆盖）；
- **archive 是否结构化只随「最后一次写入该事件 archive 的 complete_task 是否带结构化 result_text」变化**，且 7890 vs 7892 差异归结为 **worker 节点不同（wo-0002 vs wo-0001）**。

## 3. 根因（v2 修正版，2026-09-24 核对源码后）

> ⚠️ v1 假设「非结构化 complete 会把 seg.output 写回空模板覆盖结构化段」**与代码不符**：
> `finalize_event_archive`（event_archive.py:246）`if output:` 才写 seg.output，output=None 时**不写 seg.output**，
> 不会覆盖既有结构化段；空模板来自 `init_event_json`（event_archive.py:176）**建段时初始写入**。
> 因此不存在「写回空模板覆盖结构化」路径。

**修正根因（候选，按可能性排序）：**

1. **workspace/event.json 跨节点不可达（最可能，§7 已实证方向）**：`finalize_event_archive` 依赖
   `_locate_by_seq → _resolve_event_json_path`（event_archive.py:69），路径由 `workspace_repo_dir(repo_key)`
   决定。**job化多 worker 时，执行节点（wo-0001）与主服务 `DEVOPS_WORKSPACE_DIR` / NFS 共享不一致 →
   archive 写在 worker 本地，主服务读不到该 path → `_locate_by_seq` 返回 ("","") → finalize 提前 return**，
   结构化 output 永远写不进主服务侧 archive，段保持 init 空模板。7890 正常 = wo-0002 与主服务共享 workspace。
2. **`_locate_by_seq` events 表 LIMIT 未命中（次因）**：`get_events(repo_key, limit=200)`（event_store.py:489
   `LIMIT ?`）按 seq DESC 取仓库最新 200 条。仓库事件量大或 worker 侧 repo_key 与主库不一致时，seq 未命中 →
   走目录扫描 → 读不到 → return ""。放大 hint 优先路径可缓解。
3. **双收尾时序（陪衬）**：纯文本 complete（parse→None）先到不写 output（保持 init 空模板）；若之后结构化
   complete 因 §3.1/§3.2 定位失败未补写 → archive 恒空。命中正常时结构化总会写入，故时序非主因。

## 3A. 代码实证小结（2026-09-24）

- `finalize_event_archive`（L230-261）：`output` 非空才写 `seg.output`；`output=None`/falsy 时**跳过 seg.output 写**，
  只更新 status/exit_code/duration/completed_at —— **无降级路径**，v1「被覆盖成空模板」不成立。
- `init_event_json`（L155-186）：触发时建段，`output` 初始为 `{actions:[], conclusion:"", ...}` 空模板——
  这是「空 template」的唯二来源（另一来源：纯文本 complete 未写，保持初始模板）。
- `_locate_by_seq`（L121-152）：hint 无效 → `get_events(limit=200)` 未命中 → 目录扫描三项（新路径/无 provider/
  带前缀）——任一失败→return ""，`finalize_event_archive` L234 提前 return。
- `complete_task`（service.py:1616-1618）已传 `issue_num_hint=task["issue_id"]`；若 hint 有效（`#4` → num=4）
  `_extract_num` 直接返回 → 无 scan。故 `issue_num_hint` 传递链路正常，异常场景出现在 hint 为空/解析失败，
  或路径文件本身在两节点间不可见。
- 本机（203.0.113.2 单机）为主服务 SQLite，**非文档所述集群**，无法直接查 7890/7892 archive 文件与日志；
  集群存储/日志需在该集群主控侧核验（§7）。

## 4. 修复目标（v2 调整）

- **archive 定位健壮性（主）**：`_locate_by_seq` 在 cluster/多 worker 下必须可靠命中——workspace 目录共享核验、
  hint 路径可靠性强化、`get_events` LIMIT 提升/按 seq 精确查。目标：结构化 complete 一旦发生，archive.output **必然**被写入。
- **archive 结构化产出不被降级（保底）**：已有非空结构化 output 后，后续纯文本/None 收尾不覆盖该段
  （当前代码天然如此，补防御性判断 + 单测锁定，防回归）。
- **agent_logs 补充**：让「处理记录」不再恒空（可选伴随修改，见 §7 待办）。

## 5. 修复方案（v2）

### 5.1 主修：`_locate_by_seq` 命中率（`src/api/event_archive.py`）

- `_locate_by_seq`：当 hint 无效且 events 表 `limit=200` 未命中时，**扩大查询**（按 seq 精确查事件，不设 200 上限；
  或按 repo_key + seq 直接查 `events` 表主键，走索引）——避免事件量大时漏命中。
- `complete_task`（service.py）已有 `task["issue_id"]` hint；确保 **始终传非空 hint**（当前 L1618 已传
  `issue_num_hint=task.get("issue_id") or ""`），并对 hint 解析失败的 return 加 WARNING 日志（可观测定位）。
- 集群：核验执行节点与详情服务共享的 event.json 路径（`_resolve_event_json_path`/workspace 布局），见 §7。

### 5.2 保底：结构化段保护

在 `complete_task` 调 `finalize_event_archive` 前不新增复杂分支（当前 `if output:` 已天然保护），
改为在 `finalize_event_archive` 内加**防御断言**：`seg.output` 已结构化时，非结构化调用（output falsy）跳过
（仅更新 status/exit_code/duration），并 `log.info`。防止未来重构引入降级。

```python
# finalize_event_archive 内，写 seg.output 处
_had = seg.get("output") or {}
_structured_before = bool(_had.get("actions") or _had.get("conclusion") or _had.get("artifacts"))
if output:
    seg["output"] = {...}      # 结构化：始终可写/幂等
elif _structured_before:
    log.info(f"Archive seg {seq} already structured, skip non-structured finalize")  # 保护：不降级
    continue
# else: 从未有结构化且本次也无 → 保持 init 空模板（旧语义）
```

### 5.3 不变更

- `update_event_complete`（events.result/status）不受影响（同状态允许覆盖结论文本）。
- `parse_agent_output_json` 鲁棒解析不变。

## 6. 验收标准 / 测试

| # | 验收项 |
|---|--------|
| 1 | `_locate_by_seq` 在事件量>200 时仍命中（单测构造 >200 events 或 mock events 查询无上限） |
| 2 | 结构化 complete 后 `archive.output` 为结构化（actions/conclusion 有值） |
| 3 | 已有结构化段 + 非结构化 finalize → 段不被降级（单测：先结构化后纯文本 complete） |
| 4 | 从未结构化 + 非结构化 → 保持 init 空模板（旧行为不回归） |
| 5 | events.result 结论文本更新不受影响 |
| 6 | 集群复现仓库重跑：7892 类事件 archive.output 恢复结构化（定位未命中被修复） |

测试载体：`tests/test_task_archive_protection.py` + `tests/test_event_archive_locate.py`。

## 7. 待办/未决

- [ ] **集群 workspace 共享核验（最高优先）**：在集群主控侧确认 hello-calculator-05 仓库执行期间，
  wo-0001/wo-0002 的 `DEVOPS_WORKSPACE_DIR` 与主服务是否一致、event.json 是否同一文件系统可见。
  命令：两个 worker 上 `echo $DEVOPS_WORKSPACE_DIR`、`ls {WS}/.../hello-calculator-05/issues/{4}/event.json`；
  主服务侧同路径是否读得到。
- [ ] **主服务日志定案（集群侧）**：grep 集群主控日志 `7890/7892|finalize_event_archive|_locate_by_seq|WARNING`，
  确认 wo-0001 是否因定位失败提前 return（WARNING `Archive finalize failed` / 静默 return 无日志）。
- [ ] **`get_events` LIMIT 精确化**：`_locate_by_seq` 改用按 seq 精确查（`WHERE repo_key=? AND seq=?`），
  或 `limit=500+`，消除仓库事件量大时漏命中。
- [ ] **agent_logs 回填**：`grape tasks complete`/双信号路径不回填 `events.agent_logs` →「处理记录」恒空（独立修复）。
- [ ] 与 #219 双信号 `finalize_task` 的收尾收敛（finalize 统一用存好的结构化 result_text）对齐。

## 8. 风险/回退

- §5.1 改进命中不影响正常路径（命中逻辑是超集，不改变已命中行为的写入内容）。
- §5.2 防御断言只防止「未来源码回归」，不改当前正确行为。
- 纯文本结论事件：非结构化 complete 仍保持空模板 → 详情页行为不变（旧语义）。
- 回退：revert 该 commit，无其它模块副作用。