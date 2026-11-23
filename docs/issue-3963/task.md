# Issue #3963 任务拆解（TDD 红/绿分提交）

## 切片 1：抢锁等待预算（kernel 原语）

- [x] 红：`acquire_lock_with_retry` 给定 `wait_seconds` 时按预算等待并成功；默认路径保持 `attempts×interval` 语义。
- [x] 绿：`src/kernel/lock.py` 新增 `wait_seconds` / `jitter_ratio`。（commit 82ddb829e）

## 切片 2：enqueue 排队等待 + 结构化 busy + 指标

- [x] 红：`service.enqueue_task` 持锁 2s 场景等待后成功；预算耗尽抛 `RepoDispatchBusyError`
      （`error_code=REPO_DISPATCH_BUSY`，文案保留 `task enqueue busy for repo`）；`busy_total` 自增；
      调用点预算 ≥ TTL；同仓 5 并发（其一持锁 2s）其余 4 个全部入队。
- [x] 绿：`service.py` 常量 + 调用点；`observability` 两个指标 + `insight.public` 转发。（同上）

## 切片 3：调度侧瞬态通道

- [x] 红：`max_retry=0` 遇 busy 仍成功派发（不落 failed）；永久失败仍 failed；busy 预算耗尽 →
      `error_code=REPO_DISPATCH_BUSY` 且 exec 非 failed、任务不推进失败计数/不退避熔断、
      `next_run` 推进到下一 tick、日志无 `failed after 0 retries`。
- [x] 绿：`scheduler.py` 常量 + `_enqueue_failure_code` + `_classify_env_error` 指纹 + `_execute_task` 循环改造。（commit 9ef0a8db4）
- [x] 绿：`deploy/alerts.yml` 连续 busy 告警。（同上）
- [x] 补：远端派发真实写路径用例（busy → skipped + error_code）。（commit 9dbd9c216）

## 验证

- [x] `tests/runtime/test_issue_3963_repo_dispatch_lease.py` → 18 passed
- [x] `tests/runtime/test_issue_3800_autopilot_offline_backoff.py` → 8 passed（#3800 回归）
- [x] `make lint` 通过（import OK / undefined-name 0 / async-blocking 未新增）
- [x] 环境性既有失败已按 baseline 复核（claim 10 例、test_runtime 5 例、arch 2 例、insight skills obs 等与本次改动无关）

## 收尾

- [x] 自审查（5 轴 + 开源前置增量检查）
- [ ] 建 PR（base=`arch-weekly-11-ljt`）+ Issue 评论 + A2A QA