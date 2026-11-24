# Issue #4064 任务拆解

| # | 切片 | 产出 | 测试（seam） | 状态 |
|---|---|---|---|---|
| 1 | 脱敏 helper | `src/domains/runtime/vcs/redact.py` | `tests/runtime/test_log_redaction.py` 纯函数用例（query/header/text + 非 str 占位） | ☐ |
| 2 | 下载分支 4 处日志 | `api_client.py:204/209/211/214` | `caplog` 驱动 HTTPError / timeout / URLError / 通用异常 | ☐ |
| 3 | 请求失败 2 处日志 | `api_client.py:1093/1099` | `caplog` + 打桩 `open_with_policy` 抛 HTTPError（err_body 含 token）/ 通用异常 | ☐ |
| 4 | 去重探测异常分支 | `feedback_issue_converter.py:185` | `caplog` + 打桩 provider `request` 抛含 token 的异常 | ☐ |
| 5 | 门禁与负例 | 测试内的 `assert_redacted` 断言 + 故意不打码负例 | 负例必须 `AssertionError` | ☐ |
| 6 | 配置驱动形态回归 | `tests/runtime/test_api_client_config_driven.py` 扩展 | query_token 与 header 两种鉴权形态的请求 URL/header 不变 + 日志脱敏 | ☐ |

提交顺序（TDD 红/绿）：`docs` → `test`（红）→ `feat`（绿）。

验证命令：`python3 -m pytest tests/runtime/test_log_redaction.py tests/runtime/test_api_client_config_driven.py -q`，
收尾再跑 `tests/runtime` 与 `tests/api` 相关子集 + `make lint`。