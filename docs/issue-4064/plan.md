# Issue #4064 方案设计

## 1. 总体思路

日志是唯一泄露通道，因此在**写日志的那一刻**统一过一次纯函数脱敏，请求 URL / 返回值保持原样：

```
VcsApiClient._do_request  ──log.warning(redact_url(url), redact_text(err_body))
_download_authed (4 处)   ──log.warning(redact_url(url), redact_text(reason))
feedback_issue_converter  ──log.warning(redact_text(e))
                          ▲
                  src/domains/runtime/vcs/redact.py
                  redact_url / redact_headers / redact_text
```

调用方单向依赖 `redact.py`（该模块只 import 标准库），无循环依赖。

## 2. 脱敏 helper（`src/domains/runtime/vcs/redact.py`）

| 函数 | 输入 | 行为 |
|---|---|---|
| `redact_url(url)` | str | query 中敏感参数**值**替换为 `***`，保留参数名/顺序/其余结构；非 str 或解析异常返回占位 `"<url-redacted>"`，不抛异常 |
| `redact_headers(headers)` | Mapping | 复制一份并掩码 `Authorization` / `PRIVATE-TOKEN` / `X-Gitee-Token`（键名大小写不敏感）；非 Mapping 返回 `{}` |
| `redact_text(text)` | 任意 | `key=value`（`=`/`:` 分隔，含引号）、`Bearer|token <凭据>`、已知 token 前缀（`grape_/gcw_/rta_/tk_/ghp_/glpat_`）一律打码；非 str 先 `str()` 兜底，失败返回占位 |

敏感 query 参数（大小写不敏感）：`access_token`、`token`、`secret`、`private_token`、`client_secret`、
`signature`、`code`、`X-Amz-Signature`、`X-Amz-Credential`、`X-Amz-Security-Token`。

**实现取舍**

- 用正则替换 `(?P<lead>[?&])(?P<name>KEY)=(?P<value>[^&#]*)` 而不是
  `urlsplit` + `parse_qsl` + `urlencode` 重排：后者会重写百分号编码与空参数形态，
  违背「保留参数名与其余结构」；正则只吃掉值，其余字节完全不变。
- 参数名匹配加词边界，`status_code=` / `error_code=` 不会被误掩（`_` 是 word 字符，
  `\bcode\b` 在 `status_code` 内部不成立）。
- 掩码为整值 `***`，不做前后缀保留：token 长度/前缀本身就是信息。
- 与既有 `providers/gitee.py::_redact_url`、`providers/card_module.py::_redact_query_token` 的关系：
  本次按 Spec 的 Affected Modules 只改造 6 处日志与探测路径，存量两个窄 helper 保持不动（避免扩大改动面，
  它们只覆盖 `access_token`，可作为后续收敛项）。

## 3. 日志点改造（文案与字段顺序逐字保留）

| 行 | 改后 |
|---|---|
| 204 | `f"{label} download failed: {redact_url(url)} -> HTTP {e.code}"` |
| 209 | `f"{label} download failed: {redact_url(url)} -> timeout ({timeout}s)"` |
| 211 | `f"{label} download failed: {redact_url(url)} -> {redact_text(_reason)}"` |
| 214 | `f"{label} download failed: {redact_url(url)} -> {redact_text(e)}"` |
| 1093 | `f"VcsApiClient: {method} {redact_url(url)} → HTTP {e.code}: {redact_text(err_body)}"` |
| 1099 | `f"VcsApiClient: {method} {redact_url(url)} → {redact_text(e)}"` |

`feedback_issue_converter.py:185` → `f"feedback→issue: title probe failed {provider}:{owner}/{repo}: {redact_text(e)}"`
（URL 本地变量照旧带 token 供请求使用，只有日志出口脱敏）。

**不改**：`return {"error": ..., "status_code": ..., "detail": err_body}` —— 上游原文仍原样返回调用方，
掩码只作用于日志行（#558 口径）。

## 4. 测试策略（seam）

- 纯函数层：直接对 `redact_*` 断言（最高 seam，无 IO）。
- 日志层：`caplog` + 打桩上游异常驱动 6 条日志路径与探测异常分支，断言**日志文本**不含原始 token，
  且出现 `?access_token=***`；成功路径不打日志，故用异常注入。
- 门禁负例：同一个 `assert_redacted(text)` 判定函数，喂一条**故意不打码**的日志文本必须 `AssertionError`，
  证明「断言对未脱敏文本确实红灯」，避免自证式空洞测试。

## 5. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 误掩排障信息 | 中 | 只掩值，保留参数名/结构/status_code/detail |
| 新日志点绕过 helper | 高 | 门禁按「日志文本不含非掩码 token」断言，含负例 |
| 轮换未执行，历史日志仍可读 | 高 | #4067 人工并行处理，不在本单 |
| 异常串隐含 URL | 中 | `redact_text` 兜底 |