# 开源合规审查检查项清单（openspec-checklist）

> 来源：《开源项目正式开源前通用优化方案（历史清洗 + 安全脱敏 + 提交规范化）》全文展开（Issue #311 附件）。
> 用途：PR 审查的**唯一依据**，逐条对照，不凭记忆转述。
> 三态结论：**符合 / 不符合（附证据：文件+行号 或 commit sha）/ 不适用**；证据不足 → **需人工复核**。

---

## §0 开源策略决策（对照 PR 是否引入违背已定策略的内容）

已定策略基线：新建公开仓 + 仅推一个开源分支 + 原地重写 + 凭据先吊销 + 提交英文。

| # | 检查项 | 判定 |
|---|--------|------|
| 0-1 | PR 是否引入新的真实凭据/密钥（策略：凭据先吊销、仓库零真实凭据） | 引入 → 不符合 |
| 0-2 | PR 是否新增绕过「仅开源分支」策略的 refs/强推操作 | 引入 → 不符合（一般不适用） |
| 0-3 | 作者身份/提交策略是否与基线一致（无新增内部真实身份暴露） | 暴露内部真实姓名/邮箱别名 → 不符合或需人工复核 |

## §1 敏感信息审计（核心章，逐项必查）

### §1.1 敏感信息类型全清单（对 diff 与新增文件逐模式扫描）

| # | 类型 | 匹配模式（典型形态） | 存放位置侧重 |
|---|------|---------------------|-------------|
| 1-1 | 云厂商密钥 | `AKIA…`、`Add_Auth_Token`、密钥 PEM/JSON 文件、`BEGIN (RSA\|OPENSSH\|EC) PRIVATE KEY` | configs/、.env*、settings*、cli/ |
| 1-2 | GitHub/GitLab PAT | `ghp_…`（≥20 位）、`glpat-…`、`oauth2:…` | 脚本、CI 配置、yaml |
| 1-3 | 数据库口令 | `password=…`、连接串明文（`user:pwd@host`） | 初始化/迁移脚本、测试夹具 |
| 1-4 | SSH/服务器口令 | 部署脚本内嵌 root 密码 | deploy*、*.sh、runbook md |
| 1-5 | 内网资产 | `192.168.*`、`10.*`、`172.16-31.*`、弹性公网 IP、`*.intra` 域名 | 文档、配置、注释 |
| 1-6 | 内部用户名/账号 | 真实姓名、内部邮箱别名、内部 bot 名 | git author、docs、README |
| 1-7 | 私有依赖 | 内网 npm/pypi 源、私有包名/域名 | requirements、package.json、.npmrc |

- 命中任一模式 → 不符合 + 证据定位（文件+行号）；值不确定是否真实凭据 → 需人工复核。
- 报告中敏感值一律掩码（`ghp_****`、`192.168.x.x`）。

### §1.2/§1.3 扫描与吊销（对 PR 增量审查）

| # | 检查项 | 判定 |
|---|--------|------|
| 1-8 | 新增的疑似泄露项是否已在替换/删除清单处理 | 新命中未处理 → 不符合 |
| 1-9 | PR 是否携带「已吊销凭据登记表」等本地文件 | 携带 → 不符合（该表只放本地不进仓库） |

### §1.4 删除路径与替换占位符

| # | 检查项 | 判定 |
|---|--------|------|
| 1-10 | 新增文件命中**删除路径**：`configs/` 真实配置（应只留 `*.example`）、`.env*`、含服务器拓扑/口令的 `deploy/` | 命中 → 不符合 |
| 1-11 | `docs/internal*` 内部文档 | ⚠️ 暂不删除保留：仅查占位符脱敏（真实值未替换 → 不符合）；删除与否不判 |
| 1-12 | 应脱敏保留结构的值是否已替换占位符（`ghp_<REDACTED>`、`YOUR_DB_PASSWORD`、`YOUR_SSH_PASSWORD`、内网 IP→`127.0.0.1`、内网域名→`example.com`） | 未替换 → 不符合 |
| 1-13 | 示例配置是否用 `.example` 而非真实值 | 用真实值 → 不符合 |

## §2 提交记录统一英文 + Conventional Commits（逐条校验 PR 全部 commit message）

格式基线：

```
feat(scope): add xxx
fix(scope): correct xxx when yyy
docs(scope): update xxx
refactor(scope): extract xxx
chore(scope): bump xxx
（后缀）(Closes #12) / (Issue #34)   ← 务必保留关联
```

| # | 检查项 | 判定 |
|---|--------|------|
| 2-1 | message 全英文（无中文/拼音混杂） | 非英文 → 不符合（附 commit sha） |
| 2-2 | Conventional Commits 格式：`type(scope): subject`，type ∈ feat/fix/docs/refactor/chore 等 | 不符 → 不符合 |
| 2-3 | 原 `Closes #N` / `Issue #N` 关联后缀是否保留 | 丢失 → 不符合 |
| 2-4 | PR 是否引入 commitlint/husky/CI 校验防反弹（涉及工程配置时） | 有 → 符合；未涉及 → 不适用 |

## §3 开源收尾质量清单（PR 涉及时逐项对照）

| # | 检查项 | 判定 |
|---|--------|------|
| 3-1 | LICENSE 存在且明确（Apache-2.0/MIT/GPL 等） | PR 改 README/发布相关而缺 LICENSE → 不符合或需人工复核 |
| 3-2 | README/快速开始与实际目录一致；示例配置用 `.example` | 不一致/真实值 → 不符合 |
| 3-3 | CONTRIBUTING / CODE_OF_CONDUCT / SECURITY 缺失 | 收尾未完成 → 不符合（属收尾阶段可不适用） |
| 3-4 | `.gitignore` 覆盖 `temp/ data/ logs/ build/ dist/ wheels/ *.env .env* *.pem *.key`，与已跟踪文件无矛盾 | 新增应忽略文件且未 ignore → 不符合 |
| 3-5 | 配置目录只留 `.example` 与必要默认值 | 真实配置入库 → 不符合 |
| 3-6 | CI/CD：Dependabot/Secret Scanning/push protection/lint/单测/构建 | 涉及 CI 改动时对照；未涉及 → 不适用 |
| 3-7 | 文档分层：对外文档进公开仓、内部文档（拓扑/事故/周报）留私仓 | 内部文档入公开范围 → 不符合或需人工复核 |
| 3-8 | 版权头/模板残留、内部绝对路径（`D:\`、`/root/`、`C:\Users\`） | 命中 → 不符合 |
| 3-9 | 清洗后初始版本标签 `v0.x.0`（涉及发布时） | 按需 |

## §7 任务计划表映射（报告汇总口径）

- 报告按 Phase 0（决策审计吊销）/ Phase 1（敏感清洗）/ Phase 2（提交英文化）/ Phase 3（收尾质量）分组汇总上述检查项结果。

---

## 报告模板（PR 评论 body，前缀 `[QA Agent]`）

```markdown
[QA Agent] PR #{N} 开源合规审查报告（head sha: {sha8}）

## 总体结论
{✅ 通过：0 项不符合 / ❌ 不通过：N 项不符合，M 项需人工复核}

## 逐项明细
| 规范章节 | 检查项 | 结论 | 证据定位 |
|---|---|---|---|
| §1.1-1-2 | GitHub PAT 泄露 | ❌ 不符合 | `src/foo.py:+12`（`ghp_****`） |
| §2-2 | commit 非英文 | ❌ 不符合 | commit {sha8}「修复登录…」 |
| §1.1-1-5 | 内网网段 | 🔍 需人工复核 | `docs/a.md:+3`（疑似测试地址） |
| §3-1 | LICENSE | ⚪ 不适用 | PR 未涉及 |

## 整改建议
1. （§1）删除 `configs/prod.yaml` 真实配置或改 `.example`；内网 IP 按 §1.4 替换占位符
2. （§2）commit message 重写为 Conventional Commits 英文并保留 `(Closes #N)`
...
```

## 判定红线

- 无证据不判「不符合」；证据不足 → 需人工复核。
- 敏感值掩码输出，不复述原文。
- 纯审查：报告之外零写操作。
