# Plan: Issue #4440 — Coding 交付核实统一入口

## 方案
统一入口 + 双路径前置挂钩（见 spec.md）。

## 改动清单
| 文件 | 类型 | 说明 |
|------|------|------|
| `src/domains/runtime/engine/delivery_verify.py` | 新增 | 唯一交付核实入口：核实清单、PR 查询（区分无 PR / 查询失败）、fail-closed 判定 |
| `src/domains/runtime/engine/service.py` | 修改 | `complete_task()`：#403 重试前核实，未通过 exit_code=1 + 事件 result 前缀 + errors 结构化错误码；关闭结构化产出复活通道 |
| `src/domains/runtime/engine/direct_executor.py` | 修改 | `_execute_agent_inner`：终态写入前同口径核实，未通过 new_status=failed + result 前缀 + `_append_task_error` 落错误码；不进瞬时重试 |
| `tests/runtime/test_issue_4440_delivery_verify.py` | 新增 | 25 用例：单元 12 + worker 6 + direct 4 + 源级守卫 3 |

## 关键决策
1. 校验位置：收尾前统一入口（worker=complete_task / direct=终态写入前），
   两条路径调用同一函数，杜绝分叉（源级守卫测试锁住）。
2. 失败语义：fail-closed——无 PR、查询失败、核实链路自身异常一律未交付，
   不静默放行；不进 #403 瞬时重试（交付缺失不是上游抖动）。
3. 范围控制：按 skill 名匹配核实清单（默认 atomcode-bug-fix，env
   GRAPE_DELIVERY_VERIFY_SKILLS 覆盖），其他 Coding 工作流零改动。
4. 复用：PR↔Issue 关联判据与 VCS client 工厂复用 issues_center
   （IM 通知同源），不新造查询链路。

## 测试与门禁
- `python3 -m pytest tests/runtime/test_issue_4440_delivery_verify.py -q` → 25 passed
- AGENTS.md 回归三件套 + 596/699 守卫：本次改动相关全部通过
  （test_596/699 中 5 个失败经基线复核为存量失败，非本次引入）
- `make lint` 通过；`python3 scripts/check-arch.py` PASS
