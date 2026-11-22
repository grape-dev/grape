# Issue #3563 任务拆解（v2）

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1 | 统一 401 出口：`reauth_required_detail` / `raise_reauth_required` / `sanitize_upstream_error` helper（action + provider，无 error_code） | `src/api/oauth_token.py` | 已完成 |
| 2 | `with_oauth_token` 3 处 401 出口改经统一 helper（补 provider，保留 action） | `src/api/oauth_token.py` | 已完成 |
| 3 | check_repo_name：verify_token 经 with_oauth_token 包装；捕获实际 token；其余异常脱敏 | `src/domains/mgmt/routes/repos.py` | 已完成 |
| 4 | create_from_template：身份获取经 with_oauth_token + verify_token；create_repo/push 用实际生效 token；异常消息脱敏（409/422 检测不变） | `src/domains/mgmt/routes/templates.py` | 已完成 |
| 5 | repo_manager：`get_user_{github,gitcode}_login_status` 变体透出上游状态码（不抛契约不变），成功/失败独立日志行 | `src/domains/mgmt/user/repo_manager.py` | 已完成 |
| 6 | A 侧单测：预刷新成功链（refresh 恰一次 + 新 token + 行更新）+ 失败链（status=4 + 401 reauth_required）+ grep 断言 | `tests/mgmt/test_issue_3563_github_token_refresh.py` | 已完成 |
| 7 | 三入口 401 分流用例（check-name / 模板创建 / 托管权限判断）+ detail 无内部 URL / 无 error_code 断言 + 状态码变体与 helper 契约 | `tests/mgmt/test_issue_3563_reauth_401_split.py` | 已完成 |
| 8 | 前端 B2：api.js 按 detail.provider 分流重授权；GitHub OAuth 未配置 → 提示 + 直达凭据页；错误附 status/detail | `src/api/frontend/shell/js/api.js` | 已完成 |
| 9 | 前端 B3：ReposView / TemplatesView 入口 catch 先判 status/action；i18n 补 `github_reauth_required` / `github_reauth_token_hint`（zh/en）；静态资源版本串 bump | `src/api/frontend/app/src/views/*.vue`、`locales/*/common.json`、`shell/*.html` | 已完成 |
| 10 | 回归：相关既有用例（s127 oauth_401_retry / repos_member_reauth_degrade / member_partial_reauth / issues_api / 395_bugfixes / repos_host_provider / project_templates）与基线逐项对比，零新增失败 | `tests/` | 已完成 |

> 可选项 C（settings.py `token_expired` 真实化，关联 #3543）本次不纳入，见 spec.md 范围说明。
> 注：本分支预存失败（test_project_templates 9 例 / test_repos_host_provider 5 例 /
> test_issues_api·395 若干 / js 套件 10 例等）均为环境造数问题（UUID 化账号 +
> gitignored 配置缺失），已用基线对比法确认本单零新增失败。
