# Issue #3620 — 实现方案（plan）

## 决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 身份判定 | 硬编码 auth_type / 能力位 | 能力位 `repo_session` | 消除矩阵与端点脱节（ADR-2），新增 token 类型零改动 |
| 仓级边界 | 严格相等 / 可见仓范围 | 严格相等（fail-closed） | 最小权限；tko_ 只服务其接管任务仓库 |
| 会话状态 | 仅 token 有效期 / DB 二次校验 | DB 二次校验 active | finish/abort 立即吊销，避免残留 token 继续换券 |
| 与 #3619 关系 | 各自修复 / 合并参考 | 合并参考 | 同缺陷；只在 #3620 落一处实现 |

## 改动点

1. `src/domains/mgmt/routes/repos.py`
   - `_repo_platform_token`：`auth_type != "session_token"` → `has_capability(user, REPO_SESSION)`；
     `auth_type == "takeover"` 时调用 `_ensure_takeover_repo_scope(user, rk)`。
   - 新增 `_ensure_takeover_repo_scope`：repo_key 存在且严格相等、接管会话 active，
     否则 403（会话状态查询异常 503）。
   - 新增 `_takeover_session_status`：走 `src.domains.runtime.public.takeover_session_status_any`
     （runtime 域 owner 原语，任意拓扑；与 `git_repo_api._tk_session_status` 同一事实源）。
   - git-credential 端点 description 更新为 repo-session（tk_/tko_）。
2. `src/kernel/auth_sdk/auth_token.py`
   - 方式 C 注释补充：`tko_`/`tk_` verify kind 互过滤 ≠ 能力位隔离；repo 域端点统一按
     `repo_session` 判定，禁止硬编码 `auth_type == 'session_token'`。
3. `tests/mgmt/test_issue_3620_takeover_git_credential.py`
   - tko_ 正向 / 跨仓 / 未绑定 / 会话已结束 / tk_ 正向 / api_key·jwt·internal 拒绝 /
     provider 前缀不一致拒绝。

## 兼容性与回滚

- 仅放宽到已有 `repo_session=True` 的 takeover；其余类型行为不变（错误文案保持）。
- 回滚 = 还原 2 个源文件改动，删除新增测试；无 schema / 契约字段变更。

## 验证

- `pytest tests/mgmt/test_issue_3620_takeover_git_credential.py tests/test_auth_caps.py -q`
- `pytest tests/mgmt/test_repo_token_getters_provider_keyed.py -q`
- `pytest tests/api/test_openapi.py -q`（x-token-scope 不漂移）