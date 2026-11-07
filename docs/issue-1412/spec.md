# Issue #1412 — 迁移版本号跨分支撞号修复（Spec）

## 目标

让 issue865 的 `agent_runtimes` 模型三列迁移在「已盖章的存量库」上真正执行，
恢复 `/api/settings/agent-runtimes`；并让迁移执行器能识别并报出「同号不同名」撞号。

## 现状

- `src/api/db_setup.py:3030` issue865 迁移登记 `version="202611030004"`，
  该号在存量库中已被 `issue-646-mysql-fks` 盖章。
- 执行器 `_applied_versions()` 只读 `version, checksum`，pending 判定仅比对
  version → 同号不同名被判「已执行」永久跳过，三列永不创建。
- checksum 漂移仅 WARNING 且日志只打印注册名，看不到库内盖章名，无法识别撞号。

## 期望行为

1. issue865 迁移 renumber 至未被占用的 `202611070001`（`name` 保持不变）。
2. `_applied_versions()` 增读 `name`，返回 `{version: (name, checksum)}`。
3. 注册名 ≠ 盖章名时 `log.error`，内容含 version、库内盖章名、注册名、
   「同号不同名=撞号，非普通漂移」及修复指引；行为仍跳过不执行（不阻断启动）。
4. `register_migration` docstring 自查清单补第 5 条：renumber 前核对目标库
   `grape_t_schema_migrations` 现值（人工步骤）。
5. checksum 漂移保持 WARNING 语义，不误报、不阻断。

## 验收标准

- 伪造 `202611030004=issue-646-mysql-fks` 盖章行 + registry 注册同名版本号 →
  ERROR 含两个名字与修复指引，且 fn 未执行。
- 无撞号不产生 ERROR。
- checksum 漂移仍 WARNING 不阻断。
- `pytest tests/atomic/` 全绿。
- 存量盖章库升级后三列存在、接口 200（QA/人工验证）。
- 空库与存量库 schema 一致。