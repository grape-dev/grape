# Spec: Issue #500 — 日志噪音问题清理（bwrap WARNING 刷屏）

> 来源：Issue #500 Spec（issue agent 已发布）；本文为开发落地版。目标分支：local-liufei。

## 目标
- 清理 worker 日志中重复刷屏的 `hermes ACP bwrap wrap failed, running unsandboxed: cannot import name 'build_bwrap_cmd' from 'src.agent.sandbox'` WARNING；
- 修复 `build_bwrap_cmd` 导入失败根因，避免「开启 bwrap 即静默失效」。

## 现状与根因
- 三个调用点（`src/agent/hermes_acp.py` / `src/agent/hwcloud_acp.py` / `src/agent/executor.py`）均为「先 try-import `build_bwrap_cmd` 再降级告警」，不看配置开关；
- 部署环境 `/opt/grape` 中 `src/agent/sandbox` 是包目录（`sandbox/__init__.py`）且缺 `build_bwrap_cmd` 导出（陈旧产物遮蔽仓库的 `sandbox.py` 模块），导致每次执行都 ImportError → 逐条 WARNING；
- 仓库源码中 `sandbox.py` 本身导出正常，问题在部署布局与仓库不一致。

## 范围
- In：上述三个 bwrap 包装调用点改为「配置短路（关闭不进路径、不告警）」；`src/agent/sandbox` 修复导出根因；失败告警改一次性/DEBUG；三态测试。
- Out：不重新启用 bwrap 沙箱；不处理其它来源日志噪音；不改部署架构。

## 验收标准
- bwrap 关闭（含 sandbox 导入链路损坏场景）：日志 0 条 `bwrap wrap failed / running unsandboxed`；
- bwrap 开启 + 包装成功：命令带 bwrap 前缀、行为不回归；
- bwrap 开启 + 包装/导入失败：WARNING 仅 1 条（log-once），后续降 DEBUG，命令降级 unsandboxed 执行；
- 不引入新静默失败（真实执行异常日志路径不动）；现有 + 新增测试通过。
