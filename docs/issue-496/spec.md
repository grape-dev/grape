# Spec: Issue #496 — sre 在「PR merged → sre 收尾」链路系统性执行失败（反馈 #31）

> 承接 Issue Agent Spec（评论 2026-10-29 00:08）| 目标分支: weekly-08-mys（场景注入基线，weekly-08 同族）

## 目标
定位并修复 pull_request_merged 触发的 sre 收尾事件批量 failed（exit_code 异常、seq 29249/28353/15625/14741/13993/13159/13104），并补充回归用例。

## 根因（日志证据，`grape events detail --seq <n>`）
| seq | 结果 | 归类 |
|-----|------|------|
| 28353 / 14741 / 13993 / 13159 | `场景绑定的引擎全部离线（scene=…, agent=sre）` exit=1 | **派发级：#259 方案A 即时终态 failed，无重试**（本修复主因，4/7） |
| 29249 | SRE 本地部署 `git merge` 冲突按技能硬规则中止 exit=1 | 业务性失败（需人工解决冲突），保留 failed 语义 |
| 15625 | `engine exited 0 with empty output` | 执行级瞬时异常，已由 #403 transient_retry 覆盖 |
| 13104 | 任务行无 started_at / 无 result（历史僵尸） | 历史存量，无复现路径 |

统一失败点：PR merge webhook 是一次性投递；派发时命中 #259「场景引擎全灭 → 立即 `_fail_event` 终态」后没有二次触发机会，引擎离线窗口内的每一次 PR 合并其 sre 收尾事件都永久 failed（event_pipeline.py 原 L2298-2304 分支）。

## 修复方案
保留 #259「宁失败不跑飞」的即时终态语义（防僵尸 running / 防并发滑入），为 `pull_request_merged` + `sre` 单链路追加**派发级自动重试**（新模块 `src/api/pr_merged_sre_retry.py`）：
1. 离线 fail 时安排 threading.Timer 指数退避轮询（默认 6 次：60/120/240/300×3s，窗口 ≈22min，环境变量 `GRAPE_PR_MERGED_SRE_RETRY_ATTEMPTS/_DELAY` 可调）；
2. 引擎池恢复（`get_scene_pool_engine_ids` 非 None 且 `pick_engine` 有引擎）→ `reset_event_terminal` 复位事件 → 按 run_pipeline 原派发段语义重新入队（广播/远程定向）或直连执行；
3. 重派前置校验：事件仍为 failed 且无活跃任务（防人工重派后重复派发）；重派异常 fail-close 收口，不产生僵尸 running；
4. 进程重启丢 timer 至多退化为旧行为（failed 可人工重派），不会更差。

## 范围
- In：上述派发级自动重试；fail 文案追加「已安排自动重试」；回归测试。
- Out（边界）：#494 超时归因透出、#495 worker CLI 健康；#403 执行级 transient_retry；29249 类 SRE 部署业务失败语义。

## 验收
- 根因定位并附日志证据（本文件根因表）
- 派发级引擎离线不再永久 failed：恢复后自动重派、事件转 running 并正常执行
- 回归用例合入（tests/test_issue_496_pr_merged_sre_retry.py，6 例）
- #259 / #403 / 重派 / 事件守卫等既有回归全部通过
