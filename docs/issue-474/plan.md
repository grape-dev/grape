# Plan: Issue #474 — 凭据接口去掉明文 AK/SK，前端改掩码占位

## 后端（src/api/routes/settings.py）

`get_credentials` 返回体 `huawei_cloud` 块删除 2 行：
- 删除 `"ak": hw_cred.get("ak", "")`
- 删除 `"sk": hw_cred.get("sk", "")`

保留 `ak_exists`/`sk_exists`。同时更新
`@router.get("/credentials", operation_id="settings_credentials_get", ...)`
的 `description`，明确「凭据明文永不回显，仅回存在性」。

设计决策：删除键而非置空——置空会与 `*_exists=false` 语义混淆；同 PR 内前端同步适配，
无跨端兼容窗口。`PUT /credentials` 写路径与 `_is_placeholder` 兜底不动。

## 前端 1（src/api/static/js/pages/account.js 凭据 tab）

`_accountLoadCredentials`：
- `var akVal = hw.ak_exists ? '********' : '';`
- `var skVal = hw.sk_exists ? '********' : '';`
- 记录原始值 `window._accountCloudCredOriginal = { ak: akVal, sk: skVal }`（占位符哨兵，
  不再含真实值）
- 删除 AK/SK 两行的 👁 切换按钮（保持输入框 type=password）
- 更新注释（不再声称「后端返回真实 ak/sk」）

`_accountSaveCloudCreds`（逻辑不变，更新注释）：
- `if (akVal !== '********' && akVal !== orig.ak) body.huawei_cloud_ak = akVal;`
- 哨兵语义自洽：未改动（`********`）= 不回传；清空（`''` 且 orig 为 `********`）= 显式清除；
  空框未配置（`''` 且 orig 为 `''`）= 不回传；输入新值 = 回传

删除已死的 `_accountToggleSecret`（👁 切换函数，移除按钮后无调用）。

## 前端 2（src/api/static/js/pages/scene-market.js 华为云场景 apply）

现状：`API.get('/settings/credentials')` 后用 `hw.ak || ''` / `hw.sk || ''` 自动填入
`HUAWEICLOUD_SDK_AK/SK` 行（只读 key），这是仓库内第二个明文消费点（Spec 原判定
「唯一消费方」不准确，实现时发现并纳入）。

变更：
- 不再读取 `hw.ak`/`hw.sk`（接口已不返回）；统一走空值分行（与 catch 兜底一致）
- 提示文案去掉「如已配置将自动填入」，改为「已配置的凭据可在个人设置中管理；
  如需在本场景使用请直接填写」——避免承诺明文自动回填

## 测试（tests/test_issue474_credentials_no_plaintext.py）

沿用 test_api_routes.py::TestSettings 路由测试模式（JWT 头 + `user_db.set_credential`
造数据 + TestClient）：
1. 有值：写入 huawei_cloud ak/sk → GET → `huawei_cloud` 块无 `ak`/`sk` 键，
   `ak_exists`/`sk_exists` 为 True
2. 无值：GET → `huawei_cloud` 块无 `ak`/`sk` 键，`ak_exists`/`sk_exists` 为 False
3. 清除：PUT 空串清除后 GET → 存在性 False 且仍无明文键
4. codearts 不回退：写入 codearts → 块内仅存在性键、有值可正确反映、无明文

## 回归核对
- `tests/test_api_routes.py::TestSettings`（credentials 相关 19 例）全部通过
- `tests/test_openapi.py`（operation_id 契约）不因 description 变更失败
- 新增单测 + 既有套件一起跑
---

# 追加：方案A —— 按需取回明文（reveal 接口）实施计划

## 后端（src/api/routes/settings.py + src/auth/audit.py + src/api/db_setup.py）

1. `src/auth/audit.py`：EVENT_TYPES 增加 `credential_reveal`。
2. `src/api/db_setup.py`：新增表 `credential_reveal_log`
   （id/user_id/provider/field/ip/created_at，DDL 默认 UTC `datetime('now')`）+ 用户时间索引，
   用于高频告警窗口计数。
3. `src/api/routes/settings.py`：
   - 常量 `_REVEAL_WINDOW_SECONDS=600`、`_REVEAL_MAX_PER_WINDOW=10`（测试 import 用）
   - `POST /credentials/reveal`（settings_credentials_reveal）：
     校验 provider ∈ {huawei_cloud, codearts}、field ∈ {ak, sk}；
     二次确认（verify_password）；高频窗口计数 ≥ 阈值 → 429 + 告警日志；
     `user_db.get_credential` 取原值；`log_audit("credential_reveal", detail={provider, field})`；
     写 credential_reveal_log；响应 Cache-Control: no-store + Pragma: no-cache。
   - 高频计数用 UTC 基准（与 DDL 默认 `datetime('now')` 一致，避免时区误判）。

## 前端（src/api/static/js/pages/account.js + locales）

- `_accountLoadCredentials`：已配置字段（*_exists=true）渲染 👁 按钮（revealBtn）；
  初始化 `window._accountCloudCredRevealed = {ak:null, sk:null}`。
- `_accountToggleReveal(field)`：已显示 → 恢复掩码；未显示 → `_accountAskPasswordForReveal`。
- `_accountAskPasswordForReveal(field)`：弹二次确认模态框（复用 _accountShowModal），
  输入当前 Grape 密码。
- `_accountDoReveal(field)`：`API.post('/settings/credentials/reveal', {provider:'huawei_cloud',
  field, password})` → 成功把明文写入输入框（type=text）并记录 revealed 值；
  失败 toast e.message（后端中文提示直接透出）。
- `_accountSaveCloudCreds`：比对新增 revealed 值跳过（取回展示的明文视为未改动，不回传 PUT）。
- locales zh/en app.json：新增 reveal_plaintext/reveal_title/reveal_desc/reveal_confirm/
  reveal_wrong_password/reveal_too_frequent/reveal_failed/reveal_need_password/reveal_not_configured。

## 测试（tests/test_issue474_credentials_reveal.py）

1. 错误/缺失密码 → 400（二次确认）
2. 正确密码 → 200 当次返回明文；ak/sk 均可；响应 Cache-Control: no-store；
   列表接口 GET /credentials 仍无明文（回归）
3. 未知 provider/field → 400/404
4. 未配置凭据 → 400/404
5. 审计：audit_logs 新增 credential_reveal，detail 含 provider/field，不含明文值
6. 高频：预填窗口内 _REVEAL_MAX_PER_WINDOW 条 → 下一次 → 429
7. 未设置密码用户 → 400 + need_set_password

## 回归

- `tests/test_issue474_credentials_no_plaintext.py`（既有 4 例）
- `tests/test_api_routes.py -k Settings`（credentials 相关）
- `tests/test_issue_372_r14_audit.py` 等审计用例
