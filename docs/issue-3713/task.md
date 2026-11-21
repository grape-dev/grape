# Issue #3713 · 任务拆解与完成状态

| # | 任务 | 状态 | 载体 |
|---|------|------|------|
| 1 | 旧码分叉点探针（换券 vs 建树，五场景） | ✅ | 临时脚本（不入库），产物见 PR 说明 |
| 2 | 守卫用例 1-6（跨入口同日断言） | ✅ | `tests/api/test_issue_3713_dual_entry_credential.py` |
| 3 | 守卫用例 7（A2A 鉴权面：rc 过期不外发旧值） | ✅ | 同上（`test_no_global_face_drops_expired_explicit_value`） |
| 4 | 身份面回归（`get_bot_identity_token` / bypass 路径） | ✅ | 同上（`test_identity_face_unchanged_with_explicit_row`） |
| 5 | 链首 `ExplicitRepoBotToken` + `resolve_exec_token` | ✅ | `src/kernel/token_resolver.py` |
| 6 | 换券路由删除三段实现，改调选择器 | ✅ | `src/domains/mgmt/routes/repos.py` |
| 7 | `create_action_handler` 认识 `rc_bot` | ✅ | `src/kernel/token_resolver.py` / `tests/kernel/test_issue_3713_action_handler_level.py` |
| 8 | `get_repo_bot_token_no_global` 过期语义收口 | ✅ | `src/domains/mgmt/user/db.py` |
| 9 | 任务面 stage 进 task errors（不再渲染 git 元数据只读） | ✅ | `repogit.worktree_credential_error()` / `agent.credential_block_result()` / `tests/runtime/test_issue_3713_task_credential_stage.py` |
| 10 | 旧「两份实现」用例改写成新契约 | ✅ | `tests/api/test_git_credential_token_plane.py` |
| 11 | 全量回归（api / mgmt / kernel / runtime） | ✅ | 见 PR 说明的回归记录 |
| 12 | 自审查（五轴） | ✅ | 见 PR 说明 |
| 13 | PR 创建 + Issue 评论 + A2A 交办 QA | ✅ | PR 链接见 Issue 评论 |
| 14 | demo 实测（`gitee:<ACCOUNT>/demo`：换券 200 / `get_task_token` 非空 source=rc_bot / 建树 refs 非 0） | ⏳ 待部署环境 | 需要 worker 环境与真实 rc 行，交由 QA/运维在 dev 复核 |