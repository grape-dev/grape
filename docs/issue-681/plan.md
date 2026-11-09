# Plan: 智能推荐入口判定换用真实绑定查询（Issue #681）

## 实现路径
1. `src/api/session_store.py`：在 `get_issue_scene` 后新增 `get_issue_bound_scene`，复用其前两层查询逻辑（PR 订阅伪 id 解析 → issue_state.scene_id → issue 表 scene_id），去掉 dev_scene 回退分支；docstring 明确"推荐入口专用，勿用于展示/A2A"。
2. `src/api/event_pipeline.py`：issue_opened 推荐入口判定（`_is_scene_recommend` 重试循环内）`get_issue_scene` → `get_issue_bound_scene`；其余 16 处 `get_issue_scene` 调用不动（含 L2184 场景解析、`_redispatch_vod_after_recommend` 重派场景解析——均为展示/A2A/派发兜底语义）。
3. worker 核查：`src/runtime/service.py` `_handle_vod_recommend_complete` 及 direct 路径（event_pipeline L4202-4252）均直接解析推荐结果写回，无"是否已有场景"同款判断，无需改动。
4. 测试：`tests/test_issue_bound_scene.py` 三态 + 415 回归 + pipeline 入口行为测试（harness 对齐 test_bot_opened_recommend.py）。

## 设计决策
- 新函数隔离语义而非删回退：57 处调用方依赖 get_issue_scene 回退语义（#415 修复），删除会恢复老 bug。
- PR 订阅伪 issue_id 在新函数中保留解析：场景 ID 编码在伪 id 内，属真实绑定语义。
- issue 表为权威源（Phase C3）：issue 行存在时 issue_state 不参与读取，与 get_issue_stage 一致。

## 风险
- 新函数与 get_issue_scene 混用 → docstring 明确用途边界。
- 回归风险（展示回退 / #565 bot 建单）→ 已全量回归相关测试。
