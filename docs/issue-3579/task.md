# Issue #3579 Task

- [x] 模板去掉 `sys.executable` / 模块绝对路径，改运行时解析
- [x] shell 层解释器解析失败 fail-open（exit 0 + 单行告警）
- [x] 内联 Python 模块缺失/导入异常 fail-open
- [x] 守卫正常路径保持 fail-closed
- [x] `_existing_hook_usable` 校验解释器/相对模块路径/repo_key
- [x] `HOOK_TEMPLATE_VERSION` → `2028-07`
- [x] 新增三条引导路径回归 + 存量升级用例
- [x] 相关历史用例适配新模板断言