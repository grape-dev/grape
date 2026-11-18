# 发现项模板（finding）

> 每条「不符合」一条 finding；字段缺一即视为不可复核。禁止编造数值风险分（如「风险 8.5/10」）。

```
### {检查项编号}·{规则编号} — {一句话标题}

- 级别：{BLOCKED | Critical | Medium | Informational}（取自四级体系，唯一取值域）
- 检查面/Phase：{review: 第 n 面 | audit: Phase X}
- 证据：{file:line} 或 {commit sha}
  ```{lang}
  {最小代码/配置片段，仅保留能支撑判定的行}
  ```
- 判定标准：{引用 references/audit-checklists.md 中该检查项的「判定标准」原文}
- 实际行为：{代码/配置如何违反该标准}
- 故障场景：{在何种负载/序列/输入下出问题，症状是什么}
- 验证方式：{安全测试 / 执行计划 / 压测 / 并发测试 / 重试测试 / 迁移回滚演练 / 恢复演练}
- 修复方向：{最小改动方向，不写具体实现代码}
- 修复顺序与依赖：{先修什么、依赖哪条 finding / 哪个前置改动}
- 数值依赖：{是 → 附 templates/numerical-register.md 登记行；否 →「无」}
```

## 使用约束

- 证据必须是可定位的（`file:line` / `commit sha`）；无法定位 → 该条落「证据不足（`INSUFFICIENT DATA TO VERIFY`）」+ 缺哪种测量，**不得**写成「不符合」。
- 级别只能取自四级；不得出现 `P0/P1/Blocker/High/Low` 等其它级别词。
- 同一规则的多个独立缺陷拆成多条 finding，共享证据时在「修复顺序与依赖」中标注。
- 涉及数字（超时/TTL/阈值/RPO/RTO 等）→ 必须附 `templates/numerical-register.md` 的登记行或 `<undecided: 需要何种测量>`。
- 敏感值掩码（`ghp_****`、`192.168.****`），不粘贴密钥、连接串、个人信息。
