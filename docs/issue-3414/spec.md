# Spec: Issue #3414 — engine_model.py 导入错误导致派单链路 500

> 来源：Issue #3414「Spec Document」评论（issue agent 已评级 ready，本文为开发侧对齐稿）。

## 需求（同 Issue Spec，不重复展开）
修复自部署形态下 engine_model/public 门面导入失败导致 agent-runtimes 500（新建需求永久 queued）；branch_guard 钩子依赖 canonical 化在基线已满足，本 PR 补 traceability 注释与回归测试；并增加自部署/worker 包导入齐套性冒烟校验防回归。

## 根因定位（相关仓回归用例 traceback，事件 seq 已脱敏）
```
cannot import name 'detect_engine_model'
from 'src.domains.runtime.engine.engine_model' (<install-root>/src/.../engine_model.py)
```
- 自部署机上的 engine_model.py 是**陈旧/部分升级**文件（缺 `detect_engine_model`）。
- 现行基线调用方 `_attach_engine_models` 经 `src.domains.runtime.public` 的惰性
  wrapper 调 `detect_engine_model`，wrapper **调用期**导入失败已被既有内层
  `try/except Exception` 兜住（返回空模型名，端点不 500）。
- 残余 500 面：若部署机连 public 门面本身都陈旧/缺 `detect_engine_model` 符号，
  函数内 `from src.domains.runtime.public import detect_engine_model` 仍会抛
  ImportError → 500 `/api/settings/agent-runtimes` → engine-select 无候选 →
  新任务 `runtime_id: 0` 永久 queued。
- 该导入失败违反 engine_model 自身的降级原则（docstring：任何解析失败返回空串）。

## 修复方案
1. **派单链路 500（服务端降级）**：`_attach_engine_models` 在函数级 from-import 外加
   try/except——**public 门面自身导入失败**时告警并跳过 local_config 兜底，仅附加
   DB recent_models，端点保持 200（模型字段留空，与 worker 未上报语义一致）。
   注：worker 包/文件本身的齐套性不在本 PR 修复范围；降级态 `model_name` 可能为空，
   属残余风险（已记录 Issue），需后续升级/补齐部署包消除。
2. **branch_guard 钩子依赖（基线已满足）**：当前基线已由 #2706 收敛点采用 canonical
   `LEASE_LOCK_MODULE="src.kernel.lock"`、`HOOK_DEP_MODULES=("src.kernel.lock",
   "src.kernel.session_manager")` 与 `_lock_module()` 惰性导入；本 PR 对该文件
   **无功能变更**，仅补 3 处 traceability 注释 + canonical 依赖/降级回归单测。
3. **导入齐套性冒烟防回归**：新增全仓静态导入完整性测试
   （AST 扫描 src/**/*.py 的 `from src.x import y` / `import src.x.y`，校验目标
   模块存在且符号已定义；仅 `try.body` 内的守卫导入属故意降级不查）——
   直接拦截「cannot import name ...」类缺陷；并将 engine_model 纳入 worker 包
   import 闭包子进程冒烟。

## 验收
- `_attach_engine_models` 在 public 门面导入失败时不再抛错，并打出降级告警（单测覆盖）。
- branch_guard 钩子依赖 canonical 路径已由基线满足；本 PR 回归测试覆盖钩子依赖逐名可导入与租约三入口收敛（**非功能变更**）。
- 全仓导入符号完整性测试通过；相关现有测试全通过。
- 复现环境（<prod-eip>）复验：**合入部署后由 SRE 复验**（当前为未观察项，不计为已满足）——新需求正常派发，无 500、无永久 queued。
- 残余风险（Issue 已记录）：降级态 `model_name` 可能为空；根因（陈旧部署包）需后续升级/补齐 engine_model 消除。
