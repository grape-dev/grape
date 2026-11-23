# Plan: Issue #3892

## 改动面（单脚本 `scripts/install-worker.ps1`）

### 1. 注册等待窗口：30s → 60s + 30s 软到期评估

- 引入三个常量：`$regSoftDeadlineSec = 30`（= 原固定窗口）、`$regWindowSec = 60`（20 × 3s 硬上限）、
  `$regIntervalSec = 3`（沿用原采样粒度）；等待循环改为 `while ($regElapsed -lt $regWindowSec)`。
- 每轮仍 `Get-Content $workerLog -Tail 40 -ErrorAction SilentlyContinue` 匹配 `RuntimeAgent registered`
  （沿用既有日志读取模式，不引入新依赖）。
- `$regElapsed -ge 30` 且未判定过时做一次「注册在途」评估（`Test-RegistrationInFlight`）：
  有迹象 → 续等至 60s；无迹象 → `break` 立即进入回滚判定，不空等剩余 30s。

### 2. 在途 / 硬失败判定

- `$regInFlightPattern`（ASCII 关键字，避免 PS 5.1 无 BOM UTF-8 按 ANSI 解码导致中文匹配不中）：
  `registering with` / `register(ing)? (runtime|engine)` / `/register` / `heartbeat` / `contract guard` /
  `codearts tasks dir` / `Killed existing worker` / `engine discovery` / `engine probe`。
- `$regFailedPattern`：`Register failed` / `RUNTIME_TOKEN rejected` / `register 401` / `credential revoked` /
  `Traceback` / `SystemExit` / `worker exiting` —— 命中级等待循环立即停等。
- 优先级：硬失败 → 不在途；在途迹象 → 在途；无判定线索（冷盘首扫 30s 内日志可能一行都没落盘）→
  回落到「新进程或本安装目录 daemon 仍存活」（`Test-NewAgentProcessAlive` / `Test-WorkerDaemonAlive`）；
  进程与 daemon 都没有 → 不在途。
  > 关键：只靠日志字面迹象会在「导入阶段 34s、日志还空」时把慢启动判死，本需求就复现不了修复；
  > 故冷启动期以进程存活为准。

### 3. 回滚前主控交叉验证（可选加固，静默降级）

- `Test-MasterRuntimeRecentlySeen`：`GET $Server/api/runtime/whoami`（`rta_` 凭据下的既有只读端点，
  5s 超时）→ 响应含 `last_seen_at` 且落在「本轮启动窗口」内（`$agentStartUtc` − 10s 容差）则视为已连上主控。
- 端点不存在 / 403 / 超时 / 无该字段 / 无法解析 → 逐条 catch 后 `return $false` 并打一行降级日志，
  绝不向主流程抛错；验收不依赖该项。当前基线主控的 whoami 不返回 `last_seen_at`，因此实际走日志判定；
  主控补字段后本函数自动生效。
- 只在窗口到期且 `-not $regOk` 时调用一次。

### 4. 结尾提示分支化

- `$installOutcome` = `installed` / `rolled_back` / `rollback_failed`，由注册结果 + 回滚结果赋值。
- `restore_upgrade_backup` 把恢复到的版本写入 `$script:rolledBackVersion`（并记录备份目录），供结尾复用。
- 结束段改为三分支：`installed` 保留原 `Install complete!`；`rolled_back` 打印
  `Install ROLLED BACK - the new version did NOT take effect!` + `Rolled back to pre-upgrade version: <ver>
  (backup: <.upgrade-backup>)` + 建议重试；`rollback_failed` 打印 `Install FAILED ... worker is OFFLINE!`
  + 手工拉起 daemon 的下一步。

### 5. 快速失败路径不变

- 进程判活循环（`for ($i = 1; $i -le 30; $i++)` + 1s 采样 + 第 3 秒 daemon 早退日志）与
  `New agent failed to start (process missing)` 的立即回滚**完全未改**；硬失败 + 进程/daemon 双消失
  也会在 30s 处提前停等，窗口放宽不会无限等待。

## 测试

- `tests/runtime/test_issue_3892_install_worker_register_window.py`：
  - 静态守卫（19 条，Linux CI 恒可跑）：窗口常量、旧 10×3s 循环已移除、日志读取模式、在途/硬失败关键字、
    ASCII 约束、软到期分支、进程存活回落、快速失败路径未变、`Install complete!` 唯一且只在成功分支、
    回滚提示含版本/备份/重试、回滚函数上报恢复版本、交叉验证静默降级。
  - 行为用例（9 条，本机有 pwsh 时执行、无则 skip）：把「Issue #3892 标记 → 文件末尾」整段原样抽进 harness，
    桩替换 `Start-Sleep`/`Get-CimInstance`/`Invoke-RestMethod`，按秒推进模拟日志，验证
    36s/48s/57s 慢注册不被回滚、无迹象且进程死亡 30s 停等、硬失败短路、回滚提示无 `Install complete!`、
    交叉验证生效与降级。

## 影响面与风险

- 仅 Windows 安装脚本的启动验证段；不改变 wire 协议、不动主控、不动 `install-worker.sh`。
- 真失败场景最多多等 30s（60s 封顶），且有硬失败/进程消失的提前停等兜底。
- 回退：单 commit revert 即可。

## 已知基线问题（非本次引入）

- `tests/runtime/test_worker_install_ux.py::test_sh_no_engine_install_traces`、
  `tests/runtime/test_worker_install_ps1_stderr.py::test_no_bare_pip_stderr_redirection`、
  `tests/api/test_install_rollback_regression.py::test_registration_watermark_sampled_before_agent_start`
  在基线分支（`arch-weekly-11-zj`，暂存本次改动后）同样失败，属既有问题。