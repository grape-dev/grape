# Runbook: pre-push 守卫故障期的 push 规避与修复（Issue #2706）

## 1. `--no-verify` 仅限守卫故障期临时使用

```bash
# ⚠️ 临时规避：仅在确认守卫自身故障（如缺 lock 模块 WARNING）时使用，
#    会跳过分支租约互斥与漂移校验，不可日常化；推送后必须尽快修复守卫。
grape git push --no-verify origin <branch>
```

守卫正常时**必须**走 `grape git push`（pre-push 钩子会做租约 + PR merged + 漂移校验）。
频繁需要 `--no-verify` 才能推送 = 守卫自身有缺陷，先按第 2 节修复，不要把规避当常规路径。

## 2. 缺 lock 模块（ImportError / ModuleNotFoundError）的修复动作

现象：push 时出现
`[branch-guard] WARNING ... src.kernel.lock ... fail-open` 或
`ImportError: cannot import name 'lock' from 'src'`（旧布局）。

修复：重装/升级 worker 发行包补齐模块，然后正常推送：

```bash
bash scripts/install-worker.sh   # 冒烟自检会拒绝缺 src/kernel/lock.py 的坏包
# 升级后验证（守卫 import 冒烟）：
python -c "import src.domains.runtime.engine.branch_guard; from src import lock"
grape git push origin <branch>   # 不带 --no-verify
```

- 新布局（arch-weekly-11 之后）：`src/kernel/lock.py` 随包下发，`src/__init__.py`
  提供 `src.lock → src.kernel.lock` 一跳别名（存量导入方兼容）。
- 旧布局存量部署（部署树含 `src/runtime/branch_guard.py`）：升级到含 #2292/#2706
  修复的发行包；升级前守卫缺 `src/lock.py` 时钩子已按 fail-open 放行并告警，
  不再阻断 push，但租约保护处于降级状态——须尽快完成升级恢复保护。

## 3. 守卫降级运行的观测信号

- 任务日志 / stderr：`branch guard lock module unavailable ... fail-open`（#2706 租约层）
- 钩子自检：`branch guard dependency missing: src.kernel.lock ...`（#2292）
- 两者任一出现 = 本次 push 无租约互斥保护，按第 2 节修复后复推一次确认告警消失。
