# Issue #699 Phase 1 方案设计（plan）

## 实施策略

「**物理迁移 + 仓库内 import 全量改写为 canonical + 旧路径 shim 兼容**」，分 3 个原子提交，每个提交后全量测试保持绿色。

### 为什么仓库内部 import 必须改写（而不是让内部继续走 shim）

1. **部分初始化环**：整包迁移后，若 canonical 代码仍 `from src.runtime.service import X`（旧路径），import 子模块需先执行 `src/runtime/__init__`（shim → 全量 import engine 包）→ 触发被 import 模块自身的部分初始化，`from ... import X` 将 AttributeError。
2. **双实例**：sys.modules 别名法会让同一文件以两个模块名各实例化一次（两份 `_DIRECT_POOL` 类全局态）。
3. **mock.patch 失效**：patch("src.session_manager.db") 打在 shim 命名空间上，canonical 模块不受影响；测试大量使用 patch 字符串路径。
4. 检索确认：`src.*` 旧路径 dot 形式引用约 410 个文件（src+tests+scripts），改写为纯前缀替换（`src.X` → `src.kernel.X` / `src.domains.<域>.X`），sed `\b` 词边界保证 `src.repo_key` 不被 `src.repo` 误伤。

### 提交拆分

| # | commit（全英文） | 内容 |
|---|------------------|------|
| 1 | `docs: #699 add Phase 1 spec, plan and task breakdown` | 本组文档 |
| 2 | `refactor: #699 extract src/kernel/ (18 modules, auth_sdk, db_migrations) with re-export shims` | 18 模块 git mv → kernel；auth_sdk 三件套；db_migrations 空壳；全仓 import 改写；7 个模块 `__file__` 根路径 +1 层修正；worker_package MANIFEST 增补 kernel；shim 20 个 |
| 3 | `refactor: #699 reorganize src/ into domains (mgmt/runtime/insight/contracts) with re-export shims` | 整包 git mv（auth/user/admin/runtime/agent/im/vcs/storage/observability + event_pipeline + 29 路由）；contracts 空壳；全仓 import 改写；main.py 聚合 import 按域拆分；shim ~140 个（含 agent.py `__main__` 透传）；MANIFEST 增补 domains |
| 4 | `feat: #699 add scripts/check-arch.py architecture guard with R1-R6 rules and arch tests` | check-arch.py + tests/arch/{test_no_circular_deps,test_domain_boundaries}.py |

### shim 模板

```python
"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.config"""
from src.kernel import config as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
```

- 包级 `__init__` shim 同模板（`from src.domains.runtime import engine as _impl`）。
- `src/runtime/agent.py` shim 追加 `from ... import main` + `if __name__ == "__main__": main()`，保证 `python -m src.runtime.agent`（worker systemd 入口）行为不变。

### __file__ 路径修正清单（深度 src/ → src/kernel/）

config.py（5 处 `dirname(__file__),".."` + SKILL_STORAGE_DIR 三层 dirname）、session_manager.py、mysql_compat.py、worker_package.py ROOT、featured_order.py、help_docs_sync.py、help_page_gen.py —— 各 +1 父层级，语义不变（worker 包内布局同步为 src/kernel/...，相对推导自洽）。

### worker 打包（worker_package.py MANIFEST）

保留原条目（旧路径 shim 继续随包下发，`python -m src.runtime.agent` / install-worker.sh 内联 import 不变），新增 `"src/kernel/"`、`"src/domains/"`；同步更新 MANIFEST 注释。test_worker_package 断言的旧路径（src/config.py、src/runtime/agent.py、src/agent/executor.py）由 shim 继续满足；import 闭包子进程用例验证 shim→canonical 链路。

### check-arch.py 设计

- AST 扫描 `src/kernel/**`、`src/domains/**` 的 import（含 from/import/`__import__` 字符串跳过——仅静态 import）。
- 规则：R1 kernel↛domains；R2 kernel↛src.api；R3 domains↛apps（预留，apps/ 不存在时 skip）；R4 跨域（runtime↔mgmt↔insight 直连）→ 白名单管控（存量暂列白名单，报告提示 Phase 5 收口）；R5 import 环检测（Tarjan SCC）；R6 kernel/domains↛旧路径 shim（src.config/src.runtime/... 等顶层 dot 路径）。
- 白名单：脚本内 `WHITELIST`（模块 → 规则 → 原因），供 QA 审阅。
- 退出码：R1/R2/R5/R6 违规 exit 1（CI 红牌）；R3/R4 违规 exit 0 仅报告。

### 风险与回滚

- 单 commit 内 git mv + 改写 + shim 原子完成，revert 即整体回滚；
- 每 commit 后跑 `pytest tests/`（Phase 0 基线 100 passed）；
- event_pipeline.py 仅移动不改内容（Phase 3 才动逻辑）。
