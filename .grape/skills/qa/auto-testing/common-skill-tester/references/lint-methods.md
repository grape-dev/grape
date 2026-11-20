# Lint 检查方法（Lint 套件执行细则）

对拉取到的**每一个检查项**（`common_skillslinter.json` 的 `groups[].items[]`），根据其 `rule` 字段描述的规则执行检查。

## 检查方法映射

**根据检查项 `check`/`rule` 字段内容动态匹配，不硬编码 ID。** 以下为常见检查类型的执行方法；遇到未知检查项，根据 `rule` 描述自行判断。

| 检查类型（按 check/rule 关键词匹配） | 执行方式 |
|---|---|
| SKILL.md 存在 | Glob 检查根目录是否有 SKILL.md |
| 目录路径格式 | 检查目标路径是否匹配 `skills/{category}/{skill-name}/`（注意：最新规范路径层级为 category/skill-name，无 subcategory） |
| Frontmatter 存在 | Read SKILL.md，检查顶部是否有 `---` 包裹的 YAML 块 |
| name/description/version 字段 | 解析 Frontmatter，检查字段是否存在及值是否合规 |
| name = 目录名 | 对比 Frontmatter name 值与目录名 |
| 命名规范 | 检查目录名是否符合 kebab-case |
| PR 单 Skill | 检查 git diff 涉及的 Skill 目录数量（如适用） |
| .env 文件 | Glob 搜索 `.env` 文件 |
| 危险脚本 | Grep 搜索 `sudo`、`rm -rf /` 等模式 |
| Skill 间调用 | Grep 搜索 SKILL.md 中是否引用其他具名 Skill |
| **引用完整性（补测）** | 对账 SKILL.md 正文 + 「五、参考文件清单」引用的 `references/*`/`scripts/*`/`templates/*` 文件**存在性**——引用不存在 = fail（附缺失路径+引用位置）；SKILL.md 运行时依赖的脚本（清理/初始化/拉取器）在 `scripts/` 缺失 = fail；frontmatter 触发词与 name/description 表述闭环 |
| 明文 IP/域名 | Grep 搜索内网 IP 段（10.x, 172.16-31.x, 192.168.x）和内部域名 |
| 脚本 shebang | Read .sh 文件首行，检查是否为 `#!/usr/bin/env bash` |
| 脚本语法 | Bash 执行 `python -m py_compile` 或 `bash -n` |
| MCP 协议 | 如适用，检查 JSON-RPC 格式（最新规范为 Low 级别） |
| 异常处理 | Grep 搜索 `set -euo pipefail` 或 `try/except` |
| 总大小限制 | Bash 统计目录总大小，阈值以检查点 JSON 中的 check 字段为准（最新为 50MB） |
| 文件数限制 | Glob 统计文件数量，阈值以检查点 JSON 中的 check 字段为准（最新为 40） |
| SKILL.md 行数 | Bash `wc -l` 统计行数，阈值 500 行（以检查点 JSON 为准） |
| 文件类型白名单 | 检查所有文件扩展名是否在白名单内 |
| 写操作确认 | Grep 搜索 SKILL.md 是否提及写操作需确认 |
| 删除操作确认 | Grep 搜索 SKILL.md 是否提及删除操作需确认（最新规范新增，Critical 级别，fail 处理为 FAIL） |
| 凭证硬编码与密钥泄露 | Grep 搜索 AK/SK/密码/token 模式，检查 configure set 传明文、读取/回显凭证文件 |
| 漏洞模式 | Grep 搜索已知漏洞代码模式 |
| 依赖安全 | 如有依赖文件，检查版本是否有已知漏洞 |
| 不安全配置 | Grep 搜索 http://（非 https）、弱密码等 |
| 安全评分子项 | 综合以上检查结果评分（敏感信息泄漏/命令注入/权限越权） |

## 判定逻辑

对每个检查项（含 `[补测]` 引用完整性），输出以下状态之一：

- **pass** — 检查通过
- **fail** — 检查不通过（根据 `failure_handling` 决定严重性；引用完整性 fail = 引用文件不存在，按缺失文件严重性定级——缺失脚本/引用断裂记 High）
- **warn** — 警告（`failure_handling` 为 WARN）
- **skip** — 不适用（注明原因）

## 评分公式

```
总分 = 100 - (Critical_fail × 15) - (High_fail × 8) - (Medium_fail × 4) - (Low_fail × 2) - (warn × 1)
```

星级/等级（与 tester、audit 统一口径）：

- 90-100: ⭐⭐⭐⭐⭐ 优秀
- 75-89: ⭐⭐⭐⭐ 良好
- 60-74: ⭐⭐⭐ 合格
- 40-59: ⭐⭐ 需改进
- 0-39: ⭐ 不合格

如果有安全评分子项（`score_map`），额外计算安全评分：`安全评分 = Σ(各子项得分)`，风险越高得分越低。

## 证据要求

- 每项 pass/fail 必须基于**客观证据**（文件是否存在、grep 命中内容、行号、统计数字），不主观臆断。
- fail 附修改建议：具体、可操作（指出文件位置 + 改法）。
- 报告语言：中文。