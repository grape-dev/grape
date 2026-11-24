# Issue #4064 需求文档：VcsApiClient 日志凭据脱敏

> 来源：Issue #4064 正文 + Issue Agent 发布的 Spec 评论（developability=ready）。

## 1. 问题

`VcsApiClient` 在 `api_auth=query_token`（GitCode/Gitee v5 实测必需，header 会 401）下把仓库
bot PAT 拼进 URL query，随后直接把该 URL 写进日志：

| 位置（当前基线） | 形态 | 影响 |
|---|---|---|
| `src/domains/runtime/vcs/api_client.py:1093` | `VcsApiClient: {method} {url} → HTTP {code}: {err_body}` | 主泄露点：`?access_token=<PAT>` 明文 + 上游 err_body |
| `src/domains/runtime/vcs/api_client.py:1099` | `VcsApiClient: {method} {url} → {e}` | 主泄露点，异常串可能内含 URL |
| `src/domains/runtime/vcs/api_client.py:204/209/211/214` | `{label} download failed: {url} -> ...` | 预签名 query（`X-Amz-Signature` / `X-Amz-Credential`）明文 |
| `src/api/feedback_issue_converter.py:181/185` | 去重探测拼 `access_token=`，异常分支打印 `{e}` | 同族泄露（异常 str 带 URL） |

线上证据：Loki 1h 14 条含 `access_token`，跨 3 仓复用同一 token。

## 2. 目标

在 VCS 客户端全部日志路径建立**统一脱敏入口**，使 token 与预签名参数不以明文落日志；不改鉴权传输形态、
不改 `error` / `status_code` / `detail` 契约。

## 3. 范围

**In**

- 新增 `src/domains/runtime/vcs/redact.py`：`redact_url` / `redact_headers` / `redact_text` 三个纯函数。
- `api_client.py` 6 处 URL 日志改走 `redact_url`，`err_body` 与异常串过 `redact_text`；文案与字段顺序不变。
- `feedback_issue_converter.py` 去重探测异常分支走同一 helper。
- 新增 `tests/runtime/test_log_redaction.py`（含负例），扩展 `tests/runtime/test_api_client_config_driven.py`。

**Out**

- 不迁移鉴权形态（GitCode v5 固定 `query_token`，header 401）。
- 不做 token 吊销/轮换、不清理历史 Loki 行（另立 #4067）。
- 不改错误契约：`detail` 保留上游原文（#558 口径）。

## 4. 验收

- [ ] 6 处日志 + 探测异常分支输出 `?access_token=***`，无原始值。
- [ ] helper 覆盖 query / header / text，用例含 `X-Amz-Signature`、`X-Amz-Credential`、`Bearer`。
- [ ] 含 token 的 URL 传入后参数名保留、值掩码。
- [ ] `error` / `status_code` / `detail` 语义不变，`detail` 仍为上游原文。
- [ ] 新增测试通过、现有测试全绿；负例能被门禁断言拦下。
- [ ] 上线后 Loki 同口径 1h 内 0 条未掩码行（构建/运维环节验证）。

## 5. 约束

- 按参数名屏蔽（而非按已知 token 值匹配）：轮换后自动生效。
- 掩码为整值 `***`，不留前后缀。
- 非 str / 解析失败返回占位，**不得抛异常**（日志路径不能因脱敏失败而中断请求处理）。