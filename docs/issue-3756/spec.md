# Issue #3756 需求文档：/openapi.json 恒 500（契约登记表缺产物 + 渲染层无降级）

> 目标合入分支：`arch-weekly-11-ljt` | 来源：Issue #3756（Spec comment）| 2026-11-22

## Objective

- 让 mgmt 进程 `GET /openapi.json` 恢复 200 可用，并把「契约登记表未随产物落盘」从**致命 500**
  降级为**显式降级（`x-degraded`）+ 部署期拦截（打包 FATAL / 预检 exit 1）**。
- 为什么：`/openapi.json` 是契约探针（`src/prompt_builder.py`）、`grape refresh`、契约漂移门禁、
  SDK/命令生成的权威源；当前恒 500 使整条契约链路不可用，且每次探测都写 traceback 污染 Loki。
- 成功标准：`/openapi.json` → 200 且 paths ≥ 390；启动日志与 Loki 无
  `deprecated_endpoints.json` FileNotFoundError，path=/openapi.json status=500 归零。

## 现象与根因

1. `src/api/openapi.py:646-647` 的 `apply_deprecation_markup(schema)` 位于 `custom_openapi` 的
   try 之外，异常直穿 ASGI → 整个契约端点 500。
2. `src/domains/contracts/deprecation.py:27-28` 的 `DEPRECATED_ENDPOINTS_PATH` 指向
   `<repo_root>/contracts/deprecated_endpoints.json`，缺失即 `RuntimeError`（fail-closed）。
3. 部署侧缺口：`scripts/release_pack.py` 的后端包收集 `src/`、`scripts/`、根文件、
   `configs`/`requirements`/`help/`，**未纳入 `contracts/`**；`scripts/deploy_preflight.py`
   亦无契约文件自检。

## 范围

### In Scope

1. `scripts/release_pack.py`：后端包纳入 `contracts/` 整目录；打包期缺关键契约文件即 `[FATAL]` 退出
   （`deprecated_endpoints.json`、`deprecated_exemptions.json`、`openapi_baseline.json`、
   `required_operations.json`、`released/*.json`）。
2. `scripts/deploy_preflight.py`：新增「契约登记表就位」检查项，缺失/损坏/无 released 快照 → exit 1。
3. `src/api/openapi.py`：`custom_openapi` 中把 `apply_deprecation_markup` 单步包入
   `try/except Exception`，降级时根级写 `x-degraded=deprecation-registry-missing` 并 WARNING。
4. 测试补齐（先红后绿）。

### Out of Scope

- 不改 `load_deprecated_endpoints()` 的 fail-closed 语义（`test_registry_loader_fail_closed` 红线）。
- 不改 `contract_integrity` / `check_baseline`、`required_operations`、`x-changelog`。
- 异常边界只包 `apply_deprecation_markup` 单步；`attach_contract_meta` 与完整性门禁保持 fail-fast。
- 不做 `main`/`weekly-11` 分支 `contracts/` 回填（部署侧确认发布基线）。

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 降级落点 | 渲染层捕获（不改 loader） | loader fail-closed 是弃用纪律的数据面契约 |
| `x-degraded` 位置 | 根级 | 与 `x-changelog` 同级，探针一次读取即可判断 |
| 缺产物处置 | 打包 FATAL + 预检 exit 1 + 运行期降级 | 部署期暴露真实缺件，运行期保证只读契约不整体不可用 |

## Acceptance Criteria

- [ ] 后端发布包收集项含 `contracts/` 关键文件；缺登记表时打包 `[FATAL]` 失败。
- [ ] `deploy_preflight.py` 在 `contracts/deprecated_endpoints.json` 缺失时退出码 1 并给出 hint。
- [ ] 登记表缺失：`custom_openapi(app, check_baseline=False)` 返回 schema（不抛），
      `paths` 非空且 `x-degraded == "deprecation-registry-missing"`。
- [ ] 登记表正常：`deprecated` 标注输出不变，`tests/api` 与 `tests/contracts` 相关子集全绿。
- [ ] 新增用例先红后绿。
- [ ] 部署后运行验证由 SRE 执行（coding 阶段不跑长驻服务）：`/openapi.json` → 200、paths ≥ 390、Loki 500 归零。