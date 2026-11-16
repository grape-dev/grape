# Plan: Issue #1208 Worker 标签化调度实现方案

## 前置确认
- 步 0（T10 会话表双读，#646 尾项）：已核对基线 arch-optimization，#868（T10）迁移 202611030005/6 与 session_manager/chat.py 双读已在基线合入 → 前置满足，本 PR 不再动会话表。
- 迁移版本号：取 202611050001/2（全新日期号段，两线天然区分，#646 重编先例）。

## 切片划分（TDD，红/绿分提交）

| # | 切片 | RED（先提交失败测试） | GREEN（最小实现） |
|---|---|---|---|
| S1 | 匹配函数 + pool_selector 列 | tests/atomic/test_issue_1208_selector_match.py（组内 OR/组间 AND/空 selector/单维度单值/多维度混合/未知前缀分组） | engine_select._selector_match + db_setup 202611050001（pool_selector TEXT NULL 幂等加列） |
| S2 | agent_tags 三维度派生 | tests/atomic/test_issue_1208_agent_tags.py（engine:/scope:/scene: 派生、心跳刷新、get_tags 过滤冒号行、all 保留） | src/runtime/agent_tags.py（新模块：派生+落 capabilities 冒号行+JSON 快照刷新）；runtime.py register/heartbeat 挂点；runtime_get_tags NOT LIKE '%:%' |
| S3 | 入队三分叉 + fail-closed | tests/atomic/test_issue_1208_enqueue_selector.py（selector 命中落 pool_selector、零命中不入队 event failed、配置死/瞬时死分流、uid 轨回归、id 轨零变化） | engine_select.get_scene_engine_selector/get_scene_engine_uids/selector_candidate_engines/selector_enqueue_failure_reason + get_scene_engine_binding uids 优先 + event_pipeline 挂点 |
| S4 | claim 六层守卫 | tests/atomic/test_issue_1208_claim_selector.py（三级顺序：能力→角色→selector；FIFO 跳过不阻塞；bound claim fail-closed；selector 任务两池列空不误杀；stale 降级+文案分流；redispatch 重校验语义） | service.py：_TASK_COLUMNS+pool_selector、enqueue_task 形参、_pool_selector_blocks、_check_pool_selector_reachable、claim/claim_by_tag 守卫、_check_stale_queued_tasks 扩展、_fail_task_pool_unreachable selector 文案 |
| S5 | 保存端三层 + 双非空 | tests/atomic/test_issue_1208_save_validation.py（双非空 400、L1 未知前缀/值域外/格式错 400+合法值列表、uid 透明升格落库回读、SRE selector 空绑定语义） | scenes.py SceneNodeInput 加字段 + _validate_scene_engine_ids 扩展 + 保存升格；agents.py _update_scene_engine_ids 升格 uid；dashboard.py agent_engines 带 engine_uid |
| S6 | T11 存量升格脚本 | tests/atomic/test_issue_1208_migrations.py（幂等两跑 uid 稳定、悬空跳过+报告、无绑定不动、升格前后派发同批引擎） | db_setup 202611050002 backfill（分批+报告落盘） |
| S7 | 前端模式 radio + 命中预览 | tests/js/test_issue_1208_engine_mode_radio.js（node 断言：radio 默认按标签、切换清空、复选组三维度、预览分层排除原因、uid 输出） | dashboard.js _openEngineModalImpl 改造 + node 保存 engine_selector/engine_runtime_uids + 徽标消费 engine-status |
| S8 | 收尾 | 全量相关 pytest 回归 + js 测试；自审查 5 轴 + pre-pr 增量检查；PR + Issue 回帖（#827 格式验证明细）+ A2A QA |

## 测试 seam
- 匹配层 seam：`engine_select._selector_match`（纯函数，最高优先复用）。
- 调度层 seam：`service.claim_by_tag` / `service.enqueue_task`（既有公开接口，测试库 monkeypatch 模式复用 tests/test_issue_253_engine_binding.py 的 setup_db）。
- 保存层 seam：`scenes._validate_scene_engine_ids` + PUT /scenes/{id}（FastAPI TestClient 或直调函数）。
- 前端 seam：node 纯函数/渲染字符串断言（tests/js node 直跑，同既有先例）。

## 风险对策（对齐 §5）
- 存量回归：id 分叉判据用 `engine_selector` 非空短路，legacy 代码路径零触碰；E2E-6 stash 基线对照由 QA 看护。
- uid 轨误杀：验收 4 专项单测（selector 任务 pool_engine_ids/uids 均空 + claim 放行）。
- MySQL TEXT DEFAULT：pool_selector 一律 TEXT NULL 无 DEFAULT（#627）。
