# Issue #3904 需求文档（Spec 摘要）

## 目标

消除 hermes ACP overlay 的「构建跳过清单」与「可用性自检必需清单」互斥：共享
`HERMES_HOME` 存在 `profiles/` 时，overlay 仍应判定可用，不再整体回退共享 home。
回退消除后：hermes 任务恢复并发（不再被 `_SHARED_HOME_LOCK` 串行）、共享
`state.db` 不再被所有会话写爆（生产实测 1.03GB / 阈值 50MB / 每任务 `skipping resume`）、
`usage_collect` 恢复读到 per-task `state.db`（不再 24h 98 条 `copy/open sqlite failed`）。

## 现象（生产 Loki，master-001，24h）

| 指标 | 24h |
|---|---|
| `overlay home unusable — falling back` | 266 |
| `state.db too large, skipping resume` | 257 |
| `usage_collect: copy/open sqlite failed ... No such file` | 98 |

四条 WARNING 每条任务固定成串出现，且 `state.db size` 始终是同一条持续增长的文件
（1030.2→1034.7MB），说明命中同一条退化路径。

## 根因

`src/domains/runtime/agents/hermes_acp.py`：

1. `_build_overlay_home()` 的跳过元组把 `profiles` 排除，overlay 里根本不产出该条目；
2. `_overlay_looks_usable()` 的 `_required` 又把 `profiles` 列为必需；
3. 共享 home 一旦有 `profiles/`（Hermes 多 profile 目录），构建必然不产出 →
   自检必然判不可用 → `_remove_overlay_tree()` + `iso_home = src_home` +
   `_shared_home_mode = True`；
4. 回退后 `_SHARED_HOME_LOCK` 让同机 hermes 任务并发=1；所有会话同写一份
   `state.db`；`usage_collect._hermes_rows()` 仍按 `~/.cache/hermes-acp-homes/<hash>/state.db`
   读，而该目录已被删除 → `No such file`。

`src/domains/runtime/engine/usage_collect.py`：回退场景下只认 overlay 路径，不认
实际使用的共享 home。

## 范围（本条收口）

- 两张清单同源：新增 `_OVERLAY_NOT_MIRRORED` / `_OVERLAY_REQUIRED_ENTRIES` 常量，
  自检按「必需清单 − 跳过清单」求值，构建按「跳过清单」跳过；`profiles` 归入只读共享
  资产（symlink / junction / 有界复制三梯次），不再「跳过却必需」。
- 回退分支补可诊断信息：单行结构化 `ACP_OVERLAY_FALLBACK total=… tier=… reasons=…`
  （含构建期梯次备注），并维护进程内累计计数 `overlay_fallback_total()`；
  正常应为 0。
- `usage_collect` 回退感知：overlay `state.db` 不存在、且本进程该 cwd 确有回退标记时，
  按共享 `HERMES_HOME/state.db` 取账本；文件消失/缺失降级为 INFO/DEBUG，不再逐任务 WARNING。
  无回退标记的 cwd 不读共享账本（防重复计量）。

## 非目标

- 不改 `#3773` 的 state.db 治理动作（VACUUM/归档/阈值）语义；
- 不新增 `runtime_credentials` 指标列（已盖章迁移 202611240006 再补列在存量库不生效，
  会打断整条心跳；回退计数走日志计数 + 进程内累计，见 plan.md §风险）；
- 不清理生产上已 1.03GB 的共享 `state.db`（运维动作，与 #3773 一并处理）；
- 不动 `agents/sandbox/overlay.py` 的历史镜像实现（当前无调用方，见 plan.md §影响面）。

## 验收

- 共享 home 含 `profiles/`（非空）时：overlay 构建产出 `profiles`（Linux symlink /
  Windows junction / 无权限时复制），`_overlay_looks_usable == True`，不触发回退。
- 必需清单与跳过清单无交集（漂移守卫用例锁死，新增条目任一侧都不会再互斥）。
- 缺运行时（tools/installs）时仍判不可用并回退——fail-closed 语义不退化。
- 回退分支日志带 `tier` / `reasons` / `total`，计数器单调递增。
- `usage_collect` 在 overlay 缺失、共享 home 有账本时能取到用量；两者都缺时无 WARNING。