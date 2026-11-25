# Spec: Issue #3912 — 静默 except 棘轮门禁恢复（weekly-mys 承接版）

## Objective
恢复 `tests/arch/test_silent_except_ratchet.py` 门禁有效性：超基线静默 except 点全部处置为显式处理，
`test_silent_except_ratchet` 与 `test_ratchet_baseline_self_check` 全绿；禁止上调基线数值"消红"。

## 承接口径修正
Spec（issue agent 版）取证于 arch-weekly-11 树；承接流水线确认目标基线为 `weekly-mys`
（Spec 头部源会话分支作废）。在 weekly-mys 上实测超基线点为：

| 文件 | 现象 | 超基线点 |
|------|------|---------|
| `src/domains/runtime/engine/branch_guard.py` | 基线3，豁免2，实测净4 | `except Exception: pass`（#b42f3e74e 新增的聚合告警 log 兜底） |
| `src/api/a2a_terminal_guard.py` | 新文件，1 处 | `except (TypeError, ValueError): continue`（载体 PR 号解析） |
| `src/api/terminal_disposition.py` | 新文件，1 处 | `get_manual_override` 的 `except Exception: pass` |

Spec 原列 5 文件（db_pool_guard.py 等）在 weekly-mys 树不存在，属 arch-weekly-11 现象，不在本分支处理。

## rebase 复核（weekly-mys@885ceface，2026-11-24）
基线再次前滚至 `885ceface`（arch-weekly-11 合入后的新架构树），同一门禁在**新基线上另增 11 处**超基线点，
均属新基线自身带入（base 自身红），按同一口径逐点显式化处置，仍不调基线、不加豁免：

| 文件 | 新增点 |
|------|--------|
| `src/domains/runtime/engine/engine_select.py` | `_pick_local_engine` 主机名过滤失败 `pass` → `log.debug`（fail-open 语义不变） |
| `src/domains/runtime/engine/repogit.py` | 基线错误字段 int 解析失败 `pass` → `log.debug` |
| `src/domains/runtime/engine/service.py` | `complete_task` 清连续失败链失败 `pass` → `log.debug` |
| `src/api/drain_state.py`（新文件，3 处） | 标记写入失败 → `log.warning`；标记不存在 → `log.debug`；删除失败 → `log.warning` |
| `src/domains/insight/public/__init__.py` | 凭据写后解析缓存失效 `pass` → `log.debug`（不阻断写路径） |
| `src/domains/mgmt/migrations/autopilot_custom_fields_cleanup.py`（2 处） | 不可解析 custom_fields 继续 → 先 `log.debug` 再 `continue` |
| `src/domains/mgmt/migrations/scene_nodes_skill_id_repo_scope.py` | 坏 nodes_json 跳过 → 先 `log.debug` 再 `continue` |
| `src/domains/runtime/engine/runtime_id_guard.py` | 告警兜底文件写失败 `pass` → `log.debug` |

## 处置决策（逐点修复，不调基线、不加豁免）
1. `branch_guard.py`：log.warning 失败兜底改写 stderr（#2933 已确立的「logger 失败兜底写 stderr」口径）。
2. `terminal_disposition.py`：改 `log.warning`（与本文件 record/clear override 同款既有风格）。
3. `a2a_terminal_guard.py`：坏条目 continue 前补 `log.debug` 留痕（issue_vcs_index 解析族同语义，显式化）。

## Acceptance
- [x] 两门禁用例全绿（净计数 ≤ 基线，豁免登记 0 漂移）
- [x] 不上调 `silent_except_baseline.json`、不新增 EXEMPT_FILES 名额
- [x] 受影响模块既有测试通过（branch_guard / terminal guard 族 89 用例 + tests/arch 35 用例）
- [x] rebase 到 885ceface 后重跑：两门禁 2 绿；`tests/arch` 除基线既有失败外全绿（仅 1 条 base 自带的 chat 具名查询失败）
