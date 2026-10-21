# Plan: Issue #386 — 第三方 token 校验失败 401 → 400 语义修正

## 改动点（src/api/routes/settings.py，4 处对称）

| # | 分支 | 触发条件 | 原行为 | 新行为 |
|---|------|---------|--------|--------|
| 1 | GitHub | 上游 401 | `HTTPException(401, "令牌无效或已过期")` | `HTTPException(400, "GitHub token 校验失败，请检查 token 有效性")` |
| 2 | GitHub | 上游其他非 200 | 同上 401 | 同上 400（warning 日志保留） |
| 3 | GitCode | 上游 401 | 同上 401 | `HTTPException(400, "GitCode token 校验失败，请检查 token 有效性")` |
| 4 | GitCode | 上游其他非 200 | 同上 401 | 同上 400（warning 日志保留） |

不变项：502 网络异常分支、409 绑定冲突/转移票据、500 保存失败、
`_is_placeholder` 跳过占位值、空字符串清除路径、前端代码。

## 前端影响核对（已验证，零改动）
- `api.js handleResp`：仅 `r.status === 401` 触发全局踢出；400 走普通错误分支，
  弹窗内展示 `detail.message`，会话保留。
- `account.js _accountTranslateBindError`：`case 400` 已映射 i18n
  `app.account.token_format_invalid`（「令牌格式不正确」）——语义从"第三方 token
  无效"角度可接受；`case 401` 分支保留（OAuth 场景调用方鉴权仍可能出现）。

## 测试方案
新增 `tests/test_issue_386_token_validation_400.py`（requests mock，不发真实网络请求）：
1. GitHub 上游 401 → 本服务 400，message 含 "GitHub"
2. GitHub 上游 500（非 200 非 401）→ 本服务 400
3. GitCode 上游 401 → 本服务 400，message 含 "GitCode"
4. GitCode 上游 403（非 200 非 401）→ 本服务 400
5. GitHub 上游网络异常（requests.get 抛 ConnectionError）→ 502 维持
6. GitCode 上游网络异常 → 502 维持
7. 回归：空字符串清除 gitcode_token 仍走删除绑定（不触发校验）

mock 方式：`unittest.mock.patch("requests.get")`（settings.py 内
`import requests as _req` 局部导入，patch 全局 requests.get 即可命中）。
