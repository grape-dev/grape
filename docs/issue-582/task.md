# Task Breakdown — Issue #582

- [x] T1 文档：docs/issue-582/{spec,plan,task}.md
- [x] T2 实现：scripts/atom_selfcheck.py（anchor_env 契约 + run_case + CLI，docstring 标注锚定规则）
- [x] T3 测试：tests/atomic/test_issue_582_env_anchoring.py（非路径透传/白名单锚定/探测兜底/URL/绝对路径/端到端子进程视角/失败不误报绿，共 12 例）
- [x] T4 验证：pytest 新用例 + 现有 atomic 套件通过
- [x] T5 提交推送 feat/issue-582，建 PR（base=weekly-08-mys），A2A 交 QA
