# Tasks: Issue #3892

- [x] T1 注册等待窗口 30s → 60s（`$regSoftDeadlineSec=30` / `$regWindowSec=60` / `$regIntervalSec=3`，20 × 3s 封顶）
- [x] T2 30s 软到期读取 `devops-bot.log` 尾部判定注册在途，有迹象续等、无迹象立即回滚
- [x] T3 在途/硬失败关键字（ASCII）+ 冷启动期回落到进程/daemon 存活判定
- [x] T4 回滚前主控 `last_seen_at` 交叉验证（`/api/runtime/whoami` + 5s 超时，失败静默降级）
- [x] T5 `restore_upgrade_backup` 上报恢复到的旧版本标识，供结尾提示复用
- [x] T6 结尾提示三分支（installed / rolled_back / rollback_failed），回滚路径不再打印 `Install complete!`
- [x] T7 快速失败路径（进程判活 30×1s、daemon 早退、进程缺失立即回滚）保持原样
- [x] T8 新增静态守卫测试 19 条（`tests/runtime/test_issue_3892_install_worker_register_window.py`）
- [x] T9 新增 pwsh 行为测试 9 条（harness 抽取注册等待整段，桩替换时钟/进程/HTTP；无 pwsh 自动 skip）
- [x] T10 回归：install-worker 相关既有测试套件比对基线（3 条既有失败已确认非本次引入）