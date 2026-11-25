# Plan — Issue #3427

## 变更清单
| # | 文件 | 改动 |
|---|------|------|
| 1 | `src/kernel/credential_errors.py`（新增） | 错误码枚举 `TOKEN_MISSING`/`TOKEN_DECRYPT_FAILED`/`TOKEN_REJECTED_UPSTREAM`/`REPO_NOT_BOUND_TO_USER` + 可行动文案 + HTTP 状态映射（400/400/401/403） |
| 2 | `src/kernel/token_resolver.py` | 新增 `CredentialResolution` dataclass 与 `resolve_credential_detailed(repo_key, provider=None, probe=False)`：固定顺序 rc(bot)→global→user 逐层读原始密文并解密；解密异常→`TOKEN_DECRYPT_FAILED`；全空→`TOKEN_MISSING`；probe=True 且解出 token 时调 `provider.verify_token`（GET /user），401→`TOKEN_REJECTED_UPSTREAM`；命中写日志（来源 rc/global/user）。路径 B（host_user_id 非空）跳过 rc/global，与执行链策略语义一致 |
| 3 | `src/domains/mgmt/user/db.py` | 新增 `set_repo_bot_credential_encrypted(repo_key, token)`：加密写 grape_t_repo_credentials(cred_type='bot') + 清 bot_token_status |
| 4 | `src/domains/mgmt/routes/repos.py` | host 无用户 token 时改调 `resolve_credential_detailed(probe=True)`：命中即用（修「同一有效 PAT 误报未配置」）；失败按错误码 400/401 返回并写 `set_host_status(... host_error)`；`_HOST_ERROR_TEXT`/`_host_http_error_code` 增补新码；新增 `PUT /{owner}/{repo_name}/bot-token`（404→校验 repo_token 有效性（verify_token=GET /user）→加密写 rc→清 expired 状态→返回 login） |
| 5 | `src/domains/runtime/issues_center.py` | POST /api/issues github 分支 400 补 error_code（经 resolve_credential_detailed 分类）与可行动文案 |
| 6 | `src/domains/runtime/routes/webhook.py` | a2a jwt/api_key 用户 403 → `error_code=REPO_NOT_BOUND_TO_USER`，文案指向 user_repos 未关联 |

## 兼容性
- HT04 回归（断言旧 `TOKEN_NOT_BOUND`）同步更新为新错误码口径。
- host 在解析器命中前保持原用户 token 优先行为不变；解析器仅作回落与失败分类。

## 任务拆解（task）
1. credential_errors 枚举（纯新增，零回归）
2. resolve_credential_detailed 解析器 + 单元测试
3. host / issues_center / a2a 三入口接入 + host_error 落库
4. PUT bot-token 端点 + 测试
5. 全量相关测试回归
