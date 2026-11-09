# Tasks — Issue #558

- [ ] T1 repo_manager.py：VcsUpstreamError + _req/_req_query/_github_req 抛结构化异常、去截断
- [ ] T2 git_repo_api.py：_vcs_req 透传上游状态码；merge_pull 增加 PRMergeBody(merge_method/title)
- [ ] T3 api_client.py：merge_pr 透传 merge_method/title；_do_request 错误补 status_code/全文
- [ ] T4 docs/openapi/10-git-repos.md merge 端点参数文档
- [ ] T5 测试：405 透传 / 参数透传 / 2xx 回归 / 超时 502；全量回归
- [ ] T6 自审查 + 建 PR（base weekly-08-mys）+ Issue 评论 + A2A QA + tasks complete
