# Issue #2923 Spec：branch guard 降级去噪 + 部署闭包自检

> 目标分支：`weekly-mys` | 优先级：P1 | 类型：bug / deployment

## 目标

修复 `/opt/grape` 部署缺 `src.vcs`（canonical 为 `src/domains/runtime/vcs/`）时，
`grape git push` 的 branch guard PR 查询虽然 fail-open 放行，却把
`No module named src.vcs` 原始异常写入 stderr 的问题：

1. 打包清单/部署闭包确保 branch guard PR 查询依赖（vcs、lock）随包下发；
2. 部署脚本增加模块 import 冒烟自检 + 安装包与运行时版本一致性校验；
3. branch guard PR 查询失败降级分层：明细进 debug 日志，用户侧仅一条可读中文提示；
4. 回归测试覆盖缺模块场景与降级输出。

## 现状

- `src/domains/runtime/engine/branch_guard.py` 的 `_guard_stage_failed` 无收集器时
  直接 `log.warning(message)`；钩子聚合输出 `_emit_fail_open_alert` 把
  `attempts[].error`（原始 ImportError 文案）写入 stderr。
- `scripts/install-worker.sh` 第 3.5 步只 import `src.kernel.config` 与
  `src.domains.runtime.engine.agent`，缺 vcs 的坏包仍能通过自检；错误分支还引用
  已删除的 `src.observability.otel`。
- `src/kernel/worker_package.py` 的 `MANIFEST` 已含 `src/domains/runtime/vcs/`、
  `src/kernel/`、`src/__init__.py`（lock 别名入口），但缺少针对该闭合的回归守卫。

## 范围

### In Scope

- branch guard 降级输出分层：钩子内 debug 明细 + 单条中文提示，保持 fail-open；
  服务端 preflight（无聚合收集器）维持 WARNING 语义，异常仍然可见。
- 钩子依赖自检（`src.lock` / `src.kernel.session_manager`）与顶层兜底同样不向
  stderr 泄漏 ImportError 原文。
- 部署脚本 import 冒烟自检扩展到 vcs / branch guard / lock / `src.kernel.otel`，且在
  **建 venv + 安装依赖之后执行**，同时覆盖全新安装与升级路径；自检模块必须在
  worker 最小依赖（WORKER_DEPS：aiohttp/requests 等）下可 import，不得引入 `prometheus_client` 等服务端依赖。
- 安装包 `VERSION` 与解压后 `src/` 运行时指纹一致性校验（不一致告警）；指纹按
  **相对归档路径 + 文件内容**计算，不含安装绝对路径，保证构建期与安装期同口径。
- 打包闭包与部署自检的回归测试（真实执行自检/指纹逻辑，而非仅文本断言）。

### Out of Scope

- 不重构 branch guard 判定逻辑与 fail-open 策略。
- 不恢复已删除的 legacy `src.vcs` 目录（架构规则禁止）。
- 不改动 VCS PR 查询协议。

## 验收标准

- [ ] 部署闭包含 canonical `src/domains/runtime/vcs/` 与 `src/kernel/lock.py` 对应入口
  （替代 Issue 原文 legacy 路径 `src/vcs`，架构规则禁止恢复 legacy 目录）
- [ ] 部署脚本 import 冒烟自检可发现缺 vcs / branch guard / lock 的坏包
- [ ] 冒烟自检在 venv 与依赖就绪后执行，全新安装同样生效（不仅升级）
- [ ] 冒烟自检在 worker 依赖（WORKER_DEPS）就绪的 venv 中可通过，且不 import 服务端专属模块（如 `prometheus_client`；新基线后 `agent` 顶层依赖 `requests`，纯标准库 venv 已非验收口径）
- [ ] 模拟 vcs 缺失时 push 仍成功（fail-open），stderr 不含 `No module named` / ImportError 原文
- [ ] 缺 lock / session_manager 或钩子模块加载失败时同样不泄漏原始异常
- [ ] 降级时仅一条可读中文提示（如「分支基线核验暂不可用，推送已放行」）
- [ ] 错误详情保留在 debug 日志；服务端 preflight 异常仍以 WARNING 可见
- [ ] 安装包 VERSION 与同内容运行时 `src/` 指纹一致（路径无关），仅在真实漂移时告警
- [ ] 新增回归测试真实执行校验逻辑并通过，相关既有测试不回归