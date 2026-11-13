# Issue #2028 任务拆解

- [x] 读 Issue/Spec，核对 `arch-weekly-11` 现状（MANIFEST、release_pack、钩子模板、module_path）
- [x] 发布闭包：`src/kernel/worker_package.py` MANIFEST 补 `src/lock.py`
- [x] 发布闭包：`scripts/release_pack.py` `pack_tools()` manifest_files 补 `src/lock.py`
- [x] 钩子模板 fail-open（模块加载 + hook_main 同一 try/except，告警可定位）
- [x] `HOOK_TEMPLATE_VERSION` 指纹 + 存量钩子重装 + 幂等
- [x] 修正钩子 `module_path` 为 canonical 布局（`__file__` 推导）
- [x] 钩子入口依赖自检（`src.lock` / `src.kernel.session_manager`）
- [x] `src/kernel/lock.py` 支持 `holder` 参数（branch_guard 租约获取）
- [x] 新增回归测试 `tests/test_issue_2028_branch_guard_fail_open.py`
- [x] 相关测试回归（worker_package / branch_guard / lock 调用方）
- [x] 实跑 `grape git push` 验证钩子放行 + 租约获取
- [ ] 创建 PR 并通知 QA