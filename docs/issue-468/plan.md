# Plan: Issue #468

## 方案
与 `_handle_vod_recommend_complete` 模式一致：把 SRE 完成 deploy_url 登记副作用抽成共享函数，直连 `_execute_agent_inner` 与远程 `complete_task()` 统一调用。

### register_sre_deploy_url(repo_key, result_text) -> str（event_pipeline.py 公开函数）
1. 结构化优先：`json.loads(result_text)`，在 `artifacts`（列表/字符串）与 `issue_summary`（字符串）中匹配 `deploy_url:\s*(https?://\S+)`。
2. 回落：全文完整 URL 正则 `https?://[^\s"\'\)\]，。]+`（修复截断）。
3. 校验：http(s) 前缀、长度 ≤512、strip 空白。
4. `UPDATE managed_repos SET deploy_url=? WHERE repo_key=?`，rowcount>0 时 commit + 日志，返回登记 URL；未登记返回空串。

### complete_task() 钩子第 6 项（service.py L2220 块尾）
```python
if _agent_type == "sre" and _result_text:
    try:
        from src.api.event_pipeline import register_sre_deploy_url
        register_sre_deploy_url(_repo_key, _result_text)
    except Exception as _se:
        log.warning(f"complete_task sre deploy_url register failed: {_se}")
```

## 任务拆解（TDD）
1. `test(#468)`：新增失败测试（结构化命中/正则回落/完整路径不截断/校验拒绝/非 sre 不触发/幂等/complete_task 钩子）。
2. `feat(#468)`：实现 register_sre_deploy_url + 双路径接线，测试转绿。
3. 回归：现有测试全量通过。

## 测试 seam
公开函数 `register_sre_deploy_url` + `complete_task()`（端到端钩子触发）。
