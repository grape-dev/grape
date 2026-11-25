# 任务拆解 — Issue #3433

- [ ] T1 失败归因模块 `failure_info.py`（构造/兜底/分类/解析） + 单测
- [ ] T2 events.failure_info 列（DDL + 启动补列 + 注册迁移）
- [ ] T3 收尾写库统一支持 failure_info（update_event_complete / update_event_status + 兜底）
- [ ] T4 异常中断调用点接入（派发/prompt/未捕获异常/非零退出/kill/超时/回收/终止）
- [ ] T5 事件详情 API 返回 failure_info
- [ ] T6 前端 failed 结构化卡片 + 重试/联系人工 + 序号复制 + 历史降级
- [ ] T7 前端 locale（zh/en）与 JS 源码断言
- [ ] T8 回归测试 + 自审查 + 建 PR