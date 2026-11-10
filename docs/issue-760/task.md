# Issue #760 任务拆解（task）

| # | 任务 | 产出 | 状态 |
|---|------|------|------|
| T1 | 安装枚举 Provider + legacy 对账 | src/im/engine/installations.py + tests/im/test_installations.py | ✅ |
| T2 | MediaWorkQueue + Router 接线 | src/im/engine/media.py、router.py 接线 + tests/im/test_media_worker.py | ✅ |
| T3 | 飞书 WS 生命周期迁入 connect() | src/im/adapters/feishu/channel.py + tests/im/test_feishu_channel_lifecycle.py | ✅ |
| T4 | 回归全量 tests/im（52+新增 全绿） | pytest tests/im | ✅ |
| T5 | 文档 | docs/issue-760/{spec,plan,task}.md | ✅ |

提交策略：单原子提交 `feat: #760 P1 engine installation provider, media worker and feishu WS lifecycle`。
