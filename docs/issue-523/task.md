# Task: Issue #523

## T1 测试先行（RED）
- [ ] tests/test_issue_523_create_403_attribution.py：仓库类型限制 403 / 权限类 403 / 未识别 403 / 正常建单成功 四类用例

## T2 实现（GREEN）
- [ ] src/api/issues_center.py 403 分支：提取上游正文 + 脱敏截断 + 语义分流（仓库类型限制 / 用户权限 / 通用回退）
- [ ] 401/422 分支保持不动

## T3 回归
- [ ] 新增用例 + tests/test_issues_api.py + 相关建单测试全部通过

## T4 收尾
- [ ] 自审查（5 轴 + pre-pr 增量检查：黑名单拦截 / 敏感扫描 / 全英文 commit）
- [ ] PR → weekly-08-mys，Issue 评论告知，A2A 交 QA
