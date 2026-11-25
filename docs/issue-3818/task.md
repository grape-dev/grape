# Issue #3818 任务拆解

- [x] T1 载荷审查纯函数模块（归一化 + 规则 + 脱敏 + hash + prompt 反抽）
- [x] T2 熔断台账模块（runtime 表、阈值/开关、dead-letter、fail-open）
- [x] T3 表 DDL / 存量迁移 / 归属登记
- [x] T4 入库侧审查（create/update 400 + 审计；读侧 dispatchable 口径）
- [x] T5 派发侧闸门（scheduler 建 exec 前 + 手动触发；同载荷失败登记）
- [x] T6 prompt 打点元信息化（direct_executor）+ 执行详情脱敏（task_logs）
- [x] T7 回归测试（规则/熔断/闸门/路由）+ 现有 autopilot 套件与架构门禁
- [ ] T8 运维止血：删除任务 79、吊销 obs AK、失效存量预签名 URL、清理 Loki 日志（本单外，需回执）
