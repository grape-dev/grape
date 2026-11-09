# 模型流瞬时异常自动重试（Issue #403）设计文档

配套需求：docs/weekly-08/model-stream-retry-requirements.md（R50-R53）

## 决策表

- D1 重试发生层：判定层收口（complete_task / fail_task / event_pipeline direct 收尾），不做 ACP 层会话重发。理由：引擎层无副作用边界感知，历史上 49 次 requeue 造成重复评论污染（service.py L2062 注释）。
- D2 重试计数：复用 agent_tasks.attempts 列。理由：schema 零变更（PRAGMA 实测列已存在），语义一致（每次重跑 +1）。既有超时回池路径同用该列，上限同为 3，天然合流。
- D3 退避机制：threading.Timer 延迟 requeue（任务保持 running）。理由：不引入新调度设施；退避窗口 ≤300s 远小于僵尸判死阈值（30min/6h）；进程重启丢 timer 由既有僵尸兜底收口，属可接受降级。
- D4 副作用闸门：写操作证据 = 结构化产出（actions+conclusion）∨ tool_uses 写操作（POST comments/pulls、A2A handoff）。理由：与假完成硬门槛（event_pipeline L3987-3998）共用同一证据定义，不造第二套口径。
- D5 瞬时判定：静态特征表 + is_auth_401_error 负向排除；未命中特征一律不重试（保守面，防误重试业务失败）。
- D6 提示通道：events.result + agent_tasks.errors 既有字段为主；task:retry 事件经既有
  日志渲染链透出，events.js 仅做图标/颜色/字段回退三点小适配（非新前端）。

## 改动清单

新增 src/runtime/transient_retry.py：
- TRANSIENT_PATTERNS / classify_stream_failure(exit_code, error_text, has_output)
- has_write_evidence(tool_uses, result_text)
- retry_transient_task(task_id, exit_code, error_text, has_output, tool_uses) -> bool
  （分类 → 闸门 → attempts 检查 → 记录提示 → Timer 退避 requeue）
- _do_requeue(task_id)：status='queued', started_at=NULL, runtime_id=0, attempts+1,
  stream_ended=0, self_reported=0, exit_code=NULL, result_text=''；事件回 pending；
  释放 busy 槽（复用 release_busy_slot）。

改动（3 个接入点，均为「终态写入前拦截」）：
- src/runtime/service.py fail_task：过期上报检查之后、finalize 之前。
- src/runtime/service.py complete_task：业务失败覆盖之后、status 分流（L2065）之前。
- src/api/event_pipeline.py direct 执行收尾：new_status 判定完成之后、update_task_status（L4021）之前。

不改动（防回归清单）：
- src/agent/hwcloud_acp.py / hermes_acp.py（exit_code 判定、seq#559 错误透传、PREMATURE_END_TURN）。
- runtime/cleanup.py 全部兜底判死（双信号/6h/30min）。
- requeue_task / worker claim 冷却 / 假完成硬门槛本体。
- events 表 schema。

配置：GRAPE_TRANSIENT_RETRY_MAX（默认 3）、GRAPE_TRANSIENT_RETRY_BASE（默认 10s，封顶 300s）。

## 兼容/降级矩阵

- 成功路径：零改动（拦截只在 failed 分支生效）。
- 退避窗口内主控重启：timer 丢失，任务停留 running → 既有僵尸回收（30min 引擎离线 / 6h 绝对上限）兜底，不会永久卡死。
- 双主 scp：attempts 更新在 db_lock 内单条 UPDATE，与既有幂等契约同构。
- worker 补报重放：终态幂等（已 failed 跳过）先于重试检查，重放不触发重复 requeue。
- Timer 线程安全：_do_requeue 全程 db_lock；Timer 泄漏风险 = 每 task 至多 3 个一次性 timer。

## 测试设计

- T1 classify_stream_failure：IAM/连接/5xx/超时 → transient；401 → auth_config；普通文本 → task_failure；exit 0 空 output → empty_output。
- T2 retry_transient_task：瞬时+零证据 → True，attempts+1、状态 queued、字段复位、errors 含 TRANSIENT_RETRY、事件回 pending；写证据 → False；401 → False；attempts≥3 → False。
- T3 退避：delay 序列 10/20/40 封顶 300（纯函数 _backoff_delay 单测，不起真 timer）。
- T4 fail_task / event_pipeline 收尾接入：瞬时失败走 requeue 分支（mock _do_requeue），非瞬时走原判死分支。
- T5 环境变量覆盖 MAX/BASE。

## 性能与风险

- 每次失败多一次特征正则扫描（<1ms 量级），无热路径影响。
- 最坏伤害半径自审：重试风暴受 attempts≤3 + 300s 退避约束；状态翻转竞态被终态幂等 + 只拦 running 收口；副作用放大被 D4 闸门阻断（有写证据不重试）。
