# Issue #1011 方案设计（Plan）

> 需求见 spec.md（基线 = Issue 正文）。本文为技术方案。

## 总体顺序（实现路径 0→4）

0. **派生源修正收口（db_setup.py）**
   - 新迁移 `202611040001 issue-1011-engine-anchor-columns`（safe）：agent_runtimes 加 5 列
     anchor 快照（engine_type / provider_id / template_id / provider_uid / template_uid）
     —— 注册路径（runtime.py register）持久化锚，使派生源可离线重算。
   - 新迁移 `202611040002 issue-101101-backfill-rederive-engine-uids`（backfill）：
     按当前 derive_engine_uid 公式全量重派生 agent_runtimes.engine_uid
     （uid 锚 → uid 公式；id 锚 → id 公式；无锚 → legacy 公式=原值零变更），
     uid 变化行经 rebase_engine_uid_references 改绑全部引用轨道（映射式，旧行 uid 从行内读取），
     对账报告落盘（复用 `_write_uid_migration_report_file`，tag=1011）。
   - 报告补写：启动时对 646/868/1011 三个 tag 的迁移报告做「已盖章且报告文件缺失 → 补写空报告
     （注明『迁移已于先前版本执行』，幂等：文件已存在不重写）」。
1. **v2 端点（runtime.py）**：路由层薄壳 + uid→行解析 helper（`_resolve_runtime_by_uid`），
   bound 归属校验 uid 口径（`_require_runtime_owned_uid`），v1 路由与 service.claim/claim_by_tag 零改动。
2. **heartbeat-v2**：新 body 模型（HeartbeatBodyV2 = HeartbeatBody + engine_uids）+ 独立端点；
   解析 uids→ids（credential 归属内）与 runtime_ids 合并去重后走与 v1 相同的更新逻辑
   （提取 `_apply_heartbeat_runtimes(c, ids, ...)` 公共函数，v1/v2 共用）。
3. **autopilot**：db_setup 加列 `engine_runtime_uids`；routes 校验 uid-first + 双非空拒绝 + 写侧升格；
   scheduler 入队 uid 模式 → pool_engine_uids 镜像（enqueue_task 已支持参数）；
   升格脚本 scripts/upgrade_autopilot_engine_uids.py（幂等 + 悬空报告）；
   前端 autopilot.js uid 优先 id 兜底。
4. **spec 勘误 + worker**：v2 spec §2.1/2.2 换真实路径；RuntimeClient 增加 v2-first 调用
   （uid 从 register 响应持久化到本地 JSON 文件；capabilities.uid_claim 探测；
   空白 404/405 → 记忆切换 v1；UNKNOWN_ENGINE_UID → 回退 v1）。

## 关键设计决策（照 Issue 正文「四、Technical Approach」）

- v2 路径形态：路径参数 uid（/runtimes/uid/{uid}/tasks/claim 系）。
- 失效语义：4xx fail-fast（404 UNKNOWN_ENGINE_UID）。
- heartbeat v2：独立端点 `/heartbeat-v2`。
- autopilot 存储：姊妹字段。
- 测试先行红→绿；全部纳入 tests/atomic + linux-backend CI。
