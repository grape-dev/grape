# Plan: Issue #3737 — 日志脱敏实现方案

## 方案概述

在 `src/domains/runtime/vcs/api_client.py` 模块级建立脱敏工具函数，所有失败日志改为
「记录前脱敏」（`_redact_url` / `_redact_text`），而不是事后过滤日志行。

## 实现路径

1. 模块顶部（常量区）新增：
   - `_SENSITIVE_QUERY_KEYS`（frozenset）：`access_token`、`private_token`、`token`、`access_key`、
     `secret`、`client_secret`、`refresh_token`、`api_key`。
   - `_LOG_BODY_MAX = 500`。
   - `_redact_url(url)`：按 `?`/`#` 手工切分 query，逐对 `k=v` 判断敏感键（含 URL 解码后比较），
     敏感键只替换值，非敏感键原样保留（不重编码，避免排障信息失真）。
   - `_redact_text(text)`：两条正则兜底——`Authorization` 头值整段掩码；敏感键 `k=v` / `k: v` /
     `"k":"v"` 形态只掩码值。
2. `_download_authed` 4 条 `log.warning`：URL 经 `_redact_url`，异常/`reason` 文本经 `_redact_text`。
3. `_do_request` 2 条 `log.warning`：`method` + `_redact_url(url)` + `e.code` + `_redact_text(err_body)[:500]`；
   通用异常分支 `_redact_text(e)`。`detail` 返回值保持全文。
4. 单测 `tests/runtime/test_vcs_api_client_log_redaction.py`：monkeypatch
   `urllib.request.urlopen` 抛 HTTPError 400/401、timeout URLError、URLError、通用异常；
   `caplog`（logger=`devops-bot`）断言无 token 全文/无 ≥8 位片段、非敏感 query 保留、
   返回结构不变。

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 日志粒度 | 全量 URL / 仅 path / 脱敏 URL | 脱敏 URL | 仅 path 丢失分页排障信息；全量 URL 即事故形态 |
| 脱敏落点 | 各调用点自处理 / 统一 helper | 统一 helper | 单一出口，防新增日志点回归 |
| query 重建 | `urlencode` 重建 / 手工按对替换 | 手工按对替换 | `urlencode` 会重编码非敏感值，弱化排障可读性 |
| 上游 error body | 原样 / 截断+脱敏 | 脱敏 + 截断 500 | body 可能回显请求参数，且原样有日志膨胀风险 |
| 返回语义 | 顺手脱敏返回值 / 保持 | 保持 | #558 已固化 `status_code` + `detail` 全文语义 |
| 工具函数位置 | 新公共模块 / `api_client` 模块级 | `api_client` 模块级 | 当前仅本文件使用，避免过度抽象 |

## 测试 seam

- 公开行为 seam：`_do_request`（静态入口，所有平台请求唯一出口）与 `_download_authed`
  （带 token 下载唯一实现）；单测不 mock 脱敏函数本身，只 mock 网络层 `urlopen`，
  断言「日志行」这一用户可见产物。
- 纯函数 seam：`_redact_url` / `_redact_text` 直接断言，覆盖编码/JSON/header 形态。

## 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 日志文本变化导致既有断言失败 | Med | 现有测试未断言 URL 全文（已 grep 核对），回归全量跑 |
| 正则过度替换丢排障信息 | Low | 只替换敏感键的值，保留键名与非敏感参数 |
| 上游 400 body 回显凭据 | Med | `_redact_text` 兜底 + 截断 |
| 只修代码不轮换 token | High | 范围外交管理员人工处置（见 spec.md） |