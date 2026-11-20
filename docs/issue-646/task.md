# Task Breakdown: Issue #646

- [x] T1 engine_uid.py 派生模块（uuid5/uuid4/校验）
- [x] T2 db_setup DDL + 3 条幂等迁移（加列/回填/MySQL FK）+ UNIQUE 索引
- [x] T3 user/db.py save_agent_runtimes uid upsert + 双读 helper + engine_ref_snapshot
- [x] T4 runtime.py 注册/心跳 uid 派生与双带返回
- [x] T5 agent_tasks 三处 INSERT 双写（engine_uid/名称快照/pool_engine_uids）
- [x] T6 scenes/agents 绑定入口 uid 支持与服务端校验
- [x] T7 测试 tests/atomic/test_issue_646_engine_uid.py 全绿 + 存量相关测试回归
- [x] T8 自审查（5 轴 + pre-pr 检查）→ 提交 → PR → 评论 → A2A QA
- [x] T9 复审整改：迁移 202611030004 三条 FK CASCADE→RESTRICT + 删除路径统一显式清理链
      （吊销 settings.revoke / devstage 删环境 / scope 切换重复行 / 刷新过期行 / 去重五路径
      统一接入 `_rebind_runtime_references` / `explicit_cleanup_runtime_references`，输出对账报告）

## 阶段 4 排期（后续提交节点，避免会话表 runtime_id 长期单轨）

- [ ] T10 会话表双读（明确排期：本 PR 合入后的下一个功能提交即启动，独立小步提交）：
      - `devops_agent_session` / `chat_agent_session` 增加 `engine_uid` 列（幂等加列迁移
        `202611030001`，模式复用 T2）；
      - `src/session_manager.py` / `src/api/routes/chat.py` 会话创建/读取路径双写双读
        （uid 优先、runtime_id 兜底，模式复用 T3/T5）；
      - 吊销/删除路径对账报告扩展 `sessions_rebound`/`sessions_reset` 到会话 uid 侧；
      - 验收：会话绑定引擎删除后按 engine_uid 快照可追溯，redispatch 重绑生效。
