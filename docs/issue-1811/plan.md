# Plan: Issue #1811

## 实现步骤

1. `src/runtime/scheduler.py::_execute_autopilot_agent` 第 6 步 env 组装末尾：
   - 函数内 `from src.api.event_pipeline import _inject_bot_agent_env`；
   - 调用 `_inject_bot_agent_env(env_extra, repo_key, agent_type)`，与 event_pipeline
     同源注入 `BOT_USERNAME` / `AGENT_NAME`；
   - 判定 `env_extra.get("BOT_USERNAME")` 为空时 `log.warning`，消息含 exec id 与 repo。
2. 不改分流函数签名：DIRECT 的 `stream_devops_agent` 与 REMOTE 的 `enqueue_task`
   均已接收同一份 `env_extra`。
3. 新增 `tests/test_issue_1811_autopilot_bot_agent_env.py`：
   - 屏蔽引擎/工作区/凭据依赖，monkeypatch 两个分流函数捕获 `env_extra`；
   - parametrize DIRECT / REMOTE，断言两分支均注入非空两变量；
   - `custom_fields` 携带 `BOT_USERNAME` / `AGENT_NAME` 时被保留集拒绝，普通变量保留；
   - 真实 `_resolve_bot_username` 取值：repo 级 bot 优先、缺失回落全局 bot；
   - 空值场景 `caplog` 断言 WARNING 含 exec id + repo。
4. 回归：
   - `python3 -m pytest tests/test_issue_1811_autopilot_bot_agent_env.py -q`
   - `python3 -m pytest tests/test_issue_573_bot_agent_env_injection.py -q`
   - `python3 -m pytest tests/test_issue_486_autopilot_engine_filter.py -q`

## 兼容性

- `setdefault` 语义：调用方已显式设置时不被覆盖。
- `BOT_USERNAME` 为空仍注入空串（沿用 #573 约定，下游 skill 按空值降级），本单额外
  输出 WARNING 提供可观测性。
- 仅新增两个字符串键，不改变既有 REPO / ACCOUNT / GRAPE_WORK_BRANCH 语义。

## 基线说明

服务端注入 `GRAPE_WORK_BRANCH=weekly-mys`，PR base 以该权威值为准；Spec 评论中
标注的 weekly-10 为上游流水线预期基线，本工作区实际基线为 weekly-mys。