# Plan: Issue #4440 — Coding 交付核实统一入口

## 方案
统一入口 + 双路径前置挂钩（见 spec.md）。

## 改动清单
| 文件 | 类型 | 说明 |
|------|------|------|
| `src/domains/runtime/engine/delivery_verify.py` | 新增 | 唯一交付核实入口：核实清单、PR 查询（区分无 PR / 查询失败）、fail-closed 判定 |
| `src/domains/runtime/engine/service.py` | 修改 | `complete_task()`：入口成功、-15 结构化复活、瞬时降级三条通道前置核实；无 issue_id 不再 fail-open；瞬时被拒回传口径；未通过 exit_code=1 + 事件 result 前缀 + errors 结构化错误码；`note_task_success` 后移到核实通过；`enrich_task_on_late_report` 迟到提升前接入核实 |
| `src/domains/runtime/engine/direct_executor.py` | 修改 | `_execute_agent_inner`：终态写入前同口径核实（无 issue_id 不再跳过），未通过 new_status=failed + exit_code=1 + result 前缀 + `_append_task_error` 落错误码；瞬时被拒同口径；会话 idle 后移并抽 `_mark_session_idle` |
| `src/domains/runtime/engine/transient_retry.py` | 修改 | 瞬时降级 completed 前 `verify_task_delivery`，未通过不降级并通过 `delivery_out` 回传核实结果 |
| `tests/runtime/test_issue_4440_delivery_verify.py` | 新增 | 38 用例：单元 15 + worker 10 + 降级 3 + 迟到提升 2 + direct 7 + 源级守卫 3 |

## 关键决策
1. 校验位置：收尾前统一入口（worker=complete_task / direct=终态写入前），
   两条路径调用同一函数，杜绝分叉（源级守卫测试锁住）。
2. 失败语义：fail-closed——无 PR、查询失败、核实链路自身异常一律未交付，
   不静默放行；不进 #403 瞬时重试（交付缺失不是上游抖动）。
3. 范围控制：按 skill 名匹配核实清单（默认 atomcode-bug-fix，env
   GRAPE_DELIVERY_VERIFY_SKILLS 覆盖），其他 Coding 工作流零改动；skill 为空
   显式 warning 留痕（fail-open 可观测）。
4. 复用：PR↔Issue 关联判据与 VCS client 工厂复用 issues_center
   （IM 通知同源），不新造查询链路；查询带墙钟超时（`GRAPE_DELIVERY_VERIFY_TIMEOUT_SECONDS`）。
5. 收敛：`is_delivery_unverified` / `delivery_unverified_error_entry` /
   `not_delivered_message` + code 常量 + `verify_coding_delivery_safe` 统一兜底；
   `_issue_text` 合并 PR 伪 id / 编号解析，消除多路径重复判定与硬编码。
6. 复审修复（第二轮）：无 issue_id fail-open、迟到提升通道、瞬时被拒口径统一；
   异常兜底仅对命中清单 fail-closed，未命中清单不误杀。`list_prs` 按 head/时间窗
   收敛列为 Out of Scope（可能漏判真实关联 PR → 反向误杀），保留有界全量翻页+超时。

## 测试与门禁
- `python3 -m pytest tests/runtime/test_issue_4440_delivery_verify.py -q` → 38 passed
- 相关回归（selfreport finalize / 2793 / transient_retry / 3820 / 3822）：111 passed
- AGENTS.md 回归套件（repogit / 3482 workspace order / 2245 git writable / 596 / 699）
  通过；其中 5 个失败经基线（stash）复核为存量失败，非本次引入
- `make lint` 通过；`python3 scripts/check-arch.py` PASS
