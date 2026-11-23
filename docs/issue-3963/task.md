# Issue #3963 任务拆解（TDD 红/绿分提交）

## 切片 1：抢锁等待预算（kernel 原语）

- [ ] 红：`acquire_lock_with_retry` 给定 `wait_seconds` 时按预算等待并成功；默认路径保持 `attempts×interval` 语义。
- [ ] 绿：`src/kernel/lock.py` 新增 `wait_seconds` / `jitter_ratio`。

## 切片 2：enqueue 排队等待 + 结构化 busy + 指标

- [ ] 红：`service.enqueue_task` 持锁 2s 场景等待后成功；预算耗尽抛 `RepoDispatchBusyError`
      （`error_code=REPO_DISPATCH_BUSY`，文案保留 `task enqueue busy for repo`）；`busy_total` 自增；
      调用点预算 ≥ TTL；同仓 5 并发（其一持锁 2s）其余 4 个全部入队。
- [ ] 绿：`service.py` 常量 + 调用点；`observability` 两个指标 + `insight.public` 转发。

## 切片 3：调度侧瞬态通道

- [ ] 红：`max_retry=0` 遇 busy 仍成功派发（不落 failed）；永久失败仍 failed；busy 预算耗尽 →
      `error_code=REPO_DISPATCH_BUSY` 且 exec 非 failed、任务不推进失败计数/不退避熔断、
      `next_run` 推进到下一 tick、日志无 `failed after 0 retries`。
- [ ] 绿：`scheduler.py` 常量 + `_enqueue_failure_code` + `_classify_env_error` 指纹 + `_execute_task` 循环改造。
- [ ] 绿：`deploy/alerts.yml` 连续 busy 告警。

## 收尾

- [ ] 自审查（5 轴 + 开源前置增量检查）
- [ ] 建 PR（base=`arch-weekly-11-ljt`）+ Issue 评论 + A2A QA