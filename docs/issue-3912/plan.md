# Plan: Issue #3912

1. 在 weekly-mys 基线上运行两门禁用例，实测超基线点（与 arch-weekly-11 Spec 取证树对账）。
2. 逐点修复为显式处理：branch_guard stderr 兜底 / terminal_disposition log.warning / a2a_terminal_guard log.debug。
3. 验证：`pytest tests/arch/test_silent_except_ratchet.py` 2 绿；受影响模块回归（branch_guard 2028/2890/2933、
   terminal guard 2816，共 89 用例）+ `tests/arch` 全量 35 用例通过。
4. 基线文件 `tests/silent_except_baseline.json` 与 EXEMPT_FILES 零改动（棘轮只拦新增语义保持）。
5. rebase 到 weekly-mys@885ceface（arch-weekly-11 新架构树）后重跑门禁，新基线另增 11 处超基线点：
   同样逐点显式化（debug/warning 留痕或显式覆盖），不调基线、不加豁免，保持门禁只拦新增。
