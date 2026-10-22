# Spec: Issue #416 — hermes 启动即死 + initialize 100s 超时 + stderr 截断

> 完整 Spec 见 Issue #416 评论（Issue Agent 发布，Developability: ready）。本文件为开发侧摘要。

## 目标
1. stderr 采集改保留尾部 ≥2000 字节（崩溃栈在末尾）；
2. 任务 errors 字段写明 `hermes exited rc=N`（退出码），不再为空；
3. initialize 超时阈值可配置（HERMES_INIT_TIMEOUT，默认 100s）+ 按配置重试（HERMES_INIT_RETRIES，默认 1）；
4. Windows worker hermes-acp-home 崩溃根因排查：overlay home 在 Windows 无符号链接权限时静默丢 skills/plugins/auth（os.symlink 失败被吞），补 copy 降级 + 完整诊断信息（rc + stderr 尾巴）随 13 台 worker 升级合入后可定位。

## 范围
- src/agent/executor.py：stderr 尾采环形缓冲（_StderrTailBuffer）+ 非零退出且无文本时 errors 写 `hermes exited rc=N`；
- src/agent/hermes_acp.py：stderr 尾采、退出码上报、initialize 超时配置化 + 重试、overlay home Windows copy 降级；
- src/runtime/agent.py：worker 失败上报 stderr hint 由前 200B 改为末尾 2000B（向后兼容）。

## 验收
- stderr 保留末尾 ≥2000B 且含崩溃栈；
- errors 含 `hermes exited rc=N`；
- initialize 超时可配置且按配置重试；
- 现有测试通过 + 新增测试覆盖（tests/test_hermes_stderr_tail_and_init_retry.py）。
