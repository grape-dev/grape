# Task: Issue #4440 — 任务拆解与完成记录

| # | 任务 | 状态 |
|---|------|------|
| 1 | 新增统一交付核实入口 `delivery_verify.py`（核实清单 / PR 查询 / fail-closed 判定） | ✅ |
| 2 | worker 收尾路径挂钩：`service.complete_task` 成功记录前核实，未通过改 failed + 原因 + 错误码，关闭结构化复活通道 | ✅ |
| 3 | direct 收尾路径挂钩：`direct_executor._execute_agent_inner` 终态写入前同口径核实 | ✅ |
| 4 | 未通过不进 #403 瞬时重试（两条路径），直接判死留痕 | ✅ |
| 5 | 回归测试：三类场景 × 两条路径 + 未命中清单零改动 + 源级共用入口守卫 | ✅ |
| 6 | 复审修复：-15 结构化复活 / 瞬时降级两条成功通道纳入核实；成功副作用后移；PR 订阅双路径同口径；direct exit_code=1 + 崩溃兜底码；查询超时；判定收敛为常量/helper；skill 为空显式留痕 | ✅ |
| 7 | 门禁：新增测试 31 passed；相关回归 153 passed；make lint 通过；check-arch PASS | ✅ |
| 8 | 第二轮复审修复：无需求编号 fail-open 闭合（worker/direct）；迟到提升接入核实；瞬时被拒统一 exit_code=1 + 未交付前缀；异常兜底仅命中清单 fail-closed；伪 id 解析去重；测试 38 passed | ✅ |
