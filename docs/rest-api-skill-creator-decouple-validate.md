# rest-api-skill-creator 自测解耦方案

## 问题

rest-api-skill-creator 的 Step 3.5 自测直接调用 rest-api-skill-tester 的 spec_check.py：
```
python3 {{SKILL_DIR}}/rest-api-skill-tester/scripts/spec_check.py skills/{name}/
```

问题：
1. creator 依赖 tester 的脚本——如果只装 creator 不装 tester，自测失败
2. creator 和 tester 不在同一机器时（分布式部署），creator 调不到 tester
3. tester 改了 spec_check.py 接口/路径，creator 自测挂
4. 违反高内聚低耦合原则

## grape skill-creator 的做法（参考）

grape skill-creator 自带 2 个脚本：
- `scripts/validate-skill.sh` — 结构验证（frontmatter/段落/文件结构/无mock/无硬编码/文件大小）
- `scripts/test-skill-commands.sh` — 功能测试（从 test-vars.json 提取用例，只读类跑真实调用）

只在第 4 步额外跑 tester 的 spec_check.py 做交叉验证（锦上添花，不依赖）。

## 方案

rest-api-skill-creator 新增自己的 `scripts/validate-skill.sh`，自测用自己的脚本。

### 新增文件

`skills/rest-api-skill-creator/scripts/validate-skill.sh`

校验项（只做 creator 自测该做的结构校验，不做 API 调用和生成质量检查）：

| # | 校验项 | 说明 |
|---|--------|------|
| 1 | SKILL.md 存在 | 基础 |
| 2 | frontmatter 完整 | name/version/description/triggers/tags/tools |
| 3 | name 与目录名一致 | kebab-case |
| 4 | 必填段落 | 安全硬约束/评论规范/Configuration/场景/知识/步骤/判断标准/输出规范/参考文档/分析结论示例/任务结束 |
| 5 | Python 入口文件存在 | scripts/*.py |
| 6 | references/api-reference.md 存在 | 端点文档 |
| 7 | templates/test-vars.json 存在 | 测试用例 |
| 8 | requirements.txt 存在 | 依赖声明 |
| 9 | 无 mock 代码 | 严禁假数据 |
| 10 | 无硬编码地址 | SKILL.md 和 api-reference.md 不含 IP |
| 11 | 无 GitCode | 安全硬约束不含 GitCode |
| 12 | 脚本用 requests 不用 urllib | 规范要求 |
| 13 | SKILL.md ≤500 行 | 包大小限制 |
| 14 | 文件数 ≤50 | 包大小限制 |
| 15 | test-vars.json 无敏感字段 | base_url/cred_prefix/auth_type 不应出现 |
| 16 | test-vars.json 字段完整 | id/name/command/method/category/description |

### 修改文件

`skills/rest-api-skill-creator/SKILL.md` Step 3.5 自测段落：

改前：
```bash
python3 {{SKILL_DIR}}/rest-api-skill-tester/scripts/spec_check.py skills/{name}/
```

改后：
```bash
bash {{SKILL_DIR}}/rest-api-skill-creator/scripts/validate-skill.sh skills/{name}/
```

### 不改动

- rest-api-skill-tester 的 spec_check.py 保持不变（tester 门控用）
- spec_check.py 的检查项比 validate-skill.sh 多（含生成质量 6 项 + 敏感字段 + 字段完整性），这些是 tester 门控的职责
- grape 的 skill-creator 不在本次范围

### 职责划分

validate-skill.sh 只做结构校验（不跑 API）。creator 自测分两步：
1. `bash validate-skill.sh` → 结构校验
2. `python3 skills/{name}/scripts/{name}.py --help` + 只读端点 API 调用 → 功能验证

| 校验项 | creator validate-skill.sh | creator API 调用 | tester spec_check.py |
|--------|--------------------------|------------------|---------------------|
| frontmatter 完整 | ✅ | - | ✅ |
| 必填段落 | ✅ | - | ✅ |
| 文件结构 | ✅ | - | ✅ |
| 无 mock | ✅ | - | ✅ |
| 无硬编码地址 | ✅ | - | ✅ |
| 无 GitCode | ✅ | - | ✅ |
| requests 不用 urllib | ✅ | - | ✅ |
| test-vars.json 无敏感字段 | ✅ | - | ✅ |
| test-vars.json 字段完整 | ✅ | - | ✅ |
| SKILL.md ≤500 行 | ✅ | - | ✅ |
| --help 无语法错误 | - | ✅ | ✅ |
| 只读端点 API 可达 | - | ✅ | ✅ |
| 生成质量 6 项 | ❌ | - | ✅ |
| --format 支持 | - | ❌ | ✅ |
| 退出码验证 | - | ❌ | ✅ |
| 完整 test-vars 用例 | - | ❌ | ✅ |

## 涉及文件

| 文件 | 操作 |
|------|------|
| `skills/rest-api-skill-creator/scripts/validate-skill.sh` | 新增 |
| `skills/rest-api-skill-creator/SKILL.md` | 改 Step 3.5 自测段落 |

同步改动（hello-skill 仓库）：
| 文件 | 操作 |
|------|------|
| `hello-skill/skills/rest-api-skill-creator/scripts/validate-skill.sh` | 新增 |
| `hello-skill/skills/rest-api-skill-creator/SKILL.md` | 改 Step 3.5 自测段落 |
