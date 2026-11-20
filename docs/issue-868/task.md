# Task Breakdown: Issue #868

- [x] T1 红测试：tests/atomic/test_issue_868_session_engine_uid.py（迁移/双写双读/对账）
- [x] T2 迁移 202611030005/6 + DDL + chat_agent_session 兼容列
- [x] T3 session_store / session_manager / chat.py 双写双读（uid 优先、缺列降级）
- [x] T4 _rebind_runtime_references 会话 uid 轨道对账扩展
- [x] T5 文档：claim-by-uid v2 协议 spec + MySQL 升级 runbook
- [x] T6 CI 纳入 linux-backend（沿用 066c6e8f9 模式）
- [x] T7 全量回归 + 自审查（5 轴 + pre-pr）→ push → PR → 评论 → A2A QA

## Scope 修订追加（评论 id=198094644：前端 uid 消费入 In Scope）

- [x] T8 红测试：tests/atomic/test_issue_868_frontend_uid.py（/chat/config uid 出口、PUT uid 优先解析、obs timeline / detached hints uid 字段、旧库缺列降级）
- [x] T9 后端 uid 出口：chat.py GET /chat/config backends/selectedEngine 带 engine_uid；PUT /chat/config 接受 engine_uid（uid 优先、runtime_id 兜底）；observability/trace.py 任务项带 engine_uid；engine_select.get_detached_engine_hints 带 engine_uid
- [x] T10 前端消费：chat.js 引擎选择器 uid 优先匹配/保存（id 兜底）；observability.js 时间线 uid 优先标注；dashboard.js 解绑提示 uid 优先；runtimes.js 引擎详情展示稳定键
- [x] T11 追加提交到 feat/issue-868（PR #895 工作线）+ 回归 + 自审查 + 评论

## 交叉预警处置追加（评论 id=198269670 / QA 复核 id=198272227：派生源含 name 与 i5 四段名改名跟随冲突）

- [x] T12 红测试：tests/atomic/test_issue_868_engine_uid_rename_guard.py（模板/Provider 改名 uid 不变守卫 + legacy 公式零漂移回归 + 锚定回填/改绑对账）
- [x] T13 派生源修正：engine_uid.py 云引擎（身份锚 engine_type/provider_id/template_id 在场）派生剔除 name 段；普通引擎 legacy 公式不变；runtime.py register 透传锚
- [x] T14 i5 合流回填/双读兼容：user/db.rebase_engine_uids_by_anchor（存量按锚重算回填+引用轨道整体改绑）+ rebase_engine_uid_references（注册路径 legacy 命中即改绑，双读兼容）；claim-by-uid v2 spec §2.5/§2.6 与合流迁移清单更新；守卫测试纳入 CI

## 种子分量 id→uid 升格追加（评论 id=198288127 / QA 复核 id=198288768：派生种子 id 分量分布式/库恢复不可靠）

- [x] T15 红测试：tests/atomic/test_issue_868_seed_uid_upgrade.py（表重建/库恢复 id 换值 uid 不变、部分 uid 不切换、id 公式过渡零漂移、映射式回填改绑全轨道、回填后 uid 寻址命中）
- [x] T16 种子切换：engine_uid.py cloud_anchor_from_runtime_info 五元组（provider_uid/template_uid 提取+归一）+ derive_engine_uid uid 公式（成对在场即切换，id/name 不参与）；runtime.py register 透传 uid 分量
- [x] T17 映射式回填：user/db.rebase_engine_uids_by_anchor anchor_rows 接受 uid 分量（旧行 uid 行内读取改绑，禁止 runtime_id 重派 JOIN）；claim-by-uid v2 spec §2.7 升格设计 + spec.md/task.md 更新；守卫测试纳入 CI
