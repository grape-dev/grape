# Issue #3579 Spec：pre-push 守卫钩子运行时解析解释器与模块路径

## 背景
`install_pre_push_hook` 安装共享 `hooks/pre-push` 时，把 `sys.executable` 与守卫模块
绝对路径固化为 marker。安装若发生在 `issues/<n>/coding/.venv` 内，worktree 回收后
解释器路径失效，钩子以 exit 127 直接阻断 push；该失败发生在 shell 层，内联 Python
的 `try/except` fail-open 尚未执行。

QA 复审（PR #1866 第 1 轮）追加两项发现：
- Required：2028-07 模板运行时优先加载 **push worktree 内**的守卫模块，部署态包导入
  兜底在 `-c` 模式下因 `sys.path[0]==''`（cwd=worktree）仍解析回同一棵树而不可达；
  守卫语义因此跟随 worktree 检出的提交（旧基线 worktree 跑旧守卫，缺 #2314/#2890
  修复；工作树内改 `branch_guard.py` 即可改写/关闭守卫），且 `_existing_hook_usable`
  用安装进程根校验存在性，与运行时真正加载的树不一致。
- Optional：`command -v python3` 与 worker 实际解释器（`start-worker.sh` 的
  `GRAPE_PYTHON` / `.venv` 前置）无关联，系统 python3 缺宿主依赖时守卫长期降级。

## 目标
- 钩子不固化任何 in-flight worktree 绝对路径。
- 守卫模块加载优先级：**部署态守卫优先，worktree 降为兜底**；可用性校验路径与
  运行时生效路径统一。
- 解释器与 worker 同源解析（`GRAPE_PYTHON` → `VIRTUAL_ENV` → push 树 `.venv` → PATH）。
- 解释器、守卫模块任一引导失败都 fail-open（exit 0 + 单行可定位告警）。
- 正常环境下分支租约、PR merged、远端漂移校验保持 fail-closed。
- 存量钩子在 `ensure_issue_worktree` 安装路径上被识别并升级。

## 方案
1. 模板写入 `# hook-module-rel:`（仓库相对模块路径）、`# hook-module-root:`
   （守卫模块稳定来源根）与 repo_key 三类 marker。
2. 安装时 `_stable_module_root` 判定来源根：安装进程跑在 issue 工作树
   （`issues/<n>/<agent_type>` 布局）或本次 push worktree 内时不记录（返回空串），
   只有部署态稳定安装根才写入 marker——避免回收后路径失效复发原始缺陷。
3. 解释器运行时按 `GRAPE_PYTHON` → `VIRTUAL_ENV/bin/python3` →
   `ROOT/.venv/bin/python3` → `command -v python3` 解析，任一失败 exit 0 告警。
4. 内联 Python 加载优先级（部署态优先）：`<module_root>/<rel>` →
   `<push_root>/<rel>` / `<cwd>/<rel>`（未记录稳定根时的兜底，与 2028-07 一致）→
   部署态包导入；包导入前移除 `sys.path` 的 `''`/cwd（否则 `-c` 模式下仍解析回
   worktree 同一棵树，部署安装根永远不可达）。按文件路径加载时把所属树插回
   `sys.path` 首位，保证守卫模块自身的 `src.*` 依赖与其来自同一棵树；
   spec/loader/import/hook_main 任一异常统一 fail-open。
5. `_existing_hook_usable` 校验 repo_key、相对模块路径、解释器可解析性与模块文件
   存在；存在性按 marker 记录的根校验（未记录时退回安装树自身，与运行时兜底候选
   同构），任一不可用即重写；旧版绝对 marker 模板一律视为不可用。
6. `HOOK_TEMPLATE_VERSION` 升级为 `2028-08`（模板 marker 集与解析链变化）。

## 验收
- [x] 解释器缺失 → exit 0 + 单行告警，无 127。
- [x] 模块缺失 → exit 0 + 单行告警。
- [x] 正常路径 → 守卫异常/违规仍返回 1（fail-closed）。
- [x] 模板不含 `sys.executable` / 守卫模块绝对路径。
- [x] 存量旧模板在安装时被重写升级。
- [x]（QA 复审）worktree 存在旧版模块时，加载来源为部署态守卫。
- [x]（QA 复审）worktree 无该文件时，走部署态来源加载。
- [x]（QA 复审）worktree 的同名 src 包不遮蔽部署态包导入。
- [x]（QA 复审）issue 工作树内安装不记录稳定根（防回收失效复发）。
- [x]（QA 复审）解释器按 GRAPE_PYTHON → VIRTUAL_ENV → .venv → PATH 顺序解析。
