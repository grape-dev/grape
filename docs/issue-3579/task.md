# Issue #3579 Task

- [x] 模板去掉 `sys.executable` / 模块绝对路径，改运行时解析
- [x] shell 层解释器解析失败 fail-open（exit 0 + 单行告警）
- [x] 内联 Python 模块缺失/导入异常 fail-open
- [x] 守卫正常路径保持 fail-closed
- [x] `_existing_hook_usable` 校验解释器/相对模块路径/repo_key
- [x] `HOOK_TEMPLATE_VERSION` → `2028-07`
- [x] 新增三条引导路径回归 + 存量升级用例
- [x] 相关历史用例适配新模板断言

QA 复审（PR #1866 第 1 轮）修复：
- [x] Required：加载优先级反转为「部署态守卫优先，worktree 兜底」；安装时记录
      稳定来源根（`_stable_module_root`，issue 工作树安装不记录）
- [x] Required：部署态包导入前移除 `sys.path` 的 `''`/cwd（兜底不再解析回 worktree）
- [x] Required：`_existing_hook_usable` 存在性校验按 marker 记录根统一（校验路径
      = 生效路径）
- [x] Optional：解释器按 GRAPE_PYTHON → VIRTUAL_ENV → push 树 .venv → PATH 顺序
      解析，shell 与 Python 侧同源
- [x] 用例：worktree 存在旧版模块时加载来源为部署态（2 例：篡改/缺失）
- [x] 用例：worktree 无该文件时走部署态来源；同名 src 包不遮蔽部署态包导入
- [x] 用例：稳定根分类（issue 工作树 / push worktree / 部署根）
- [x] 用例：解释器解析顺序（GRAPE_PYTHON / push 树 .venv / Python 侧四级顺序）
- [x] `HOOK_TEMPLATE_VERSION` → `2028-08`；branch_guard 回归接入 linux-backend CI 门禁
