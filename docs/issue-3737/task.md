# Task: Issue #3737 任务拆解

- [x] T1（红）新增 `tests/runtime/test_vcs_api_client_log_redaction.py`：400/401/超时/URLError/
      通用异常 + `_redact_url`/`_redact_text` 纯函数断言，先于实现提交（对当前代码变红）。
- [x] T2（绿）`api_client.py` 新增 `_redact_url` / `_redact_text` 单一日志出口。
- [x] T3（绿）改造 `_download_authed` L204/L209/L211/L214 四条失败日志。
- [x] T4（绿）改造 `_do_request` L982/L988 两条失败日志（err_body 先脱敏再截断 500）。
- [x] T5 自审查：5 轴审查 + 开源前置增量检查（无关内容拦截、敏感信息增量扫描）。
- [x] T6 建 PR（base=`arch-weekly-11-ljt`）+ Issue 评论 + A2A 通知 QA。
- [ ] T7（范围外，人工）token 轮换 / 凭据表回填 / Loki 存量处置。