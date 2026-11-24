# Spec: Issue #3737 — VcsApiClient 日志脱敏（消除 access_token 明文泄漏）

> 来源：Issue #3737（P1 凭据泄露级安全）。基线分支 `arch-weekly-11-ljt`。

## 目标

为 VCS 客户端建立「记录前脱敏」的单一日志出口，使 GitCode/Gitee bot `access_token`（以及
`private_token` / `token` / `Authorization` 头值）不再以明文进入日志；同时保留排障所需的
method + path + 非敏感 query + 状态码 + 上游错误摘要。

## 现状（基线分支实测）

| 位置 | 现状 | 问题 |
|---|---|---|
| `api_client.py` L902 | `url = f"{url}{sep}access_token={self.token}"` | query_token 鉴权把 token 拼进 URL（保留，属鉴权实现） |
| L204 / L209 / L211 / L214 | `log.warning(f"{label} download failed: {url} -> ...")` | `_download_authed` 4 条失败日志直接输出完整 URL（含 token） |
| L982 | `log.warning(f"VcsApiClient: {method} {url} → HTTP {e.code}: {err_body}")` | `_do_request` HTTPError 分支输出完整 URL + 上游 body 全文 |
| L988 | `log.warning(f"VcsApiClient: {method} {url} → {e}")` | 通用异常分支输出完整 URL，异常文本可能回显 URL |

同类问题 #860 已修 webhook 路径，本单为 VCS 客户端通道的遗漏面。

## 期望行为

1. 模块内新增单一日志出口：
   - `_redact_url(url)`：保留 scheme/host/path 与非敏感 query（`per_page`/`page`/`state` 等原样），
     敏感键（`access_token`/`private_token`/`token`/`access_key`/`secret`/`client_secret`/`refresh_token`）
     的值替换为 `***`；保留原始编码形态，不重排非敏感参数。
   - `_redact_text(s)`：兜底正则，把 `access_token=…` / `"token":"…"` / `Authorization: Bearer …`
     等形态的值替换为 `***`，用于异常文本与上游错误体。
2. 6 处失败日志全部经脱敏出口：`_download_authed` 4 条 + `_do_request` 2 条。
3. 上游 `err_body` 先脱敏再截断到 ≤500 字符写日志；返回值中的 `detail` 保持全文（#558 契约不变）。
4. 不改变 `_request` / `_do_request` 返回结构与状态码语义。

## 验收标准

- [x] 400/401 后日志无 token 全文，也无 token 任意 ≥8 位连续片段
- [x] `_download_authed` 四条失败日志（HTTP 错误/timeout/URLError/通用异常）均脱敏
- [x] 日志路径 `grep 'access_token='` 零命中（仅 L902 URL 构造处允许拼接）
- [x] 上游 400 body 回显 `access_token=` 时日志不泄漏，且截断 ≤500 字符
- [x] `per_page` 等非敏感 query 在日志中保留，method + path + 状态码可读
- [x] 现有测试全绿；`_request` 返回语义不变

## 范围外

- token 轮换 / bot 凭据表回填 / Loki 存量数据处置（管理员人工执行）。
- webhook 路径日志改动（#860 已完成）；其他模块潜在同类泄漏面另开单。