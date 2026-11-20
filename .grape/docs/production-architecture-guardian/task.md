# 任务拆解与验收记录：production-architecture-guardian v3

## 任务清单（逐项验收）

| # | 任务 | 验收方式 | 结果 |
|---|------|----------|------|
| T1 | 规则基线 23 条入库（含级别） | 规则 ID 计数 A1–A14、B1–B4、C1–C3、D1–D2 = 23 | ✅ |
| T2 | 检查项 43 条编号 + 四段式 | `grep -c "^| [A-J]-[0-9]"` = 43；每行 6 列（编号/内容/标准/证据/级别/依据） | ✅ |
| T3 | 四级唯一取值域 | 检查项级别列只出现 BLOCKED/Critical/Medium/Informational | ✅ |
| T4 | N1–N5 归位 | B-2 / D-3 / G-3 / H-2 / F-3 各自判定标准与证据形态齐备 | ✅ |
| T5 | Gate 阻断清单 + 17 面最低证据 + 条件模板 | gate-review §2（17 行）、§3（3 条阻断 + 口径澄清）、§4 | ✅ |
| T6 | 三态结论向后兼容 | gate-decision §1 首屏；裁决仅三取值 | ✅ |
| T7 | 脚本 JSON 可机器校验 | `--json` 输出 `schema/rule/summary/unbacked_numbers[]`，五字段齐备 | ✅ |
| T8 | 白名单四类出处 | `--whitelist` 非法 basis_type 被拒收并告警（自检断言覆盖） | ✅ |
| T9 | 「无出处不得给数字」保留 | SKILL.md 硬规则 1 + numerical-evidence-gate §2/§6 | ✅ |
| T10 | 路径零硬编码 | 全包 grep `C:\`、`/Users/`、`/root/`、`/home/` 无命中；脚本路径由 `__file__` 解析 | ✅ |
| T11 | 三支脚本可执行 | `chmod +x` + shebang；`magic_number_audit.py --self-check` OK；三支在仓库根实际运行 | ✅ |
| T12 | 交叉引用无悬空 | 引用扫描：13 处路径引用 0 missing；反向 0 未引用文件 | ✅ |
| T13 | 行数约束 | SKILL.md 130 行（≤130）；references 最大 160 行（≤250） | ✅ |
| T14 | 平台交互义务 | Prerequisites（`grape --version`/`grape refresh`/env 校验）、环境变量表、`grape tasks complete` 收尾 | ✅ |

## 自检命令留痕

```bash
# 行数
wc -l SKILL.md references/*.md                                  # 130 / ≤160

# 绝对路径零命中
grep -rn "C:\\|/Users/|/root/|/home/" . | grep -v scripts/      # 空

# 检查项与规则计数
grep -c "^| [A-J]-[0-9]" references/audit-checklists.md          # 43
# 规则：A1–A14 (14) + B1–B4 (4) + C1–C3 (3) + D1–D2 (2)          # 23

# 脚本自检与实际运行
python3 scripts/magic_number_audit.py --self-check               # OK
python3 scripts/magic_number_audit.py "$REPO_PATH" --json        # schema=grape.magic-number-audit/v1
python3 scripts/repo_inventory.py "$REPO_PATH" --json
python3 scripts/dependency_inventory.py "$REPO_PATH" --json

# 引用一致性（脚本扫描 references/templates/scripts 路径存在性）
# referenced: 13  missing: []  files not referenced: []
```

## 遗留与交接

- **sre 上传节点**：技能为 user-uploaded（skill_id=3161，scope=user），本 PR 只交付技能包；需 sre 经 `grape-skill-uploader` 重新上传注册并确认注册名沿用 `production-architecture-guardian`（version 3.0.0）与覆盖范围。
- **存量正文不可读**：平台 `skills detail --skill_id 3161` 返回 403（user-scope 非本人），本次以 Spec v2 结构 + Issue 正文规则 3.1 + 仓库内同类资产重建，未照抄旧文本；如需与线上原文逐字比对，需技能所有者协助导出。
