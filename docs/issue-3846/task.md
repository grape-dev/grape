# Task — Issue #3846 任务拆解

| # | 任务 | 产出 | 状态 |
|---|------|------|------|
| 1 | 失败测试先行 | `tests/api/test_issue_3846_cloud_sandboxes_migration.py`（红：旧号仍注册、无 post_check） | commit 1 |
| 2 | renumber 202611180001 → 202611230010 | `ensure_cloud_sandboxes_table.py`（docstring/日志/`_INDEXES` 相关注释） | commit 2 |
| 3 | 新增 `_postcheck_cloud_sandboxes` 并以 `post_check=` 注册 | 同上文件（表 + 三索引校验） | commit 2 |
| 4 | db_setup 三处现状注释同步新号 | `src/api/db_setup.py`（纯注释） | commit 2 |
| 5 | CI 门禁接入 | `.github/workflows/windows-smoke.yml` linux-backend 新增 pytest 步 | commit 3 |
| 6 | 文档 | `docs/issue-3846/{spec,plan,task}.md` | commit 4 |
| 7 | 验证：新测试绿 + 既有相关测试（#3648/#536/track-guard/arch integrity）回归 | 本地 pytest | PR 前 |

验收口径见 spec.md；基线快照测试的存量红态（#3648 未刷新基线）不在本单修复范围。
