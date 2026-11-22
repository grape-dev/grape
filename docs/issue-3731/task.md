# Issue #3731 任务拆解

> 目标合入分支：`arch-weekly-11-ljt` | 开发分支：`feat/issue-3731`

| # | 切片 | 测试 seam | 红（失败测试） | 绿（最小实现） | 状态 |
|---|------|-----------|----------------|----------------|------|
| 1 | 路由解包收敛到三元组 | `_repo_target()` / `_repo_bot_token()` 返回值 | `test_repo_target_three_tuple_unpack`（当前 2 元解包 → ValueError） | `_repo_target` / `_repo_bot_token` 改 3 元解包 | TODO |
| 2 | 标题截断 + 查重同键 | `_create_provider_issue()` payload / `_find_existing_open_issue()` 查重键 | `test_long_title_clipped`、`test_clipped_title_reuse_existing` | `_MAX_TITLE_LEN` + `_clip_title()` 接入两处 | TODO |
| 3 | 失败轮次持久化 + ERROR 升级 | `convert_pending_feedback()` 返回值 + `user_feedback.convert_fail_count` | `test_consecutive_failure_escalates_error`、`test_success_resets_fail_count` | `convert_fail_count` 列 + `feedback_bump_convert_fail` + `_note_failure` | TODO |
| 4 | 历史用例对齐 #673 语义 | `tests/runtime/test_feedback_issue_converter.py::test_only_agent_pending_selected` | 基线已红（user+repo_key 现应转换） | 用例改为「带 repo_key 的 user 反馈转换、纯意见类不转换」 | TODO |
| 5 | 文档 + PR | — | — | `docs/issue-3731/{spec,plan,task}.md`、自审查、建 PR、A2A QA | TODO |

## 验证命令

```bash
python3 -m pytest tests/runtime/test_feedback_issue_converter.py -q
python3 -m pytest tests/runtime/test_issue_official_issue_switch.py tests/mgmt/test_s87_true_dichotomy_closure.py -q
python3 -c "import src.api.main"     # Makefile lint 门
python3 scripts/check_undefined_names.py
python3 scripts/check_async_blocking.py
```