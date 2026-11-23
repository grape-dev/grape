# 规范来源（Spec Sources）

common-skill-tester 的三份规范 JSON 全部**在线动态拉取**，不硬编码任何检查项。每次执行必须拉取最新版本。

## 三份来源

| 套件 | 用途 | 文件 | raw URL（优先） | API v5（回退，base64 解码） |
|------|------|------|-----------------|------------------------------|
| lint | 结构合规/安全基线/资源限制检查点 | `common_skillslinter.json` | `https://raw.gitcode.com/developer-skill/skills-group-contribution/raw/update-audit-clearlist-20260924/tools/skills_checker/common_skillslinter.json` | `https://gitcode.com/api/v5/repos/developer-skill/skills-group-contribution/contents/tools/skills_checker/common_skillslinter.json?ref=update-audit-clearlist-20260924` |
| test | 运行时功能测试点 | `common_functions.json` | `https://raw.gitcode.com/developer-skill/skills-group-contribution/raw/update-audit-clearlist-20260924/tools/skills_checker/common_functions.json` | `https://gitcode.com/api/v5/repos/developer-skill/skills-group-contribution/contents/tools/skills_checker/common_functions.json?ref=update-audit-clearlist-20260924` |
| audit | 恶意代码/CWE 安全审计规则 | `common_auditor.json` | `https://raw.gitcode.com/developer-skill/skills-group-contribution/raw/update-audit-clearlist-20260924/tools/skills_checker/common_auditor.json` | `https://gitcode.com/api/v5/repos/developer-skill/skills-group-contribution/contents/tools/skills_checker/common_auditor.json?ref=update-audit-clearlist-20260924` |

拉取策略：raw URL 失败（403/超时/网络错误）→ 自动回退 API v5 → 仍失败则提示用户手动指定本地 JSON 文件（将文件路径作为 `--type` 对应的 URL 参数传入，脚本会对本地不存在的路径报错，此时由 agent 直接读取本地 JSON 内容执行）。

## 合并输出结构

`node scripts/fetch_specs.js`（不带 `--type`）输出：

```json
{
  "lint":  { "doc": "...", "groups": [ ... ] },
  "test":  { "doc": "...", "test_groups": [ ... ] },
  "audit": { "doc": "...", "groups": [ ... ] }
}
```

带 `--type` 时输出该类型的原始 JSON 结构。

## lint JSON 预期结构（以实际为准，不假设固定字段）

```json
{
  "doc": "...",
  "scope": "...",
  "severity_legend": { "Critical": "...", "High": "...", "Medium": "...", "Low": "..." },
  "automation_legend": { "full": "...", "semi": "..." },
  "groups": [
    {
      "group": 1, "name": "...", "item_count": 9,
      "items": [
        { "id": "GS-001", "check": "...", "severity": "Critical",
          "rule": "...", "failure_handling": "FAIL", "automation": "full" }
      ]
    }
  ],
  "summary": { ... }
}
```

## test JSON 预期结构（以实际为准，不假设固定字段）

```json
{
  "doc": "通用 Skill 功能测试点",
  "subtitle": "Skill 运行时功能验证 · 按专业测试方向组织",
  "priority_legend": { "P0": "必测项，fail 触发卡点打回", "P1": "中等，必须执行并给结果" },
  "automation_legend": { "full": "全自动", "semi": "建议补测" },
  "test_groups": [
    {
      "section": 1, "name": "正确性测试", "item_count": 5,
      "items": [
        { "id": "SF-F-01", "case": "触发词命中", "direction": "触发",
          "input": "用户输入含触发词", "expected": "Skill 被正确激活",
          "verify": "命中率统计", "priority": "P0", "automation": "full" }
      ]
    }
  ],
  "summary": { ... }
}
```

## audit JSON 预期结构（以实际为准，不假设固定字段）

```json
{
  "doc": "...",
  "groups": [
    {
      "group": 1, "name": "CRITICAL 严重安全检测", "item_count": 6,
      "items": [
        { "id": "SA-01", "check": "...", "severity": "CRITICAL",
          "rule": "...", "detection": "...", "example": "...", "cwe": "CWE-913" }
      ]
    }
  ],
  "cwe_patterns": [ { "cwe": "CWE-78", "name": "...", "example": "...", "mapped_rule": "SA-xx" } ]
}
```

> 注意：audit 的 CWE 模式可能放在顶层 `cwe_patterns` 或独立分组中，以实际拉取内容为准。

## 最新规范概要（截至 2026-09 拉取，实际以动态拉取为准）

- **lint**：6 组共 31 项检查（27 检查项 + 3 安全评分子项 + 1 新增删除操作确认）。分组：目录与元数据结构(9) + 提交与隔离(4) + 脚本安全规范(5) + 资源限制与声明(6) + 安全扫描(4) + 安全评分(3)。严重级别：Critical(15)/High(4)/Medium(6)/Low(3)。关键变更：GS-002 目录路径升级为 Critical；GS-017 MCP 协议降级为 Low；GS-019 总大小限制改 50MB；GS-020 文件数改 40；新增 GS-024 删除操作确认声明(Critical)。
- **test**：5 组共 19 条测试用例。分组：正确性测试(5) + 性能测试(4) + 兼容性测试(3) + 可靠性测试(4) + 集成编排测试(3)。优先级：P0(8)/P1(11)。关键变更：section 1 名称从"功能正确性测试"改为"正确性测试"。
- **audit**：4 组共 12 条检测规则 + 11 条 CWE 漏洞模式。分组：CRITICAL 严重安全检测(6) + WARNING 警告级(4) + INFO 提示级(2) + CWE 漏洞模式映射(11)。覆盖网络请求/文件操作/命令执行/代码注入/依赖安全/混淆隐藏。CWE 映射：CWE-913/78/502/522/749/1106/327/506/74。