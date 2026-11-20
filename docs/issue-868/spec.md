# Spec: Issue #868 — T10 会话表双读 + claim-by-uid v2 设计 + MySQL 升级 runbook

> 源自 Issue Agent Spec（评论 📋 Spec Document，评级 ready）。开发基线：arch-optimization。

## Objective
- 落地 #646 排期中的 T10：`devops_agent_session` / `chat_agent_session` 增加 `engine_uid` 稳定键列，读写路径双写双读（uid 优先、runtime_id 兜底、旧库缺列降级），完成会话侧 uid 改造四阶段中的阶段 3→4 过渡。
- 交付 claim-by-uid 协议 v2 设计 spec（只设计不切协议）与 MySQL 生产升级运维 runbook。

## Scope
- In：会话表加列 + 幂等迁移（版本号 202611030005/6，与 arch 线已用 202611030001~4 区分）；session_store / session_manager / chat.py 双写双读；对账报告（孤儿 uid 置 NULL + 报告）；专项测试 + 纳入 linux-backend CI；两份文档。
- Out：阶段 5 删 id 读路径、bot_lease 方言 SQL 缺口（另行立 issue）。

## Scope 修订（2026-11-04，评论 id=198094644）
- 「前端 uid 消费」从 Out 移入 **In**：经核实 grape-dev 无独立前端仓，产品前端即本仓 `src/api/static/`（FastAPI 托管的内嵌静态页），不另立 issue。
- 实施对象：`src/api/static/js/pages/` 引擎/会话相关页面（chat.js / observability.js / dashboard.js / runtimes.js）——对引擎/会话的引用与展示改为 **engine_uid 优先、自增 id 兜底**，与后端 T10 会话表双读口径一致；改动点由 coding agent 核定：
  - 后端补 uid 出口：GET /chat/config 的 backends/selectedEngine 带 engine_uid（旧库缺列降级 ''）；PUT /chat/config 接受 engine_uid（uid 优先解析、runtime_id 兜底）；obs timeline 任务带 engine_uid；detached_engines 提示带 engine_uid。
  - 前端消费：chat.js 引擎选择器 uid 优先匹配与保存；observability.js 时间线引擎标注 uid 优先、#id 兜底；dashboard.js 解绑引擎提示 uid 优先；runtimes.js 引擎详情展示稳定键（uid 优先、#id 兜底）。
- 验收补充：前端引擎/会话相关页面 uid 优先、id 兜底改造完成，旧数据（uid 空）不报错，回退展示 #id。

## Scope 修订（2026-11-04，交叉预警，评论 id=198269670 / QA 复核 id=198272227）
- engine_uid 派生源修正入 **In**（Required，合流前修正）：uuid5 键的 `name` 段与 i5
  （feat/devbox-sandbox-integration5）云引擎四段名「改名跟随」机制冲突——云引擎
  （身份锚 engine_type/provider_id/template_id 透传在场）派生剔除 name 段改用不可变
  身份锚；普通引擎（无锚）legacy 公式保持不变（存量 uid 零漂移）。
- 配套：守卫测试（模板/Provider 改名前后 uid 不变 + legacy 公式回归锁定）；
  i5 合流迁移回填项（rebase_engine_uids_by_anchor 存量重算回填 + 注册路径
  legacy 命中即改绑的双读兼容）写入 claim-by-uid v2 spec §2.6 合流迁移清单。

## Scope 修订（2026-11-04，种子分量 id→uid 升格，评论 id=198288127 / QA 复核 id=198288768）
- 云引擎派生种子的 provider_id/template_id 自增 id 分量升格为
  provider_uid/template_uid（UUIDv4，与 credential_uid 同构）入 **In**
  （Required，合入前采纳；须在 claim-by-uid v2 端点实现之前完成）：
  - i5 侧（契约，feat/devbox-sandbox-integration5 工作线落地）：sandbox_provider_config
    加 provider_uid、sandbox_templates 加 template_uid（创建时一次性生成终身不变；
    backfill 沿旧行值按旧 id 映射拷贝、不新发 UUID）；worker 注册 payload runtime
    条目透传两 uid；身份锚 UPSERT 五元组继续用 provider_id（行内定位语义）。
  - grape 侧（本仓）：种子切 uid 公式（id/name 均不参与）；cloud_anchor_from_runtime_info
    提取 uid 表示（id 回退过渡）；rebase_engine_uids_by_anchor 接受 uid 分量、
    旧行 uid 行内读取的映射式回填（禁止按 runtime_id 重派 JOIN）；守卫测试扩展
    （表重建/库恢复 id 换值 uid 不变用例）。设计详录 claim-by-uid v2 spec §2.7。

## 技术方案
- `engine_uid` 语义复用 #646：VARCHAR(36)，UUIDv5 自然键派生，全链路不新造派生。
- 迁移 202611030005（safe 加列+索引，模式复用 #646 T2）+ 202611030006（backfill 回填 + 孤儿 uid 置 NULL 入报告）。
- 双读复用 #646 既有 seam：`get_engine_uid_by_runtime_id` / `get_runtime_id_by_engine_uid`；写路径在落 runtime_id 的同时镜像 engine_uid。
- 对账扩展：`_rebind_runtime_references` 会话步骤同步 uid 轨道（改绑指向保留行 uid；孤儿 uid 置 NULL 并入报告）。

## 验收标准
- [ ] 两张会话表新增 engine_uid 列，迁移幂等可重跑，列未建成不盖章
- [ ] 会话写入双落 uid；读取 uid 优先、旧库缺列回退 id 不报错
- [ ] 对账报告覆盖会话表孤儿 uid（置 NULL + 报告，不静默丢弃）
- [ ] tests/atomic/test_issue_868_session_engine_uid.py 通过并纳入 linux-backend CI
- [ ] claim-by-uid v2 协议设计文档成文（双端点并存期、uid 失效回退 runtime_id）
- [ ] MySQL 生产升级 runbook 成文（ALTER 步骤/锁表窗口/回滚/核验）
- [ ] 存量测试回归通过

## 测试 seam 声明
- 迁移：`db_setup._migration_868_add_session_uid_columns / _migration_868_backfill_session_uids`（conn 注入，同 #646 模式）
- 双写双读：`session_store.get_or_create_agent_session / upsert_agent_session`、`session_manager.get_chat_config / _write_chat_config_row`（monkeypatch `session_manager.db`）
- 对账：`user/db._rebind_runtime_references` 返回报告
