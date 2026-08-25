# skill-publish 优化方案

> 日期：2026-08-23
> 状态：待评审
> 范围：skills/skill-publish/SKILL.md + scripts/sync_skill_platform.sh

## 改动 1：sync_skill_platform.sh 环境变量加载

**问题**：脚本依赖 GITCODE_TOKEN/GITHUB_TOKEN 环境变量，但 agent 执行 bash 命令是独立 shell，export 不跨 shell 生效。agent 读到 token 后 export 到环境变量，但执行 sync 脚本时脚本进程拿不到。

**改动**：在环境变量解析段（行 89）之前加一段——环境变量缺失时从项目知识加载。

**位置**：sync_skill_platform.sh 行 88 之后、行 89 之前

```bash
# ---- 环境变量缺失时从项目知识加载（.project-info/ 下递归扫描 JSON）-----------
if [[ -z "${GITCODE_TOKEN:-}" && -z "${GITHUB_TOKEN:-}" ]]; then
  while IFS='=' read -r key val; do
    [[ -n "$key" && -n "$val" ]] && export "$key=$val"
  done < <(python3 -c "
import json, glob
for f in glob.glob('.project-info/**/*.json', recursive=True):
    try:
        d = json.load(open(f))
        for k, v in {**d.get('secrets',{}), **d.get('config',{})}.items():
            if v: print(f'{k}={v}')
    except: pass
" 2>/dev/null)
fi
```

**理由**：
- agent 不需要 export——脚本自己读项目知识
- 只在环境变量缺失时才读（有环境变量时跳过，不覆盖）
- 项目知识文件在 worktree 的 .project-info/member/ 目录下，脚本能访问
- 和 SKILL.md 的优先级一致：项目知识 > 环境变量

---

## 改动 2：sync_skill_platform.sh 返回契约标准化

**问题**：脚本返回纯文本（PUSHED=1 / BACKUP_BRANCH=xxx / TARGET_URL=xxx），agent 要用正则解析，容易出错。

**改动**：所有输出改为 JSON 格式。

**位置**：4 处输出点

当前：
```bash
echo "PUSHED=0"
echo "MESSAGE=${SKILL_NAME} 在 ${PUBLISH_REPO} 中不存在，无需下架"
```
改为：
```bash
echo "{\"PUSHED\":0,\"BACKUP_BRANCH\":\"\",\"TARGET_URL\":\"\",\"MESSAGE\":\"${SKILL_NAME} 在 ${PUBLISH_REPO} 中不存在，无需下架\"}"
```

当前：
```bash
echo "PUSHED=0"
echo "MESSAGE=${SKILL_NAME} 内容与目标仓库一致，无需推送"
```
改为：
```bash
echo "{\"PUSHED\":0,\"BACKUP_BRANCH\":\"\",\"TARGET_URL\":\"\",\"MESSAGE\":\"${SKILL_NAME} 内容与目标仓库一致，无需推送\"}"
```

当前：
```bash
echo "PUSHED=1"
echo "BACKUP_BRANCH=${BACKUP_BRANCH}"
echo "TARGET_URL=${TARGET_URL}"
```
改为：
```bash
echo "{\"PUSHED\":1,\"BACKUP_BRANCH\":\"${BACKUP_BRANCH}\",\"TARGET_URL\":\"${TARGET_URL}\",\"MESSAGE\":\"\"}"
```

失败时（clone/push/commit 失败）也统一在 stderr 输出错误，stdout 输出 JSON：
```bash
echo "{\"PUSHED\":0,\"BACKUP_BRANCH\":\"\",\"TARGET_URL\":\"\",\"MESSAGE\":\"git clone 失败或超时: ${PUBLISH_REPO}\"}" 
```

**退出码不变**：0=成功，1=参数/环境错误，2=git 操作失败

**SKILL.md 同步改**：行 109 和行 186 的"脚本输出 PUSHED=1/0"改为"脚本输出 JSON：{PUSHED, BACKUP_BRANCH, TARGET_URL, MESSAGE}"

---

## 改动 3：skill-publish SKILL.md 补入参契约

**问题**：文档没有明确定义必填/可选参数，agent 调用时参数不全出现隐式兜底。

**位置**：SKILL.md 入口判定段（行 55）之后

```markdown
## 入参契约

| 参数 | 必填 | 说明 |
|------|------|------|
| action | 是 | publish / unpublish |
| skill_name | 是 | skill 名称 |
| issue_number | 是 | Issue 编号，用于评论和 commit message |
| pr_number | 是（publish） | PR 编号，用于追溯 |
| skill_dir | 是（publish） | skill 源目录路径 |
| version | 否（publish） | 版本号，默认从 frontmatter 读取 |

缺失必填参数 → 终止任务，Issue 评论返回参数缺失。
```

---

## 改动 4：skill-publish SKILL.md 补 5xx 重试策略

**问题**：只有 401/403/404 处理，5xx/超时没有重试策略。

**位置**：安全硬约束段（行 24）之后

```markdown
8. **重试策略**：
   - 401/403/配置缺失 → 不重试，直接跳过该平台
   - 5xx/连接超时/DNS 失败 → 最多 3 次指数退避重试（1s、2s、4s）
   - 版本冲突 bump → 最多 3 次（已有规则）
```

---

## 改动 5：skill-publish SKILL.md 补敏感文件拦截

**问题**：发布前不扫描敏感文件，可能上传密钥到 ClawHub/Git 仓库。

**位置**：发布流程 Step 1 之后（行 85 之后）

```markdown
### 0. 敏感文件扫描（发布前必执行）

扫描 skill 目录，发现以下文件直接终止该平台发布并 Issue 评论告警：
- .env / .env.*
- id_rsa / id_ed25519 / *.pem / *.p12 / *.keystore
- 文件名含 key/secret 但实际非密钥（如 keyboard-config.json）→ grep 文件内容检测 AK/SK/token 模式，命中才拦截
```

---

## 改动 6：skill-publish SKILL.md 补下架指纹校验

**问题**：发布有指纹比对幂等，下架没有前置校验，存在误删风险。

**位置**：下架流程 Step 1 之后（行 159 之后）

```markdown
### 前置校验（下架前必执行）

1. 从目标仓库拉取远端 skill 目录
2. 计算远端 skill SHA-256 指纹
3. 与本地预期指纹比对
4. 不匹配 → Issue 评论告警「远端版本与预期不一致，请人工确认」→ 终止下架
5. 匹配 → 继续执行下架
```

---

## 不改动的项

- **并发控制**：grape A2A 链路是串行的，同一 skill 不会并发发布
- **dry-run 模式**：过度设计，grape 链路是 Issue 触发自动发布，没有预演场景
- **request_id 幂等**：grape 的 event_seq 就是幂等键
- **换行符归一化**：git .gitattributes 自动处理
- **版本 bump 进位全覆盖**：1.99.99 极端场景不会出现
- **changelog 截断**：ClawHub API 无长度限制
