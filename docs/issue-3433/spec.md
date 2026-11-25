# Spec — Issue #3433 任务异常中断时透出可读失败原因

## 目标

`a2a_event` 等任务异常中断（exit_code=-1、非零退出、未捕获异常、被 kill）时，事件终态写入
结构化失败原因；前端事件详情页 failed 态展示失败原因、失败阶段与「重试 / 联系人工」入口。

## 现状

- 事件 seq（序号已脱敏）status=failed、exit_code=-1，result 只有执行中日志的中途截断文本，无终局错误信息。
- 前端 `#event/<seq 已脱敏>` 只展示中断态，用户无法判断是否可重试（30 分钟内重复反馈 3 次）。
- 现有 `update_event_complete` / `update_event_status` 已能兜底非空 result，但无结构化失败字段。

## 期望行为

1. 失败事件落库结构化 `failure_info`：异常类型、失败阶段（phase + phase_label）、原始报错摘要、
   runtime、agent、exit_code。
2. 成功与失败共用同一收尾写库入口（`update_event_complete`）；失败且调用方未提供结构化原因时，
   由收尾入口按 result/exit_code 自动兜底，保证任何中断路径都有归因。
3. 覆盖异常中断来源：派发/入队失败、prompt 构建失败、未捕获异常、非零退出、被 kill、超时、
   孤儿回收判死、人工终止。
4. 前端 failed 态：优先展示结构化失败卡片（原因 / 阶段 / 类型 / 引擎），提供「重试」「联系人工」
   入口；事件序号支持一键复制；历史事件缺 `failure_info` 时展示降级提示，不报错。
5. succeeded/completed 事件的展示与归档结构不变。

## 验收标准

- [ ] 人为 kill / 未捕获异常后，事件 `failure_info` 非空且包含 error_type/phase/message/runtime/agent。
- [ ] 同类失败事件在详情页可直接看到失败原因与失败阶段。
- [ ] failed 详情页有「重试 / 联系人工」入口，事件序号一键复制。
- [ ] 历史失败事件缺字段时展示降级提示。
- [ ] 现有测试全绿，新增失败归因单测与前端源码断言。

## 不做

- 不改变 succeeded/completed 的展示与归档结构。
- 不重构调度/派发核心逻辑。
- 不处理 #3417 / #2747 既有实现。