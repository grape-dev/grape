# Issue #1011 需求文档（Spec）

> 基线：Issue #1011 正文（完整 spec 已在 Issue 正文，本文为开发侧要点摘录，权威以 Issue 正文「三、Scope / 五、Acceptance Criteria / 七、用例矩阵」为准）。

## 目标

在 #868 既有交付之上完成 uid 改造下一阶段代码落地：

1. **In Scope 0**：engine_uid 派生源修正收口（存量重派生回填 + 已盖章库迁移报告补写）。
2. **In Scope 1**：claim-by-uid 协议 v2 端点（claim / claim-by-tag / claim-bound / claim-by-tag-bound / claim-capabilities / heartbeat-v2 / complete-v2 / fail-v2 / progress-v2），双端点并存 + uid 失效语义（回退 id / 双失效 404 UNKNOWN_ENGINE_UID）+ uid_claim 能力位。
3. **In Scope 2**：autopilot 前后端 uid 消费收尾（姊妹字段 engine_runtime_uids 方案）+ 存量升格脚本。
4. **In Scope 3**：v2 协议 spec（docs/issue-868/claim-by-uid-v2-protocol-spec.md）端点路径勘误。
5. **In Scope 4**：worker 侧 v2-first / v1-fallback 消费（404 语义二分）。

## 关键语义（照 Issue 正文）

- v2 路由寻址键 = engine_uid（路径参数 str），内部解析为 runtime_id 后**复用与 v1 完全相同的服务层逻辑**（语义零漂移）。
- uid 命中 → 认领，payload 双带 id+uid 三键（engine_uid / engine_name_snapshot / pool_engine_uids，#646/#868 已有）。
- uid 失效 + body.runtime_id 命中 → 回退 id 认领（并存期兜底）。
- 双失效 → 404 + 错误码 UNKNOWN_ENGINE_UID，绝不静默 200 空任务。
- heartbeat-v2：engine_uids 优先解析、失效回退 runtime_ids、全失效条目不造行（日志取证）。
- 上报 v2（complete/fail/progress）：按 uid 校验任务归属（任务行 engine_uid 与上报引擎 uid 不一致 → 403/409）。
- 404 语义二分：空白 404（`{"detail":"Not Found"}`）= 旧服务端（worker 记忆切换 v1）；body 带 UNKNOWN_ENGINE_UID = uid 失效（回退 v1，v1 再 404 才重注册）。
- autopilot 姊妹字段：engine_runtime_uids 与 engine_runtime_ids 并存；读侧 uids 非空优先；写侧升格（写 uids 清空 ids）；双非空 400 拒绝；入队 uid 模式 → uid 池路径（pool_engine_uids 镜像）。

## 测试 seam

- 服务端：HTTP 端点层（FastAPI TestClient / 直接调用路由函数 + service 层）+ db_setup 迁移函数。
- worker 侧：RuntimeClient（封装 v2-first/fallback 决策，可单测）。
- 前端：autopilot.js 的 payload 构造/展示降级函数（js 测试沿用 #868 模式）。
