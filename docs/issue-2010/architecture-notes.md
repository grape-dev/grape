# Issue #2010 — src/ 顶层布局与历史路径别名约定

> 适用对象：grape 系统开发者 / 维护者。本文替代 PR 描述作为仓库内可检索的约定入口。

## 最终布局

```
src/
├── __init__.py        # 历史路径别名层（唯一兼容面）
├── api/               # 主控 HTTP 层（FastAPI 路由、静态资源）
├── domains/           # 业务域：mgmt / runtime / insight + contracts（共享契约）
└── kernel/            # 跨域共享的 canonical 实现（配置、锁、repo、token、会话、打包…）
```

`src/` 顶层**只允许**这四个条目：`__init__.py` + 上述三个包。新增业务代码分别落到：

| 代码性质 | 落点 |
|----------|------|
| HTTP 路由 / 请求响应编排 | `src/api/` 或 `src/domains/<域>/routes/` |
| 某业务域的实现（鉴权、任务、观测…） | `src/domains/{mgmt,runtime,insight}/` |
| 跨域共享的底层能力（无域语义） | `src/kernel/` |
| 域间共享的数据结构 / 协议 | `src/domains/contracts/` |

**不要**再为「仓库管理 / 凭证 / 帮助 / 基础设施」这类横向分类新建 `src/` 顶层子包：
Issue #2010 首轮曾引入 `src/repos/`、`src/authn/`、`src/help/`、`src/infra/`，
但这些目录里 18/19 个文件只是指向 `src/kernel/*` 的兼容 shim，唯一真实实现
（`service_diagnostics`）也已并入 `src/kernel/`。多一层目录的收益是 0，
代价是旧路径变成两跳（`src.config → src.infra.config → src.kernel.config`），
且同一份映射要在三处维护。它们已随收敛阶段删除。

## 历史路径别名（唯一兼容面）

外部调用方可能仍在使用 Issue #699 时代的旧顶层路径（`src.lock`、`src.session_manager`、
`src.repo_key` 等）。这些路径由 `src/__init__.py` 中的 `LEGACY_MODULE_ALIASES`
通过 `sys.meta_path` finder **懒解析**到 canonical 模块：

```python
LEGACY_MODULE_ALIASES = {
    "src.lock": "src.kernel.lock",       # 一跳直达，无中间 shim 文件
    ...
}
```

语义保证：`import src.lock` 得到的模块对象 **就是** `src.kernel.lock` 本身
（`sys.modules["src.lock"] is sys.modules["src.kernel.lock"]`），因此
`monkeypatch` / 属性赋值 / 全局状态补丁在两条路径上等价。仓库内部的 import
一律写 canonical 路径；旧路径只服务外部调用方，`scripts/check-arch.py` 的 R6
硬门禁禁止 `kernel/` 与 `domains/` 内部回退到旧路径。

### 增删一条别名

1. 只改一处：`src/__init__.py` 的 `LEGACY_MODULE_ALIASES`；
2. `tests/arch/test_issue_2010_src_top_level_layout.py` 的 `CANONICAL_MODULES`
   与 `tests/arch/test_domain_boundaries.py` 的 `LEGACY_MODULE_TARGETS` 是**校验清单**，
   新增迁移模块时需同步（它们保证「表里的映射」与「实际文件」一致、且无中间跳）；
3. 若要退场（确认无任何外部引用），从表里删掉即可 —— 之后 `import src.<旧名>`
   会正常报 `ModuleNotFoundError`，不会静默命中别的模块。

### 迁移到 domains 的前置条件（Phase 5）

把 token / session / repo 这类 canonical 实体物理搬进 `mgmt.auth` / `runtime.vcs`
**不能直接搬**：`scripts/check-arch.py` 的 R1（kernel 不得 import domains）与
R5（kernel↔domains 循环）会立刻转红。正确顺序是先做 contracts 反转
（kernel 使用方经 `src/domains/contracts` 调用），再由域承接实现，属 Phase 5 范围。

## 相关门禁与守卫

| 位置 | 作用 |
|------|------|
| `scripts/check-arch.py` R1–R6 | 分层与遗留路径硬门禁（`LEGACY_SHIM_PREFIXES` 含全部 19 个旧名） |
| `tests/arch/test_issue_2010_src_top_level_layout.py` | 顶层只允许 3 包、别名一跳、无残留引用 |
| `tests/arch/test_domain_boundaries.py` | 旧路径与 canonical 同一对象 |
| `src/kernel/worker_package.py` `MANIFEST` | worker 打包闭包（`src/kernel/` 目录条目覆盖全部旧路径） |
| `.grape/skills/qa/.../grape_constraint_check.py` Gate 1-5 | QA 侧校验 MANIFEST 含 `src/kernel/` |