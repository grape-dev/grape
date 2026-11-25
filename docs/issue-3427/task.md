# Task — Issue #3427

- [x] T1 错误码枚举 `src/kernel/credential_errors.py`
- [x] T2 单点解析器 `resolve_credential_detailed`（token_resolver.py）+ 解析顺序/错误码测试
- [x] T3 host 接入解析器 + host_error 落库（repos.py）
- [x] T4 POST /api/issues github 分支错误码（issues_center.py）
- [x] T5 a2a 403 解耦（webhook.py）
- [x] T6 PUT /api/repos/{owner}/{repo}/bot-token 端点（repos.py）
- [x] T7 测试回归（tests/mgmt/test_issue_3427_credential_read_errors.py 17 用例通过；HT04/T2-15 更新到新口径；基线失败的用例与改动前一致，无新增回归）
- [x] T8 自审查 + 敏感扫描 + PR
