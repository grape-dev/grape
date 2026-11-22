# Issue #3756 方案设计

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22

## 方案要点

三层收口：**打包期断言 → 部署期预检 → 运行期降级**，各层职责不重叠。

### 1. 打包期（`scripts/release_pack.py`）

- 必需文件清单唯一定义在 `src/domains/contracts/registry_files.py`
  （`REQUIRED_REGISTRY_FILES` / `CONTRACTS_REQUIRED_FILES` / `REQUIRED_RELEASED_GLOB`），
  打包与预检共同引用，避免两处清单各自漂移（审查 R1）。
- `assert_contracts_ready(root)`：任一关键文件缺失、或 `contracts/released/` 无 `*.json` 快照
  → 打印 `[FATAL]`（措辞对齐 `static-dist/index.html` 缺产物）并 `sys.exit(1)`。
- `pack_backend` 在 `help/` 之后、根文件之前纳入 `contracts/` 整目录（`(contracts_dir, "contracts")`）。
- 抽出 `_backend_file_items()` 纯清单函数（不含 static-dist 构建校验），便于在不构建前端的前提下
  断言「后端包收集项含 contracts」。`_tar_filter` 现有跳过规则不涉及 `contracts/`。
- INSTALL.md 产物表格同步注明 contracts。

### 2. 部署预检（`scripts/deploy_preflight.py`）

- `check_contracts_registry(contracts_dir)`：
  - 与打包共用 `REQUIRED_REGISTRY_FILES`（4 件：registry / exemptions / baseline /
    required_operations）必须存在且为合法 JSON——缺 baseline / required_operations 时
    完整性门禁仍会 fail-fast，旧预检只查 2 件会放过（审查 R1）；
  - `released/` 至少一份 `*.json` 快照；
  - 失败写入 detail + hint，沿用既有汇总与退出码 1（不得启动）。
- `run()` 在 role 检查之后、DB 检查之前挂入（契约自检与 DB 无关，DB 不可达也要报出）。
- 新增 `--contracts-dir`（默认 `GRAPE_CONTRACTS_DIR` 或 `REPO/contracts`）用于测试注入缺失目录。
- `deploy/README.md` 明确部署顺序「解包 → 预检 → 起服务」，并说明 `--contracts-dir`。

### 3. 运行期（`src/api/openapi.py`）

```python
from src.domains.contracts.deprecation import (
    RegistryUnavailable, apply_deprecation_markup)
try:
    apply_deprecation_markup(schema)
except RegistryUnavailable as _de:
    schema.setdefault("x-degraded", "deprecation-registry-missing")
    log.warning("openapi deprecation markup degraded: %s", _de)
```

- `deprecation.py` 新增 `RegistryUnavailable(RuntimeError)`，**只**表达登记表缺失/不可读
  （`FileNotFoundError` / `OSError`）；登记表损坏（非法 JSON / 条目非法）与挂牌位置漂移
  继续抛 `RuntimeError`，不被降级（审查 R2）。
- 只包 `apply_deprecation_markup` 调用；`import` 在 `try` 之外，半套代码（模块缺失）不得被吞成 200。
- `attach_contract_meta` 与完整性门禁保持原样 fail-fast。
- 正常路径行为零变化：登记表可读时 `deprecated` / `x-deprecated-since` /
  `x-deprecated-replacement` 输出不变。
- 新增模块级 `log = logging.getLogger(__name__)`。

### 4. 测试 seam

- 渲染层：`openapi.custom_openapi(app, check_baseline=False)`：
  - 缺失 → 返回 schema + `x-degraded`（不抛）；
  - 损坏 / 漂移 → `RuntimeError`（`corrupt` / `drifted`），不得降级；
  monkeypatch `src.domains.contracts.deprecation.DEPRECATED_ENDPOINTS_PATH` 指向临时文件，
  并在前后重置 `app.openapi_schema` 缓存，避免污染其它用例。
- 打包层：`release_pack.assert_contracts_ready(root)`（传临时 root 构造缺失 → `SystemExit`）+
  `release_pack._backend_file_items()` 断言含 `(".../contracts", "contracts")` +
  断言打包清单等于 `registry_files.CONTRACTS_REQUIRED_FILES`（单一来源）。
- 预检层：subprocess 跑 `deploy_preflight.py --contracts-dir <tmp>`：缺登记表 / 缺
  `openapi_baseline.json` / 缺 `required_operations.json` → exit 1；四件齐备 + released
  快照 → `contracts_registry.ok`。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 误改 loader 语义 | 仅新增 `RegistryUnavailable` 子类（`RuntimeError`），fail-closed 语义不变，`test_registry_loader_fail_closed` 作为红线回归 |
| 降级掩盖真实缺产物 | 打包 FATAL + 预检 exit 1 + `x-degraded` + WARNING 四重可观测 |
| 降级吞掉损坏/漂移（审查 R2） | 只捕获 `RegistryUnavailable`；损坏/条目非法/位置漂移仍 `RuntimeError` fail-fast，各有用例 |
| 预检清单窄于运行期门禁（审查 R1） | 打包/预检共用 `registry_files` 单一清单（含 baseline / required_operations） |
| `app.openapi_schema` 缓存污染其它用例 | 新用例前后保存/恢复该属性 |