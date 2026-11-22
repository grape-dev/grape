# Issue #3756 方案设计

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22

## 方案要点

三层收口：**打包期断言 → 部署期预检 → 运行期降级**，各层职责不重叠。

### 1. 打包期（`scripts/release_pack.py`）

- 新增 `CONTRACTS_REQUIRED_FILES`（registry / exemptions / baseline / required_operations）与
  `assert_contracts_ready(root)`：任一关键文件缺失、或 `contracts/released/` 无 `*.json` 快照
  → 打印 `[FATAL]`（措辞对齐 `static-dist/index.html` 缺产物）并 `sys.exit(1)`。
- `pack_backend` 在 `help/` 之后、根文件之前纳入 `contracts/` 整目录（`(contracts_dir, "contracts")`）。
- 抽出 `_backend_file_items()` 纯清单函数（不含 static-dist 构建校验），便于在不构建前端的前提下
  断言「后端包收集项含 contracts」。`_tar_filter` 现有跳过规则不涉及 `contracts/`。
- INSTALL.md 产物表格同步注明 contracts。

### 2. 部署预检（`scripts/deploy_preflight.py`）

- `check_contracts_registry(contracts_dir)`：
  - `deprecated_endpoints.json` / `deprecated_exemptions.json` 必须存在且为合法 JSON；
  - `released/` 至少一份 `*.json` 快照；
  - 失败写入 detail + hint，沿用既有汇总与退出码 1（不得启动）。
- `run()` 在 role 检查之后、DB 检查之前挂入（契约自检与 DB 无关，DB 不可达也要报出）。
- 新增 `--contracts-dir`（默认 `GRAPE_CONTRACTS_DIR` 或 `REPO/contracts`）用于测试注入缺失目录。

### 3. 运行期（`src/api/openapi.py`）

```python
try:
    from src.domains.contracts.deprecation import apply_deprecation_markup
    apply_deprecation_markup(schema)
except Exception as _de:
    schema.setdefault("x-degraded", "deprecation-registry-missing")
    log.warning("openapi deprecation markup degraded: %s", _de)
```

- 只包 `apply_deprecation_markup` 单步（含其 import）；`attach_contract_meta` 与完整性门禁保持原样。
- 正常路径行为零变化：登记表可读时 `deprecated` / `x-deprecated-since` /
  `x-deprecated-replacement` 输出不变。
- 新增模块级 `log = logging.getLogger(__name__)`。

### 4. 测试 seam

- 渲染层：`openapi.custom_openapi(app, check_baseline=False)`（用户可见行为：返回 schema、不抛）。
  monkeypatch `src.domains.contracts.deprecation.DEPRECATED_ENDPOINTS_PATH` 指向缺失文件，
  并在前后重置 `app.openapi_schema` 缓存，避免污染其它用例。
- 打包层：`release_pack.assert_contracts_ready(root)`（传临时 root 构造缺失 → `SystemExit`）+
  `release_pack._backend_file_items()` 断言含 `(".../contracts", "contracts")`。
- 预检层：subprocess 跑 `deploy_preflight.py --contracts-dir <tmp>`，断言 exit 1 且 hint 出现。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 误改 loader 语义 | 不改 `deprecation.py`，`test_registry_loader_fail_closed` 作为红线回归 |
| 降级掩盖真实缺产物 | 打包 FATAL + 预检 exit 1 + `x-degraded` + WARNING 四重可观测 |
| 降级分支吞掉其它渲染异常 | 异常边界只包 `apply_deprecation_markup` 单步 |
| `app.openapi_schema` 缓存污染其它用例 | 新用例前后保存/恢复该属性 |