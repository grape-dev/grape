# Plan: Issue #700 — Windows hermes 引擎 healthy 震荡修复

## 切片划分（TDD 红/绿分提交）

### 切片 1：register 降级守卫（治本）
- **红**：`tests/test_issue_700_healthy_oscillation.py::TestRegisterDegradationGuard`
  - 已 healthy + probe_status='ok' + probe_last 新鲜 → re-register 上报 healthy=False 时 DB healthy 保持 1
  - 探活过期（超过新鲜窗口）→ 降级仍生效
  - 心跳连败 2 次降 0 后，re-register 上报 True → 升回 1（原行为不变，防守卫误伤升路径）
  - 新引擎首次注册 healthy=False → INSERT 不受守卫影响
- **绿**：`src/api/routes/runtime.py` UPDATE 守卫
  - 计算"探活新鲜"判定（`_probe_fresh_guard` 辅助：读现有行 healthy/probe_status/probe_last，判新鲜窗口 2×RUNTIME_PROBE_INTERVAL，默认 1200s）
  - fresh 且上报 False → healthy 保持原值；其余情形沿用上报值
  - INSERT 分支不动

### 切片 2：ACP_CHECK_TIMEOUT env 覆盖
- **红**：`TestAcpCheckTimeoutOverride`
  - 默认 ≥20
  - `RUNTIME_ACP_CHECK_TIMEOUT=30` → 30
  - 非法值（`abc` / `0` / `-5`）→ 回退默认 + warning 日志
- **绿**：`acp_bootstrap.py` 常量改函数式解析（`ACP_CHECK_TIMEOUT` 保持模块级可变属性，导入即解析；`_resolve_acp_check_timeout()` 供测试）

### 切片 3：Windows 启动器解析
- **红**：`TestHermesVenvBinWindows`
  - `.cmd` `@py -3 -m hermes` 形态 + `venv\Scripts` 目录存在 python.exe → 返回 venv Scripts 目录
  - `.cmd` `@"C:\...venv\Scripts\python.exe"` 绝对路径形态 → 同上
  - `.exe` 直接解析（接受 .exe 路径，按同目录/`__PYVENV_LAUNCHER__` 不适用 → 用 .exe 所在 Scripts 目录 + python.exe 存在校验）
  - `.bat` 同 .cmd
  - 解析失败（无 python.exe / 乱内容）→ 返回空串
- **绿**：`_hermes_venv_bin()` Windows 分支
  - `_venv_bin_from_windows_launcher(cli)`：读 .cmd/.bat 文本提取 python 解释器路径（`@py -3` 特例：`py -3` → 查 launcher 同目录 `..\Scripts\python.exe`? 不 —— `@py -3` 是系统 Python Launcher，无法直接定位 venv；正确形态是 venv 的 .cmd 内容 `@"X\Scripts\python.exe" ...`）
  - `.exe`：`os.path.dirname(cli)` 即 venv Scripts 目录（`venv\Scripts\hermes.exe`），校验 `python.exe` 存在
  - 所有分支异常吞掉返回空串（保持现契约）

### 回归
- `pytest tests/test_issue_490_probe_idempotent.py tests/test_issue_471_reregister_robustness.py tests/test_issue_700_healthy_oscillation.py -v`
- 相关面（register/heartbeat/acp_bootstrap）全量：`pytest tests/ -k "runtime or acp or hermes" -m "not e2e_ui"`

## 关键实现细节

- 新鲜窗口阈值：`RUNTIME_PROBE_FRESH_WINDOW` env（默认 `2×600=1200s`）——直接读 env `RUNTIME_PROBE_INTERVAL`（默认 600）×2。
- 守卫放在 UPDATE 的 SQL CASE 里或 Python 侧预读行再决定 healthy 参数值。选择 **Python 侧预读**（SELECT 已有行 → 决定 healthy 参数 → UPDATE），可读性高、便于日志记录"守住不降级"事件。
- probe_last 时间解析容错：`datetime.fromisoformat` 失败 → 视为不新鲜（保守，允许降级）。
- `.cmd` 解析：逐行找 `@py`/`@python`/`@"python.exe"` 开头行，提取带引号或不带引号的路径 token；`py -3` 特例 → 尝试 `py -3 -c "import sys; print(sys.executable)"` 定位 base python，再由其反推 venv（`sys._base_executable` / `os.path.dirname` 上溯找 `pyvenv.cfg`）。**简化**：`py -3` 形态仅当 .cmd 位于 venv Scripts 目录内时，直接用同目录 python.exe（venv 的 Scripts 目录同时含 hermes.cmd 与 python.exe，`@py -3` 由 Python launcher 解析到注册的 python —— 但 venv .cmd 模板实际是 `@"%VIRTUAL_ENV%\Scripts\python.exe"` 或直接绝对路径）。对 `@py -3` 做子进程定位（`py -3 -c ...` 输出解释器路径 → 上溯找 pyvenv.cfg → venv Scripts）。
- `.exe` 解析：`os.path.dirname(os.path.realpath(cli))` 为 Scripts 目录，`python.exe` 存在即返回。

## 风险

- 守卫过强延迟发现真实故障 → 以探活新鲜度为界（1200s），心跳连败路径保持原样（2 次即降 0，窗口内仍生效）。
- `@py -3` 子进程探测慢（冷启动）→ 仅解析失败兜底时使用；解析不了返回空串走人工修复指引（现契约）。
