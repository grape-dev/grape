# Todo — Issue #238

- [x] 修改 hermes_acp.py: /tmp → ~/.cache/hermes-acp-homes
- [x] 修改 executor.py: mkdtemp(dir=~/.cache/hermes-iso)
- [x] 修改 sandbox.py: bwrap bind 路径同步迁移到 ~/.cache/hermes-acp-homes（自审查发现一致性缺陷）
- [x] 修改 cleanup.py + agent.py: 清理 glob 模式同步新增 ~/.cache/hermes-iso-*（自审查发现清理遗漏）
- [x] py_compile 语法校验通过
- [x] 功能验证：目录创建 + 清理逻辑正常
- [x] 五轴自审查（发现并修复清理逻辑 + sandbox 一致性问题）
- [ ] 建 PR (base: multitask)
- [ ] Issue 评论告知
- [ ] A2A 通知 QA
