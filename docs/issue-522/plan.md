# Plan: Issue #522

## 方案

1. `src/runtime/agent.py` 新增模块级 `_current_username()`：
   - `os.environ.get("USER")` 非空 → 返回；
   - 否则 `try: import pwd; pwd.getpwuid(os.getuid()).pw_name`；
   - 再失败降级 `str(os.getuid())`（无 `getuid`（Windows）时返回 None）。
2. `_kill_stale_engine` Linux 分支 pgrep 参数构造：
   - `user = _current_username()`；`user` 非空时 `["pgrep", "-u", user, "-f", pattern]`，
     否则省略 `-u`（pgrep 按当前有效 UID 过滤），保留终止能力。
   - 彻底移除 `os.getlogin()` 默认参数表达式。
3. 测试 `tests/test_kill_stale_engine_no_tty.py`：
   - 单元测试 `_current_username`（USER 设置/未设置/降级）。
   - 回归测试：`setsid`（`start_new_session=True`）启动无 TTY 子进程，在子进程内调用
     `_kill_stale_engine`，验证无 ENXIO、pgrep 执行、victim（带 `opencode run` 参数、
     独立会话、cwd 匹配）被 SIGTERM 终止；USER 已设置/未设置两场景。

## 不做的事

- 不改 Windows 分支、不改 pgid/cwd 匹配语义（保持现有防误杀防线）。
