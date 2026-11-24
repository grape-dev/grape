# Task: Issue #3739 拆解（TDD 红/绿切片）

> 基线演进说明：Issue #4065 已在 arch-weekly-11-ljt 合入并实现同一语义（kernel 单点判据 +
> 回帖短路 + 卡片兜底），本单在 rebase 后转为「语义锁定 + 回归用例」，代码侧不再重复实现。

| # | 切片 | 类型 | seam | 提交 |
|---|------|------|------|------|
| 1 | 伪号/真实号判定与回帖短路用例先行（红） | test（红） | `create_client` 断言失败桩、TestClient + runtime internal router | `test(#3739)` |
| 2 | 复用基线 #4065 实现（kernel `deliverable_comment_ident` + 回帖短路 + 卡片归一），不新增平行判据 | feat（绿，基线承载） | 同上 | 基线已合入 |
| 3 | rebase 到 arch-weekly-11-ljt 并解冲突：三处源码冲突一律取基线口径，用例按基线 API 重写 | rebase | `git rebase origin/arch-weekly-11-ljt` | 本次提交 |
| 4 | 回归验证：本单用例 + #4065 用例 + D29 卡驱动回帖 + 1545 回帖策略 + 3055 ident + internal 路由契约 | verify | pytest | — |

验证口径：
- `pytest tests/runtime/test_issue_3739_autopilot_pseudo_issue_number.py tests/runtime/test_issue_4065_result_comment_ident.py tests/api/test_issue_4065_issue_card_ident.py tests/runtime/test_d29_comment_delivery_card_driven.py tests/api/test_result_post_policy.py tests/runtime/test_3055_issue_ident_hardening.py tests/api/test_mgmt_no_internal_routes.py tests/contracts/test_internal_contract_p2.py`
- 提交/push 前过 pre-pr-release-check（黑名单拦截 + 敏感增量扫描）。