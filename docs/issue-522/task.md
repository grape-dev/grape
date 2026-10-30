# Task: Issue #522

- [x] T1: `_current_username()` 辅助函数（USER → pwd.getpwuid → UID 降级）
- [x] T2: `_kill_stale_engine` Linux 分支 pgrep 参数改造，移除提前求值表达式
- [x] T3: 无 TTY（setsid）取消回归测试，USER 已设置/未设置两场景
- [x] T4: 现有测试回归（tests/test_runtime_agent.py 等）
