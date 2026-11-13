# 任务拆解：grape-architecture-guardian-v3（Issue #2403）

> 验收口径来自 `.grape/docs/grape-architecture-guardian-v3/spec.md` §Acceptance Criteria。逐项勾选后才允许创建 PR。

## 任务清单

| # | 任务 | 验收标准 | 验证命令/方式 | 状态 |
|---|------|----------|----------------|------|
| 1 | 建独立技能目录 | `.grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/` 存在，含 SKILL.md/references/templates/scripts | `find .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3 -type f` | ✅ |
| 2 | 2.0 零改动 | diff 不含 `ai-coding-review/grape-architecture-guardian/` 任何文件 | `grape git status` + PR diff | ✅ |
| 3 | frontmatter 合规 | `name=grape-architecture-guardian-v3`、`tools: [bash, grape]`、description 内含触发条件与 24 条规则包 | `head -12 SKILL.md` | ✅ |
| 4 | 规则包 24 条齐备 | A1-A14 / B1-B5 / C1-C3 / D1-D2 各带 Severity；B5 标「内容待补充」 | `grep -c` 规则行；人工核对级别分布 12/7/4/1 | ✅ |
| 5 | 4 级定义 + 合入映射 | BLOCKED→不建议合入；Critical→条件整改；Medium→人工判断；Informational→不影响合入 | 规则包 §1 + SKILL.md 领域规则 | ✅ |
| 6 | 7 Gate 保留为维度 | 附 Gate→规则组映射，覆盖全部 24 条 | 规则包 §3 | ✅ |
| 7 | 硬规则 1（数值五源闸门） | 五源齐备；缺源写 `<undecided: 需要何种测量>` + `INSUFFICIENT DATA TO VERIFY` + 待决策清单 | `grep -n "undecided"` / `grep -n "待决策清单"` | ✅ |
| 8 | 硬规则 2（证据分级） | 8 个标签：`verified: standard/vendor/code/config`、`measured`、`load-tested`、`plausible`、`INSUFFICIENT DATA TO VERIFY` | `grep -n "verified:"` | ✅ |
| 9 | 7 模式 | audit/design/plan/review/security/scale/verify 均有材料、必经步骤、产出 | runbook Step 1 矩阵 + SKILL.md 模式行 | ✅ |
| 10 | review 唯一裁决 | APPROVED / APPROVED WITH CONDITIONS / BLOCKED 三选一；BLOCKED 须给具体技术缺陷（file:line） | runbook Step 5 裁决表 | ✅ |
| 11 | 发现项契约 | 每条含规则编号+Severity+file:line 或 commit sha+验证方式+修复顺序与依赖；禁数值风险分 | `templates/finding.md` | ✅ |
| 12 | 资产移植 | 10 个领域 references + 5 个 templates + 3 只只读脚本 | `ls references templates scripts` | ✅ |
| 13 | 脚本只读可运行 | 3 只脚本在仓库根 `--json` 跑通，零第三方依赖 | `python3 scripts/<f>.py . --json` | ✅ |
| 14 | 可移植性 | 无 `C:\Users\...`/`/home/`/`.claude` 绝对路径；bash 用 SKILL_DIR、python 用 `Path(__file__).resolve().parent` | `grep -rn "C:\\\\\|/Users/\|/home/\|\.claude"` | ✅ |
| 15 | 平台交互 | Prerequisites（≥0.3.2 / refresh / env 校验）+ env 表 + `[QA Agent]` 前缀 + `tasks complete` 结构化字段 | SKILL.md 对应章节 | ✅ |
| 16 | 自检无未标源数字 | A3/A4 标「明确业务要求（Issue #2403 原文）」；全文无未标源架构数字、无「最佳实践」式无据断言 | `grep -n "50tps\|200ms\|最佳实践"` | ✅ |
| 17 | 开发过程文档 | `.grape/docs/grape-architecture-guardian-v3/{spec,plan,task}.md` 随 PR 提交 | `ls .grape/docs/grape-architecture-guardian-v3` | ✅ |
| 18 | PR + A2A | PR base = `$GRAPE_WORK_BRANCH`；A2A 交棒 `qa`（agent 名） | `pulls get` 的 `html_url` / a2a 返回 seq | ✅ |

## 自检命令记录

```bash
wc -l .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/SKILL.md \
      .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/references/*.md      # SKILL ≤130 / reference ≤250
grep -rn 'C:\\\|/Users/\|/home/\|\.claude' .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/ || echo "no absolute path"
grep -rn "undecided\|INSUFFICIENT DATA TO VERIFY\|待决策清单" .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/
grep -rn "verified: standard\|load-tested\|plausible" .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/
grape git status                                                  # 2.0 路径必须不出现
python3 .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/scripts/repo_inventory.py . --json | head
python3 .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/scripts/magic_number_audit.py . --min-severity medium | head
python3 .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/scripts/dependency_inventory.py . --json | head
```

## 遗留项

- B5「相关依赖约束」原文无内容：占位保留，待用户补充；不阻塞本技能交付。
- D2「两种风格」原文未点名风格名称：按仓库现有主题机制判定，不补写名称。