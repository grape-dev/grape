# Issue #699 Phase 1 任务拆解（task）

| # | 任务 | 验证 | 状态 |
|---|------|------|------|
| T1 | 编写 spec/plan/task 文档 | 文档存在且评审通过 | done |
| T2 | kernel 提取：18 模块 git mv + auth_sdk + db_migrations | `python -c "from src.kernel.config import log"` | |
| T3 | kernel 提取：全仓 import 前缀改写（含 patch 字符串） | `grep -rn "from src.config import" src/ tests/ scripts/` 为 0 | |
| T4 | kernel 提取：7 模块 __file__ 根路径 +1 层 | `pytest tests/ -k "config or session or worker_package or skill"` | |
| T5 | kernel 提取：旧路径 shim ×20 + MANIFEST 增补 | TC-1.1/TC-1.2 命令 | |
| T6 | domains 重组：整包 git mv + contracts 空壳 + 29 路由迁移 | `ls src/domains/{mgmt/auth,runtime/engine,insight/observability}` | |
| T7 | domains 重组：全仓 import 改写 + main.py 聚合 import 拆分 | `python -c "from src.api.main import app"` | |
| T8 | domains 重组：shim ~140（含 agent __main__ 透传）+ MANIFEST 增补 | TC-1.3/TC-1.5 命令 + worker 闭包用例 | |
| T9 | check-arch.py R1~R6 + tests/arch/ | TC-1.4 命令 | |
| T10 | 全量回归 + TC-1.1~1.6 全部执行 | `pytest tests/` ≥ 100 passed | |

每任务一个原子动作，提交粒度：T1 → commit1；T2~T5 → commit2；T6~T8 → commit3；T9 → commit4。
