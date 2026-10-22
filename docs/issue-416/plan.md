# Plan: Issue #416 方案设计

## 1. stderr 尾采环形缓冲（executor.py + hermes_acp.py）

新增 `_StderrTailBuffer`（src/agent/executor.py，模块级，hermes_acp 惰性导入避免循环）：
- `append(s)`：追加 chunk；累计字节 > 2×cap 时折叠为末尾 cap 字节（内存上限有界）；
- `text()`：返回末尾 ≤ cap 字节（崩溃栈在末尾，只保尾部）。
- cap 默认 2000，env `HERMES_STDERR_TAIL_BYTES` 可调（下限 200）。

替换点：
- executor `_run_once`：`_stderr_chunks` 全量列表 + `"".join(...)[:500]`（保头）→ 尾采 buffer；
- hermes_acp `_stderr_reader`：同上；done 事件 `stderr[:500]` 上限移除，直接输出尾巴。

## 2. 退出码上报 errors（hermes_acp.py + executor.py）

- hermes_acp 收尾：`exit_code != 0` 且 errors 尚无 `hermes exited` 记录时追加 `hermes exited rc=N`；reader EOF 无产出分支同样先记 rc；
- executor `_run_once` 收尾：`_exit_code != 0 且无文本` 时 errors 追加 `<backend> exited rc=N`（与 E7 修正逻辑一致：`hermes exited rc=N, last stderr: ...` 由 worker 侧拼接 stderr）；
- worker（src/runtime/agent.py `_wrap` 失败上报）：stderr hint 由 `[:200]`（保头）改 `[-2000:]`（保尾）。

## 3. initialize 超时配置 + 重试（hermes_acp.py）

- `HERMES_INIT_TIMEOUT`（默认 100s，解析失败回退 100）；
- `HERMES_INIT_RETRIES`（默认 1，≥0）；
- initialize 失败（超时/错误）→ 按重试次数重发（进程存活时）；进程已退出 → 立即失败并在 errors 写 `initialize failed: ... (hermes exited rc=N)` + stderr 尾巴。

## 4. Windows overlay home 崩溃排查支撑（hermes_acp.py `_build_overlay_home`）

根因假设（依 E7：`.env` 加载后崩溃 / hermes-acp-home）：Windows 无符号链接权限（非 Developer Mode/管理员）时 `os.symlink` 抛 OSError 被 `except: pass` 吞掉 → overlay home 静默缺 skills/plugins/auth → hermes ACP 启动后加载资产失败即死。修复：
- Windows 先试 symlink，失败降级为复制（文件 copy2，单文件 ≤10MB；目录浅层复制一层）；
- 降级/跳过均打 WARNING 日志（worker 日志可定位）；
- 叠加 1-3 的 rc + stderr 尾巴诊断，13 台 worker 升级后即可收敛根因。

## 5. 测试（tests/test_hermes_stderr_tail_and_init_retry.py）

仿 test_hermes_global_timeout.py 的 fake hermes 子进程模式：
1. stderr > 2000B：done.stderr 是末尾 2000B（含尾部标记、不含头部标记）；
2. 启动即死（打印 Starting 后 rc=1 退出、无 stdout）：errors 含 `hermes exited rc=1`、stderr 含崩溃栈尾巴；
3. initialize 首次超时 + 重试成功（HERMES_INIT_TIMEOUT/HERMES_INIT_RETRIES env）；
4. initialize 重试仍失败 → failed 且 errors 含 timeout 与 rc；
5. CRLF stderr 正确入尾采；
6. `_StderrTailBuffer` 单元：边界折叠、cap 下限；
7. 配置助手单元：HERMES_STDERR_TAIL_BYTES / HERMES_INIT_TIMEOUT / HERMES_INIT_RETRIES 默认与覆盖、非法值回退。
