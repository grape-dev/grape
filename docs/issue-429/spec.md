# Spec: Issue #429 — worker一键安装脚本 pip stderr 终止性错误修复

> 来源：issue Agent 可开发性分析（评级 ready）+ VOD 结构化收敛 | 2026-10-22
> 开发基线：`功能分支`（`$GRAPE_WORK_BRANCH`）｜开发分支：`feat/issue-429`

## 1. 问题

`scripts/install-worker.ps1` 顶部设置 `$ErrorActionPreference = "Stop"`。PowerShell 5.1 对原生命令做
`2>$null` 重定向时会把 stderr 行包装成**错误记录**写入错误流，配合 Stop 语义直接变成**终止性错误**。

pip 在检测到「有新版 pip 可用」时会向 stderr 打提示（`[notice] A new release of pip is available...`）。
两相叠加，安装脚本在依赖安装 / grape-cli 安装 / pip 自愈段随机终止，留下半成品状态：
代码已解压、依赖部分已装，但凭据未写入、agent 未启动、未注册。

实测崩溃点：可选的 grape-cli 安装步骤（`pip install ... 2>$null`）。

## 2. 目标

- pip 向 stderr 打「有新版 pip 可用」提示时脚本**不终止**，继续完成全流程
  （依赖 → grape-cli → 凭据 → 启动 → 注册）。
- 从源头静默 pip 版本检查提示；stderr 处理模式与同文件 tar 解压段（既有成熟模式）对齐。
- 幂等性保持：重跑安全，复用已有目录/venv，仅更新代码与配置并重启。

## 3. 范围

### In Scope
- `scripts/install-worker.ps1` 所有 `... 2>$null` 的 pip 调用点：
  - pip 自愈段：line 242（`_VenvPipOk`）/ 254（ensurepip L1）/ 262（get-pip L2）/ 274（rebuild 后 ensurepip L3）/ 279（rebuild 后 get-pip L3）
  - grape-cli 安装段：line 341（离线 wheel）/ 346（在线回退）/ 354（非 wheels 直接在线）
- 脚本顶部（`$ErrorActionPreference = "Stop"` 之后）设置 `$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"`。
- 新增 `tests/test_worker_install_ps1_stderr.py` 静态回归测试。

### Out of Scope
- Linux 版 `install-worker.sh`（bash 下 `2>/dev/null` 无此问题）。
- pip 自身升级逻辑、新功能特性。
- `Find-Python` 里 `& $py -c ... 2>$null`（探测失败走 catch 兜底，行为不变，见 §5.3）。
- Python tarfile 兜底解压 `... 2>$null`（同上，见 §5.3）。

## 4. 方案

双重保障（Spec 决策表选项 C）：

1. **源头静默**：脚本顶部设 `$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"`，pip 不再向 stderr
   打版本检查提示。
2. **防御层**：所有 pip 调用点统一改为「临时切 `Continue` → `2>&1 | Out-Null` 执行 →
   检查 `$LASTEXITCODE` → 恢复原 EAP」模式，与 tar 解压段（line 152-174）既有模式对齐。
   即使 pip 打其他 stderr 警告（deprecation 等）也不会触发终止。

pip 失败语义不变：仍以 `$LASTEXITCODE` 判断成败（失败照旧走 fallback / self-healing / Fail），
只是不再让 stderr 噪音伪装成致命错误。

## 5. 实现细节

### 5.1 顶部环境变量（line 26 之后）

```powershell
$ErrorActionPreference = "Stop"
# Issue #429: pip 版本检查提示（stderr）在 Stop 模式下会被 2>$null 重定向包装为终止性错误；
# 从源头禁用版本检查提示（pip 官方支持的环境变量），具体调用点另有 EAP 防御层（见 _Invoke-PipQuiet）
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
```

### 5.2 新增辅助函数 `Invoke-PipQuiet`

位置：`Log`/`Fail` 函数定义之后（参数校验之前）。

```powershell
# Issue #429: 在 $ErrorActionPreference="Stop" 下，PowerShell 对原生命令的 2>$null/2>&1 重定向
# 会把 stderr 行包装成错误记录，配合 Stop 变成终止性错误（pip "new release available" 提示即触发）。
# 临时切 Continue 执行（stderr 丢弃、不中断），用 $LASTEXITCODE 判定真实成败，然后恢复原 EAP。
# 与 tar 解压段（line 152-174）既有模式对齐。
function Invoke-PipQuiet {
    param([Parameter(Mandatory)][string]$CommandPath, [string[]]$Arguments)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $CommandPath @Arguments 2>&1 | Out-Null } catch {}
    $ErrorActionPreference = $prevEAP
    return $LASTEXITCODE
}
```

### 5.3 调用点改造清单

| 位置 | 原调用 | 新调用 | 失败语义 |
|------|--------|--------|----------|
| `_VenvPipOk` (242) | `& $VenvPy -m pip --version 2>$null \| Out-Null; return $LASTEXITCODE -eq 0` | `return (Invoke-PipQuiet $VenvPy @("-m","pip","--version")) -eq 0` | 不变 |
| L1 ensurepip (254) | `& $VenvPy -m ensurepip --upgrade 2>$null` | `Invoke-PipQuiet $VenvPy @("-m","ensurepip","--upgrade") \| Out-Null` | 由后续 `_VenvPipOk` 复核 |
| L2 get-pip (262) | `& $VenvPy $getPkgTmp 2>$null` | `Invoke-PipQuiet $VenvPy @($getPkgTmp) \| Out-Null` | 由后续 `_VenvPipOk` 复核 |
| L3 rebuild ensurepip (274) | `& $VsemPy -m ensurepip --upgrade 2>$null` | `Invoke-PipQuiet $VenvPy @("-m","ensurepip","--upgrade") \| Out-Null` | 由后续 `_VenvPipOk` 复核 |
| L3 rebuild get-pip (279) | `& $VenvPy $getPkgTmp2 2>$null` | `Invoke-PipQuiet $VenvPy @($getPkgTmp2) \| Out-Null` | 由后续 `_VenvPipOk` 复核 |
| grape-cli 离线 (341) | `& $VenvPip install -q --no-index --find-links $wheelsDir grape 2>$null` | `Invoke-PipQuiet $VenvPip @("install","-q","--no-index","--find-links",$wheelsDir,"grape")` + `if ($LASTEXITCODE ...)` | 不变（fallback 在线） |
| grape-cli 在线 (346) | `& $VenvPip install -q --no-cache-dir "git+https://gitcode.com/grape-dev/grape-cli.git" 2>$null` | 同上模式 | 不变（warning 放行） |
| grape-cli 在线 (354) | 同上 | 同上模式 | 不变（warning 放行） |

注：L1/L2/L3 调用改为函数后 `$LASTEXITCODE` 由函数返回值承载，调用处丢弃（rc=1 也由后续
`_VenvPipOk` 复核兜底）；grape-cli 段保留 `$LASTEXITCODE` 显式判断，与原结构一致。

### 5.4 不改动项（有意保留）

- `Find-Python` 中 `& $py -c "..." 2>$null`：探测失败会走外层 `catch {}` 兜底（Get-Command/参数异常同样如此），
  探测失败仅跳过该候选，不属本 bug（pip 提示）触发路径。
- Python tarfile 兜底解压 `... 2>$null`：外层有 `try/catch Fail "Extract failed"`，且该路径仅在无 tar 命令时触发。
  本 MR 保持范围收敛，不做顺手改写。
- `Log "Using Python: $Py ($(& $Py --version 2>&1))"`：子表达式内联 `2>&1` 合并流，仅显示用，无 Stop 风险。

## 6. 测试方案

新增 `tests/test_worker_install_ps1_stderr.py`（纯静态断言，Linux CI 可跑）：

1. `test_no_bare_pip_stderr_redirection`：脚本中 pip 调用点不得再出现 `2>$null`（Find-Python/tarfile 两处豁免见 §5.4）。
2. `test_pip_version_check_disabled_at_top`：`PIP_DISABLE_PIP_VERSION_CHECK` 在 `$ErrorActionPreference = "Stop"` 之后、首个 pip 调用之前设置。
3. `test_invoke_pip_quiet_eap_pattern`：`Invoke-PipQuiet` 函数含 EAP 临时切换（Continue）+ 恢复 + `$LASTEXITCODE` 返回。
4. `test_pip_calls_use_safe_wrapper`：pip 自愈段与 grape-cli 段的 pip 调用均通过 `Invoke-PipQuiet`。
5. `test_grape_cli_still_checks_exitcode`：grape-cli 段保留 `$LASTEXITCODE` 判断（失败语义不软化）。
6. `test_idempotent_relevant_assertions_unchanged`：关键幂等要素（Test-Path venv / Test-Path EnvFile / 备份目录逻辑）不受影响。

回归：`tests/test_runtime_install.py`、`tests/test_windows_worker.py` 全量（既有静态断言 + 端点测试）。

## 7. 验收标准（对照 Issue）

- [x] 有「有新版 pip 可用」提示环境下安装不终止（EAP 防御层 + 静默变量双保障）
- [x] 全流程端到端成功（依赖→grape-cli→凭据→启动→注册；失败语义不变，仅 stderr 噪音不再致命）
- [x] `PIP_DISABLE_PIP_VERSION_CHECK=1` 默认静默（脚本顶部设置）
- [x] 干净环境回归不受影响（既有测试全绿 + pip 失败仍走原 fallback/自愈路径）
- [x] 幂等性保持（不改幂等相关逻辑）
