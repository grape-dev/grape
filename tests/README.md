# tests 目录布局（按 domain 规整，Issue #699）

`tests/` 采用与 `src/domains/` 对齐的目录结构，便于按领域定位用例、按领域独立跑回归。

```
tests/
├── conftest.py           # 全局 fixture（测试库隔离 / src 模块与 env 逐用例恢复）
├── mock_db.py            # 共享测试替身
├── contracts/            # 跨域契约（Issue #699 Phase 5 接口、字段/状态契约）
├── kernel/               # src/kernel：config / session_manager / lock / token / schema / 迁移
├── insight/              # src/domains/insight：可观测性、SLO、指标、探针、dashboard
├── api/                  # src/api 网关层：路由装配、OpenAPI、静态资源、中间件
├── mgmt/                 # src/domains/mgmt：auth/user/org/repo/skill/scene/template/feedback/IM
├── runtime/              # src/domains/runtime：agent/task/pipeline/event/chat/engine/webhook
│   └── im/               # src/domains/runtime/im：渠道适配、卡片渲染、引擎接线、去重/租约
├── arch/                 # 架构约束（R1–R6）
├── atomic/               # 原子切片 / 迁移类
├── js/                   # 前端 node:test 用例（原 tests/frontend 已并入）
├── playwright/           # 手工 E2E 脚本（非 pytest 用例，python 直跑）
├── curl/                 # 手工后端守卫脚本（bash，需 JWT）
└── README.md
```

前端与手工套件不参与 `pytest tests` 收集：

```bash
node tests/js/test_chat_recall_464.js          # 单条前端用例
bash tests/curl/test_r14_backend_guard.sh <jwt>  # 手工后端守卫
python tests/playwright/test_r12_fullscreen_zindex.py  # 手工 E2E
```

## 归属规则

1. 用例名带领域特征词优先（如 `test_obs_*` → insight、`test_agent_*` → runtime、
   `test_skill_*`/`test_scene_*` → mgmt、`test_config*`/`test_mysql_*` → kernel）。
2. 无特征词时按被测模块的 canonical 路径判定（`src.domains.<domain>` / `src.kernel` / `src.api`）。
3. `src/api/*` 只有网关装配类用例留在 `api/`；业务路由用例归对应 domain。

## 新增用例放哪

- 改的是 `src/domains/runtime/**` → `tests/runtime/`
- 改的是 `src/domains/mgmt/**` → `tests/mgmt/`
- 改的是 `src/domains/insight/**` → `tests/insight/`
- 改的是 `src/kernel/**` → `tests/kernel/`
- 跨域契约 / 字段契约 → `tests/contracts/`
- 只涉及路由装配、OpenAPI、静态资源 → `tests/api/`

## 运行方式

单个 domain（推荐，进程隔离、顺序稳定）：

```bash
pytest tests/runtime -q
pytest tests/mgmt -q
```

全部 domain 依次在独立进程运行（顺序稳定，等价于历史平铺布局的稳定性）：

```bash
make test-domains
```

整库一次跑（`pytest tests`）仍可用；注意本仓库部分用例对执行顺序敏感
（`pytest.ini` 已固定 `-p no:randomly`，逐用例 DB 隔离是 P1-B 待改造项），
不同顺序下的失败集合可能与历史平铺布局略有差异。

## 路径约定

用例内定位仓库根目录时，`tests/<domain>/x.py` 需比原平铺布局多一层：

```python
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
```

`tests/` 自身已由 `conftest.py` 注入 `sys.path`，共享 helper（如 `mock_db`）
可直接裸名 import。

## 对齐规则（2026-11-12 复规）

`tests/` 与 `src/` 一一对应，**不再接受平铺的 `tests/test_*.py`**：

| src | tests |
|-----|-------|
| `src/kernel/**`（含 `persistence/`） | `tests/kernel/**` |
| `src/domains/mgmt/**` | `tests/mgmt/**` |
| `src/domains/runtime/**`（含 `im/`） | `tests/runtime/**（含 im/）` |
| `src/domains/insight/**` | `tests/insight/**` |
| `src/domains/contracts/**` | `tests/contracts/**` |
| `src/api/**` | `tests/api/**` |

例外（不对应单个 src 包）：`arch/`（架构约束与仓级 ratchet）、`atomic/`（迁移原子切片）、
`js/ playwright/ curl/`（前端与手工套件）。新增用例按「被测代码所在包」落到对应目录；
跨域场景用例归到主被测域，并在文件 docstring 写明另一侧的契约来源。

搬移后 `ROOT` 计算比平铺时多一层，写新用例时统一用
`ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))`（域内两层）。

## 已知既有失败（搬迁前就存在，非目录规整引入）

- `tests/runtime/test_issue_990_session_token_snapshot.py`、
  `tests/runtime/test_issue_1753_handoff_target_branch.py`：依赖本线未合入的特性
  （task session token 快照 / scene advanced options），已改为带原因的 skip。
- `tests/arch/test_silent_except_ratchet.py`：分支合并新增文件带入静默 except，基线文件不允许上调豁免。
- `tests/contracts`：2 条 milestone 契约（旧）。
- `tests/mgmt` 域整体失败较多（多为鉴权夹具与外部平台 mock 依赖，非搬迁引入）。
- D105 三条源码守卫（org 哨兵、user_id 列亲和、`_ensure_column` 口径）已于 2026-11-12 修绿；
  `org_id` 列在存量 MySQL 上仍是 INT，`org_id=''` 比较会被强制转 0，语义与 TEXT 哨兵一致，
  全量 TEXT 化留待单独的数据迁移轮次。
