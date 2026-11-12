# Issue #1931 实施（新架构适配版）

| # | 切片 | 类型 | 产出 |
|---|------|------|------|
| 1 | release_pack 过滤 static 契约生成物 + 4 条断言（先红） | test | tests/api/test_release_pack_openapi_filter.py |
| 2 | `_tar_filter()` 增加 `src/api/static/openapi.json` | fix | scripts/release_pack.py |
| 3 | write_openapi_contract「不缩小」保护 + 5 条断言 | test+feat | tests/api/test_write_openapi_contract_no_shrink.py、src/kernel/help_page_gen.py |

## 刻意不做
- **B 项 merge 脚本**：arch 已有 `src/api/openapi_merge.py:dump_contract()`（进程内合并 4 份 spec，
  合并失败降级 + 可重试），无需第二套脚本。
- **必要端点清单（CORE_WORKFLOW_ENDPOINTS）**：清单式哨兵依赖端点归属，路由搬迁即需同步维护；
  新架构按「磁盘对照」判定是否缩小，无需清单。
- **baseline 刷新**：`help/api/openapi-baseline.json` 是上次发布快照（255 路径），刷新应以
  arch 四进程实际合并结果为准，属运维动作，不放进本分支——避免把推测数字写进 checked-in 文件。

## 自证
- `pytest tests/api/test_release_pack_openapi_filter.py tests/api/test_write_openapi_contract_no_shrink.py` → 9 passed
- `pytest tests/api/test_check_openapi_endpoints.py` → 15 passed / 1 failed（arch 基线既有，未打补丁的干净树同样 1 failed）
