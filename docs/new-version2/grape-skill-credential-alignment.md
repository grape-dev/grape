# grape 仓库 skill 凭据获取 + 角色称呼对齐方案

## 背景

hello-skill 仓库的 rest-api-skill-creator/tester 已完成以下改动：
1. repo-credentials.json 固定文件名 → 扫描所有 JSON 文件
2. 凭据获取项目知识优先（不是环境变量优先）
3. Agent 角色称呼统一（sre→SRE Agent / qa agent→QA Agent / coding agent→Coding Agent）

grape 仓库的 skill-creator/tester/publish 需要对齐。

## 改动范围

### 1. repo-credentials.json → 扫描所有 JSON 文件（10 处）

| 文件 | 行号 | 当前 | 改为 |
|------|------|------|------|
| skill-creator/SKILL.md | 135 | 递归扫描找 repo-credentials.json | 递归扫描所有 JSON 文件 |
| skill-creator/SKILL.md | 321 | 递归扫描 repo-credentials.json | 递归扫描所有 JSON 文件 |
| skill-tester/SKILL.md | 82 | 递归扫描找 repo-credentials.json | 递归扫描所有 JSON 文件 |
| skill-publish/SKILL.md | 16 | 找 repo-credentials.json | 扫描所有 JSON 文件 |
| skill-publish/SKILL.md | 17 | .project-info/ 下 repo-credentials.json | .project-info/ 下 JSON 配置文件 |
| skill-publish/SKILL.md | 18 | 项目知识库 repo-credentials.json | 项目知识 JSON 配置文件 |
| skill-publish/SKILL.md | 36 | 回退 repo-credentials.json | 回退 JSON 配置文件 |
| skill-publish/SKILL.md | 78 | repo-credentials.json config | JSON 配置文件 config |
| skill-publish/SKILL.md | 86 | 回退 .project-info/ 下 repo-credentials.json | 回退 .project-info/ 下 JSON 配置文件 |
| skill-publish/SKILL.md | 159 | 回退 .project-info/ 下 repo-credentials.json | 回退 .project-info/ 下 JSON 配置文件 |

### 2. skill-publish 令牌获取改为项目知识优先（4 处）

| 文件 | 行号 | 当前 | 改为 |
|------|------|------|------|
| skill-publish/SKILL.md | 16 | 优先读环境变量...缺失时扫描 | 优先从项目知识读取...缺失时扫描环境变量 |
| skill-publish/SKILL.md | 36 | 令牌优先读环境变量，缺失时回退 | 令牌优先从项目知识读取，缺失时回退环境变量 |
| skill-publish/SKILL.md | 86 | 优先环境变量，缺失时回退 | 优先项目知识，缺失时回退环境变量 |
| skill-publish/SKILL.md | 159 | 优先环境变量，缺失时回退 | 优先项目知识，缺失时回退环境变量 |

### 3. Agent 角色称呼统一（14 处）

| 文件 | 行号 | 当前 | 改为 |
|------|------|------|------|
| skill-tester/SKILL.md | 5 | qa agent 执行 | QA Agent 执行 |
| skill-tester/SKILL.md | 7 | A2A 交棒 sre | A2A 交棒 SRE Agent |
| skill-tester/SKILL.md | 53 | A2A sre | A2A SRE Agent |
| skill-tester/SKILL.md | 76 | 直接 A2A sre | 直接 A2A SRE Agent |
| skill-tester/SKILL.md | 147 | A2A 交棒 sre | A2A 交棒 SRE Agent |
| skill-tester/SKILL.md | 148 | sre 不会收到 | SRE Agent 不会收到 |
| skill-tester/SKILL.md | 167 | 不 A2A sre | 不 A2A SRE Agent |
| skill-tester/SKILL.md | 169 | A2A sre | A2A SRE Agent |
| skill-tester/SKILL.md | 198 | A2A sre | A2A SRE Agent |
| skill-tester/SKILL.md | 217 | A2A sre | A2A SRE Agent |
| skill-tester/references/test-report-format.md | 55 | coding agent | Coding Agent |
| skill-publish/SKILL.md | 194 | 交棒 sre | 交棒 SRE Agent |
| skill-creator/SKILL.md | — | 无小写 agent 称呼（已改过） | — |
| configs/skill-description.json | — | skill-tester description 里 qa agent/sre | QA Agent/SRE Agent |

### 4. skill-issue 角色称呼统一（24 处，第二批改动）

skill-issue 里的 coding agent / issue agent / sre 称呼也需要统一。

| 文件 | 处数 | 改动类型 |
|------|------|----------|
| skill-issue/SKILL.md | 1 | coding → Coding Agent |
| skill-issue/references/comment-guide.md | 8 | coding agent→Coding Agent(2) + issue agent→Issue Agent(5,含评论前缀) + coding Agent→Coding Agent(1) |
| skill-issue/references/issue-developability-analysis.md | 5 | coding agent→Coding Agent(2) + issue agent→Issue Agent(3) |
| skill-issue/references/spec-generation.md | 6 | coding agent→Coding Agent(5) + sre Agent→SRE Agent(1) |
| skill-issue/references/codeowners-guide.md | 1 | coding Agent→Coding Agent |

**影响分析**（已确认无影响）：
- grape 系统用正则 `\[([A-Za-z_]+)\s+Agent\]` + `re.IGNORECASE` 匹配评论标记——`[issue agent]` 和 `[Issue Agent]` 都能匹配
- AGENT_NAME 环境变量从 agent-description.json 注入（已是正确格式：Issue Agent / Coding Agent / QA Agent / SRE Agent）
- A2A API 参数值和 agent_type 保持不变

### 5. 不改动

| 项目 | 原因 |
|------|------|
| A2A API 参数值 `"agent": "sre"` | grape 系统注册名 |
| a2a:sre / a2a:coding | API 参数值 |
| configs 里的 `"agent_type": "sre"` | 系统注册名 |
| chat/SKILL.md 里的 sre | 非 skill-dev 链路的描述 |
| iac/iac-qa 里的 sre | IaC 链路，不在本次范围 |

## 验证

1. skill-creator/tester/publish/issue 无 repo-credentials.json 残留
2. skill-publish 令牌获取为项目知识优先
3. skill-creator/tester/publish/issue 无小写 agent 称呼（A2A API 参数和 agent_type 除外）
4. configs/skill-description.json description 无小写 agent 称呼
