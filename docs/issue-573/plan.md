# Plan: Issue #573

## 实现步骤

1. `src/api/event_pipeline.py` 在 `get_agent_display_name` 附近新增：
   - `_resolve_bot_username(repo_key) -> str`：三级兜底（managed_repos.bot_username → 全局 bot（github/gitcode 分流）→ host_user 平台用户名），整体异常返回空串。
   - `_inject_bot_agent_env(env_extra, repo_key, agent_type)`：`env_extra["BOT_USERNAME"] = _resolve_bot_username(...)`、`env_extra["AGENT_NAME"] = get_agent_display_name(agent_type)`，无 agent_type 门控。
2. 替换三处 coding 门控重复块为 helper 调用：
   - 正常入队（原 `# coding agent 注入 BOT_USERNAME` 块）
   - direct 执行路径（同注释块）
   - `build_task_env()`（重派路径）
3. `src/scene_custom_env.py` `_RESERVED_ENV_NAMES` 增加 `"AGENT_NAME"`。
4. 新增 `tests/test_issue_573_bot_agent_env_injection.py`（monkeypatch DB/全局函数，纯函数级测试）。
5. 回归：`python3 -m pytest tests/test_issue_573_bot_agent_env_injection.py tests/test_scene_env.py tests/test_issue_528_pr_sub_env_injection.py -q`。

## 风险

- BOT_USERNAME 注入范围从 coding 扩到全部 agent：变量原已列为场景保留名，其它 agent skill 仅读取不写入，风险低。
- AGENT_NAME 与 direct 路径 agent_name 参数同值双写（env_extra 先 set，stream_agent 后 set 同值），幂等无冲突。
