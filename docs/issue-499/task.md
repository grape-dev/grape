# 任务拆解：Issue #499

- [x] T1 文档：spec.md / plan.md / task.md
- [x] T2 红测试：`_ensure_wheels` 多平台矩阵选择 + 脚本 UX 静态守卫（tests/test_worker_package.py 扩展 + tests/test_worker_install_ux.py）
- [x] T3 绿实现：worker_package.py `WHEEL_PLATFORMS` / `_pip_download_wheels` / `_ensure_wheels` 重写；测试 fixture 隔离 wheels 下载
- [x] T4 绿实现：install-worker.sh 删引擎提示段 + 输出全英文
- [x] T5 绿实现：install-worker.ps1 删 engineHints + 去 ARM64 门槛 + 输出英文
- [x] T6 包体对比记录：三平台 wheels 实测体积 → package-size.md（4.0MB → 8.1MB 未压缩）
- [x] T7 回归：pytest 相关测试全集 + bash -n 语法检查
- [ ] T8 自审查（5 轴 + pre-pr 增量检查）→ PR → A2A QA
