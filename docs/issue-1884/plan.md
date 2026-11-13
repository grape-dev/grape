# Plan — Issue #1884

## 任务拆解（TDD 红/绿）

| # | 切片 | 类型 | 提交 |
|---|------|------|------|
| 1 | tests/test_worker_install_ux.py 新增断言：脚本无引擎安装痕迹 + `--engine` 退出码 1（对现状红） | test | test(#1884) |
| 2 | scripts/install-worker.sh 删除 `--engine`/`ENGINE`/`install_engine` 全部逻辑（使 1 绿） | feat | feat(#1884) |
| 3 | help/usage/faq.md 删除 `--engine` 说明与 `pip install 'hermes[acp]'` 建议 | docs | docs(#1884) |

## 实施要点

1. **红**：新断言按验收标准 1、2 编写，跑 `pytest tests/test_worker_install_ux.py -k "1884"` 确认失败（现状脚本仍含 `install_engine`/`--engine`）。
2. **绿（脚本）**：
   - 头注释 usage 去 `[--engine ...]`；流程注释第 5 步去"best-effort 安装执行引擎"；删第 23-24 行引擎注释；
   - 删 `ENGINE` 变量、`--engine)` 分支、`case "$ENGINE"` 校验、Unknown argument usage 的 `[--engine ...]`；
   - 删 5.7 `install_engine()` 整段与调用。
3. **绿（文档）**：faq.md Q3「安装引擎」删 `--engine` 行与 hermes pip 建议，统一"自行安装 + 自动发现注册"口径。
4. 回归：`pytest tests/runtime/test_worker_install_ux.py tests/runtime/test_worker_install_arch_check.py tests/runtime/test_worker_install_ps1_stderr.py` 全绿；验收 grep 无输出；`bash -n` 语法检查。
5. 自审查 → 建 PR（base=`arch-weekly-11`）→ 提交评审。

## Out of Scope 守卫

- 不动 `src/domains/runtime/agent.py` 引擎发现/重注册；
- 不动 5.8 codearts 信任段、5.9 codegraph 段；
- 不动 `install-worker.ps1`。
