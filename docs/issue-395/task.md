# Task: Issue #395

- [ ] T1 测试基线：tests/test_issue_395_bugfixes.py 覆盖四缺陷边界（RED）
- [ ] T2 #381 P0：deps.py 路径 org 鉴权 + orgs.py update/delete 消费结果（404/403/0行）
- [ ] T3 #380：knowledge.py page/page_size ge/le 校验（422）+ main.py 全局异常脱敏
- [ ] T4 #382：webhook.py a2a repository 非 str → 400
- [ ] T5 #383：repos.py host_repo RuntimeError 分流（404/403/固定文案）
- [ ] T6 回归：相关既有测试全绿
- [ ] T7 原子测试：AT-KNW-06/AT-ORGP-03/AT-A2A-03/AT-REPO-06 转 XPASS（grape-test 仓侧验证）
