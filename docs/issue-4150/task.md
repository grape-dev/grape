# Issue #4150 任务拆解（Task）

按 TDD 红/绿分提交执行。

| # | 切片 | 红（测试先行） | 绿（实现） |
|---|------|---------------|-----------|
| 1 | 局部 import json 遮蔽清零 | `test_execute_agent_inner_has_no_local_json_import` / `test_src_has_no_json_shadow_pattern`（命中 L978） | 删除 `direct_executor.py` 内局部 `import json` |
| 2 | 断言 A：关联仓注入恢复 | `test_env_carries_linked_repos_json` / `test_linked_repo_tokens_injected_with_env` / `test_no_linked_repos_keeps_env_clean` | 同切片 1（L408 `json.dumps` 解析回模块级） |
| 3 | 断言 B：失败 errors 落库恢复 | `test_errors_passed_to_update_task_status` / `test_errors_merge_keeps_existing_records` | 同切片 1（L927 `json.dumps` 解析回模块级） |
| 4 | 文档 | — | `docs/issue-4150/{spec,plan,task}.md` |

提交序（git log 可证明「先红后绿」）：

1. `test(#4150): 先写失败用例——局部 import json 遮蔽致关联仓注入与失败 errors 落库失效`（6 failed / 2 passed）
2. `fix(#4150): 删除 _execute_agent_inner 局部 import json，恢复关联仓注入与失败 errors 落库`（8 passed）

## 验证命令

```bash
python3 -m pytest tests/runtime/test_issue_4150_local_json_shadow.py -q   # 8 passed
python3 -m pytest tests/runtime -q                                        # 与基线失败集一致
make lint                                                                 # OK
```

## 待外部验证（不在本 PR 范围）

- 部署后生产日志 `{service_name="grape-worker"} |= "errors merge for seq="` 计数为 0（sre/发布窗口）。