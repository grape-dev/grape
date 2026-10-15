# Spec: Issue #317 — 引擎异常退出错误信息补采与自愈

> 继承 Issue #309 已收敛 Spec（Issue Agent 已发布于 #317 评论，评级 ready），目标分支 weekly-06。

## 目标
修复引擎异常退出（exit 1，stderr 为空、result 截断）无遗言问题：
1. exit != 0 且 stderr 为空时，补采 agent 执行日志尾部（末 50 行 / 4KB 截断）写入 errors；
2. returncode == -9（SIGKILL）结合系统日志启发式检查，标记 `suspected OOM`；
3. 此类异常退出自动幂等重试一次（重试发生在同一事件执行内，event_active_guard 已由 pipeline 抢占并持有至终态，天然防重复事件）。

## 现状
- `src/agent/executor.py` `_run_once` 末尾 done 事件（L794 附近）：exit != 0 时仅透传 returncode 与 stderr，stderr 为空时 errors 无任何线索；
- 已观测 seq 10353 / 10392 两起异常退出：result 半截、errors 为空，事后无法定位根因。

## 期望行为
- exit != 0 且 stderr 为空 → done.errors 追加 `agent_logs tail (last 50 lines/4KB)` 摘要（含 OOM 判定标记）；
- returncode == -9 且系统日志（dmesg/journalctl/内核日志文件）含 oom-killer 证据 → errors 标记 `suspected OOM`；-9 无证据 → 标记 `killed by SIGKILL (no oom-killer evidence)`；
- 异常退出（exit != 0 且 stderr 空）自动重试一次（不带 session），重试结果作为唯一 done 反映在事件状态；重试与既有 session_missing 重试共用"每流最多一次重试"额度，保证幂等（单一 done、不重复派发事件）。

## 验收标准
- [ ] exit != 0 且 stderr 为空时，errors 包含 agent 日志尾部摘要
- [ ] -9 退出被识别并记录（suspected OOM / SIGKILL 无证据两分支）
- [ ] 自动重试一次，重试结果正确反映在事件状态
- [ ] 重试幂等：单一 done、不产生重复事件（event_active_guard 作用域内）
- [ ] 现有测试全部通过
- [ ] 新增单测覆盖：空 stderr 补采 / OOM 标记 / 重试幂等

## 影响模块
| 模块 | 改动 |
|------|------|
| src/agent/executor.py | 修改：遗言采集 + OOM 判定 + 异常退出重试 |
| tests/test_agent_executor.py | 新增：三场景单测 |

## 优先级
P1（线上问题定位效率，已有多起无遗言事件）
