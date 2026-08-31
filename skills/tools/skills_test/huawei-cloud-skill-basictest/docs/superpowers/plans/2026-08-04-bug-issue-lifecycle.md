# BUG Issue 生命周期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 测试发现 BUG 时（PR 创建人 ≠ skills_bot）创建带 `bug` 标签的 issue 给 PR 创建人；PR 更新时重测并留言"还未修复"或关闭 BUG issue。

**Architecture:** BUG issue 的创建/留言/关闭由「专业功能测试」agent 在测试运行内通过 `gitcode_api.sh` 完成（新增 `issue comment` / `issue close` / `issue find-by-pr`）；`gitcode_event_handler.py` 在 PR update 事件时检测该 PR 已打开的 BUG issue 并派发重测 Multica 任务（幂等守卫）。仓库源 `/root/skills_test` 与已部署 `/root/.agents/skills` 两份同步。

**Tech Stack:** bash(gitcode_api.sh)、Python3(gitcode_event_handler.py)、markdown(skill/agent 指令)、multica CLI。

**文件清单（受本计划影响）：**

| 文件 | 改动 |
|------|------|
| `/root/skills_test/huawei-cloud-skill-basictest/scripts/gitcode_api.sh` | 新增 `issue comment`/`issue close`/`issue find-by-pr`（仓库源） |
| `/root/.agents/skills/huawei-cloud-skill-basictest/scripts/gitcode_api.sh` | 补齐 `issue close`；find-by-pr 匹配对齐 spec（标题 `(PR #n)` 空格兼容） |
| `/root/skills_test/huawei-cloud-skill-basictest/SKILL.md` | 7.4 改为「BUG issue 管理」；新增 7.8「重测与 BUG 闭环」 |
| `/root/.agents/skills/huawei-cloud-skill-basictest/SKILL.md` | 同上（两份一致） |
| `/root/skills_test/deploy/agent_test_instructions.md` | 第三节改为按 PR 创建人建 BUG issue；新增重测识别 |
| `/root/.agents/skills/gitcode-event-handler/scripts/gitcode_event_handler.py` | 新增重测派发逻辑 + 环境变量 + handle_pr 挂载 |
| multica agent「专业功能测试」(ID 19e4d25c-5f82-4280-bbca-4a001e321c8c) | `multica agent update --instructions` 同步 agent 指令 |

**统一约定（含对 spec 的修正）：**
- BUG issue 标题：`测试发现BUG: {skill} (PR #{n})`（与既有 `close-by-pr`/`url-by-pr` 排除 `测试\s*发现\s*bug` 过滤一致）
- BUG 标签：`bug`（小写，与 gitcode-event-handler `LABEL_BUG = ("bug", ...)` 及部署位一致；spec 原写 `BUG`）
- 匹配正则：`PR\s*#\s*{n}`（兼容有无空格）与 body PR URL

---

## Task 1: gitcode_api.sh — 新增 `issue comment` / `issue close` / `issue find-by-pr`

**Files:**
- Modify: `/root/skills_test/huawei-cloud-skill-basictest/scripts/gitcode_api.sh`
- Modify: `/root/.agents/skills/huawei-cloud-skill-basictest/scripts/gitcode_api.sh`

- [ ] **Step 1: 在仓库源 `issue)` 分支的 `labels-set)` 与 `close-by-pr)` 之间插入 `comment)`/`close)`/`find-by-pr)` 三个子命令**

在 `/root/skills_test/huawei-cloud-skill-basictest/scripts/gitcode_api.sh` 中，找到：
```bash
            close-by-pr)
```
紧邻其前插入：

```bash
            comment)
                number="${1:-}"; shift
                [[ -z "$number" ]] && { echo "错误: issue comment 需要 number" >&2; exit 1; }
                body=""
                while [[ $# -gt 0 ]]; do
                    case "$1" in
                        --body) body="$2"; shift 2 ;;
                        --body-file) body=$(cat "$2"); shift 2 ;;
                        *) echo "未知参数: $1" >&2; exit 1 ;;
                    esac
                done
                [[ -z "$body" ]] && { echo "错误: issue comment 需要 --body/--body-file" >&2; exit 1; }
                data=$(python3 -c "import json,sys; print(json.dumps({'body': sys.argv[1]}, ensure_ascii=False))" "$body")
                resp=$(req POST "/repos/$OWNER/$REPO/issues/$number/comments" "$data")
                req_ok "$(req_code)" "提交 issue #$number 评论" "$resp"
                echo "$resp"
                echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print('comment id:', d.get('id'))" 2>/dev/null || exit 3
                ;;
            close)
                number="${1:-}"
                [[ -z "$number" ]] && { echo "错误: issue close 需要 number" >&2; exit 1; }
                resp=$(req PATCH "/repos/$OWNER/$REPO/issues/$number" '{"state":"close"}')
                req_ok "$(req_code)" "关闭 issue #$number" "$resp"
                echo "✅ issue #$number 已关闭"
                ;;
            find-by-pr)
                pr_number="${1:-}"; shift
                label=""
                while [[ $# -gt 0 ]]; do
                    case "$1" in
                        --label) label="$2"; shift 2 ;;
                        *) echo "未知参数: $1" >&2; exit 1 ;;
                    esac
                done
                [[ -z "$pr_number" ]] && { echo "错误: find-by-pr 需要 PR 编号" >&2; exit 1; }
                OWNER="$OWNER" REPO="$REPO" PR_NUMBER="$pr_number" PR_LABEL="$label" TOKEN="$TOKEN" python3 <<'PYEOF'
import json, os, re, sys, urllib.parse, urllib.request
owner, repo, pr = os.environ["OWNER"], os.environ["REPO"], os.environ["PR_NUMBER"]
label = os.environ.get("PR_LABEL") or ""
token = os.environ["TOKEN"]
api = "https://api.gitcode.com/api/v5"
pr_re = re.compile(r"PR\s*#?\s*%s(?![0-9])" % re.escape(str(pr)), re.IGNORECASE)
url_re = re.compile(r"(pull|pulls|merge_requests)/%s\b" % re.escape(str(pr)), re.IGNORECASE)
found = {}
def collect(items, require_title=False):
    if isinstance(items, dict):
        items = items.get("data") or []
    for it in items or []:
        if str(it.get("state", "")).lower() not in ("open", "opened"):
            continue
        num = str(it.get("number"))
        title = it.get("title") or ""
        body = it.get("body") or ""
        matched = pr_re.search(title) if require_title else (pr_re.search(title) or url_re.search(body))
        if matched:
            found[num] = True
def get(path):
    req = urllib.request.Request(f"{api}{path}", headers={"PRIVATE-TOKEN": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())
try:
    collect(get(f"/repos/{owner}/{repo}/pulls/{pr}/issues"), require_title=True)
except Exception:
    pass
for page in range(1, 20):
    qs = urllib.parse.urlencode({"state": "open", "per_page": 100, "page": page})
    if label:
        qs += "&labels=" + urllib.parse.quote(label)
    try:
        items = get(f"/repos/{owner}/{repo}/issues?{qs}")
    except Exception as e:
        print("  [find-by-pr] 查询失败: %s" % e, file=sys.stderr)
        sys.exit(3)
    if not items:
        break
    collect(items)
    if len(items) < 100:
        break
print(" ".join(sorted(found.keys())))
PYEOF
                ;;
```

> 注：部署位已有 `find-by-pr`(旧版，标题只匹配 `PR#{n}`) 与 `comment`，需替换为上述规范版本并补 `close`；`find-by-pr` 用环境变量传值（避免单引号注入），先查 PR 关联 issue，再分页全仓按标题/body 匹配。

- [ ] **Step 2: 语法校验**

Run: `bash -n /root/skills_test/huawei-cloud-skill-basictest/scripts/gitcode_api.sh && bash -n /root/.agents/skills/huawei-cloud-skill-basictest/scripts/gitcode_api.sh`
Expected: 退出码 0，无输出。

- [ ] **Step 3: 只读冒烟测试 `find-by-pr`（真实验证，不产生副作用）**

Run:
```bash
GITCODE_TOKEN=ZKJa8ysnd83zTCSjHPHQ-5by bash /root/skills_test/huawei-cloud-skill-basictest/scripts/gitcode_api.sh issue find-by-pr developer-skill huaweicloud-skills-dev 1 --label bug
```
Expected: 退出码 0；输出为空或空格分隔的 open BUG issue 编号（匹配 PR#1）；stderr 无 `错误:`。

- [ ] **Step 4: 部署位同步**

Run: `diff /root/skills_test/huawei-cloud-skill-basictest/scripts/gitcode_api.sh /root/.agents/skills/huawei-cloud-skill-basictest/scripts/gitcode_api.sh`
Expected: 无差异（两文件一致）。

## Task 2: basictest SKILL.md — 7.4 改造 + 新增 7.8

**Files:**
- Modify: `/root/skills_test/huawei-cloud-skill-basictest/SKILL.md`
- Modify: `/root/.agents/skills/huawei-cloud-skill-basictest/SKILL.md`

- [ ] **Step 1: 第 7.4 节替换为「BUG issue 管理（强制）」（两份文件都改）**

将**仓库源** `/root/skills_test/huawei-cloud-skill-basictest/SKILL.md` 的旧 7.4 小节
```markdown
### 7.4 问题单创建（有 BUG 时不创建）

- **有 BUG 时（强制）**：**不创建 GitCode BUG issue**。BUG 发现通过上传到 PR 的总测试报告体现（7.5/7.6），`hd_skill_test_detail.issue_url` 留空
- **无 BUG 时**：无需创建 issue，也**不关闭需求 issue**（需求 issue 保持原状态）
- 创建失败（无凭证/网络/仓库不存在）不阻塞流程，在报告中注明原因
```
以及**部署位** `/root/.agents/skills/huawei-cloud-skill-basictest/SKILL.md` 的旧 7.4 小节
```markdown
### 7.4 BUG issue 管理（强制）

**有 BUG 时**，根据 PR 创建人决定是否创建 BUG issue：

- **PR 创建人 = `skills_bot`（豁免）**：不创建 BUG issue，BUG 通过总测试报告体现
- **PR 创建人 ≠ skills_bot**：
  - 用 `gitcode_api.sh issue find-by-pr <PR编号>` 定位该 PR 已有 BUG issue
  - **已有** → 在该 issue 留言"该 BUG 还未修复"
  - **无** → 创建 BUG issue：标题 `[BUG] {skill} PR#{n} 测试发现问题`，责任人=PR 创建人，内容含 BUG 描述+复现+**修复建议**，加 bug 标签
- `hd_skill_test_detail.issue_url` 记录 BUG issue URL（若有）

**无 BUG 时**：若该 PR 存在 open 的 BUG issue（说明 BUG 已修复），关闭它并在 issue 留言"BUG 已修复"

**不关闭需求 issue**（需求 issue 保持原状态，与 BUG issue 独立）
```
（两份文件存在差异，本步骤统一替换为同一份新内容）
替换为：
```markdown
### 7.4 BUG issue 管理（强制）

**有 BUG 时**，根据 PR 创建人决定是否创建 BUG issue（PR 创建人取 `gitcode_api.sh pr get <owner> <repo> <n>` 的 `user.login`）：

- **PR 创建人 = `skills_bot`（豁免）**：不创建 BUG issue，BUG 通过上传到 PR 的总测试报告体现（7.5/7.6）
- **PR 创建人 ≠ skills_bot**：
  - 用 `gitcode_api.sh issue find-by-pr <owner> <repo> <PR编号> --label bug` 定位该 PR 已有的 open BUG issue
  - **已有** → 不新建，把最新问题单追加为该 issue 评论
  - **无** → 创建 BUG issue：
    - 标题：`测试发现BUG: {target} (PR #{n})`
    - 标签：`bug`（`gitcode_api.sh issue labels-set <owner> <repo> <n> bug`）
    - 责任人：PR 创建人（`gitcode_api.sh issue create ... --assignee <login>`）
    - 内容：问题单（严重级别/复现步骤/期望 vs 实际）+ **修复建议**（问题单"改进点"字段）+ `--pr-url {PR链接}`
- `hd_skill_test_detail.issue_url` 记录 BUG issue URL（若有）

**无 BUG 时**：若该 PR 存在 open 的 BUG issue（说明 BUG 已修复），关闭它并在 issue 留言"BUG 已修复"（用 `issue close` + `issue comment`）；**不关闭需求 issue**（需求 issue 保持原状态，与 BUG issue 独立）

创建/留言/关闭失败（无凭证/网络/仓库不存在）不阻塞流程，在报告中注明原因
```

- [ ] **Step 2: 在第 7.7 节（测试结果入库）之后新增 7.8「重测与 BUG 闭环」（两份文件都改，追加同一段内容）**

在仓库源 SKILL.md 第 7.7 节之后（`### 7.8` 前无同名节），追加：
```markdown
### 7.8 重测与 BUG 闭环（重测任务时强制）

**触发条件**：本任务为重测任务（Multica 任务描述含「重测」标记，或该 PR 存在 open 的 BUG issue）。

**流程**：
1. 全量重跑本评测流程（与首次一致），生成问题单
2. 用 `gitcode_api.sh issue find-by-pr <owner> <repo> <PR编号> --label bug` 列出该 PR 的 open BUG issue
3. 对每个 open BUG issue 判定原 BUG 是否仍复现（按原 issue 问题单逐条比对最新问题单）：
   - **仍复现** → `gitcode_api.sh issue comment <owner> <repo> <n> --body "该 BUG 还未修复，最新复现信息：..."`（幂等：同一 BUG 已留言"还未修复"则更新为最新证据，不重复刷屏）
   - **未复现（已修复）** → `gitcode_api.sh issue close <owner> <repo> <n>` 关闭该 BUG issue，并 `issue comment` "BUG 已修复"
4. 重测结果照常：总测试报告上传 PR（7.5）、指派测试人 Dengbiqiang、测试结果入库（7.7）
5. **重测任务不新建 BUG issue**（仅复用/关闭已有）
```

- [ ] **Step 3: 更新命令清单与 7.4 旧注释**

将 SKILL.md 第 53 行注释 `# 命令能力保留；评测有BUG时不再调用（见7.4）` 改为 `# 有BUG时按7.4创建/复用BUG issue（PR创建人≠skills_bot）`；在 55 行后新增两行：
```markdown
bash scripts/gitcode_api.sh issue find-by-pr <owner> <repo> <PR编号> [--label bug]   # 定位PR的open BUG issue
bash scripts/gitcode_api.sh issue comment <owner> <repo> <number> --body-file <留言>   # 给issue留言
bash scripts/gitcode_api.sh issue close <owner> <repo> <number>   # 关闭issue
```

- [ ] **Step 4: 两份文件同步校验**

Run:
```bash
diff <(grep -A4 '### 7.4' /root/skills_test/huawei-cloud-skill-basictest/SKILL.md) <(grep -A4 '### 7.4' /root/.agents/skills/huawei-cloud-skill-basictest/SKILL.md)
diff <(grep -A6 '### 7.8' /root/skills_test/huawei-cloud-skill-basictest/SKILL.md) <(grep -A6 '### 7.8' /root/.agents/skills/huawei-cloud-skill-basictest/SKILL.md)
```
Expected: 均无输出（两份 7.4/7.8 一致）。

## Task 3: agent_test_instructions.md 改造

**Files:**
- Modify: `/root/skills_test/deploy/agent_test_instructions.md`
- Update deployed: multica agent「专业功能测试」instructions（`multica agent update --instructions`）

- [ ] **Step 1: 替换第三节「存在 BUG 时」的创建规则**

将 `/root/skills_test/deploy/agent_test_instructions.md` 第 49-59 行的"### 存在 BUG 时（阻断流程，不创建 BUG issue）"小节开头与步骤替换为：

```markdown
### 存在 BUG 时（阻断流程）

**创建/复用 BUG issue（按 PR 创建人决定）**：取 PR 创建人（`gitcode_api.sh pr get <owner> <repo> <n>` 的 `user.login`）：
- `== skills_bot`：不创建 BUG issue（BUG 通过上传到 PR 的总测试报告体现）
- `!= skills_bot`：
  - `gitcode_api.sh issue find-by-pr <owner> <repo> <n> --label bug` 查已有 open BUG issue
  - **已有** → 不新建，最新问题单追加为该 issue 评论
  - **无** → 创建：标题 `测试发现BUG: {skill} (PR #{n})`、标签 `bug`、责任人=PR 创建人、内容=问题单+修复建议+`--pr-url`；`hd_skill_test_detail.issue_url` 填 BUG issue URL
```

并在"### 无 BUG 时（正常流程）"增加一句：
```markdown
若该 PR 存在 open 的 BUG issue（说明已修复），用 `issue close` 关闭并在 issue 留言"BUG 已修复"。
```

- [ ] **Step 2: 新增重测任务识别**

在第六节"任务闭环"之后新增：
```markdown
## 六.5 重测任务（PR 更新触发）

若本任务为**重测任务**（Multica 任务描述含「重测」标记，或 PR 已存在 open 的 BUG issue）：
- 走 7.8「重测与 BUG 闭环」：全量重测 → 对每个 open BUG issue：仍复现 → issue 留言"该 BUG 还未修复"+最新证据；已修复 → `issue close` 关闭+留言"BUG 已修复"
- **不新建 BUG issue**
- 其余流程（总测试报告上传 PR、指派 Dengbiqiang、DB 节点更新）与首次一致
```

- [ ] **Step 3: 同步到已部署 agent 指令**

Run:
```bash
multica agent update 19e4d25c-5f82-4280-bbca-4a001e321c8c --instructions "$(cat /root/skills_test/deploy/agent_test_instructions.md)" --output json 2>&1 | head -5
```
Expected: 返回 agent 更新结果；`multica agent get 19e4d25c-...` 的 instructions 含 `重测` 与 `BUG issue`。

## Task 4: gitcode_event_handler.py — PR update 重测派发

**Files:**
- Modify: `/root/.agents/skills/gitcode-event-handler/scripts/gitcode_event_handler.py`

- [ ] **Step 1: 配置区新增两个常量**

在 `ENABLE_HUAWEI_RELATED_CHECK` 定义（约 174 行）后插入：
```python
ENABLE_PR_BUG_RETEST = os.environ.get("ENABLE_PR_BUG_RETEST", "1") == "1"  # PR update 时派发 BUG 重测
BUG_ISSUE_LABEL = os.environ.get("BUG_ISSUE_LABEL", "bug")                # BUG issue 标签
```

- [ ] **Step 2: 新增 `find_open_bug_issues` 与 `dispatch_pr_bug_retest`**

在 `handle_pr` 定义（2786 行）之前插入：
```python
def find_open_bug_issues(repo, pr_number):
    """返回该 PR 已打开的 BUG issue 编号列表; 失败返回 [] 不阻断主流程"""
    if not repo or not pr_number:
        return []
    nums = []
    try:
        pr_re = re.compile(r"PR\s*#?\s*%s" % re.escape(str(pr_number)))
        url_re = re.compile(r"(pull|merge_requests)/%s\b" % re.escape(str(pr_number)))
        def collect(items):
            for it in items or []:
                if str(it.get("state", "")).lower() not in ("open", "opened"):
                    continue
                text = f"{it.get('title') or ''}\n{it.get('body') or ''}"
                if pr_re.search(text) or url_re.search(text):
                    nums.append(str(it.get("number")))
        try:
            collect(gitcode_get(f"/repos/{repo}/pulls/{pr_number}/issues"))
        except Exception:
            pass
        for page in range(1, 20):
            path = f"/repos/{repo}/issues?state=open&per_page=100&page={page}&labels={BUG_ISSUE_LABEL}"
            items = gitcode_get(path)
            items = items if isinstance(items, list) else (items.get("data") or [])
            if not items:
                break
            collect(items)
            if len(items) < 100:
                break
    except Exception as e:
        logger.warning(f"[bug-retest] 查询 PR #{pr_number} BUG issue 失败: {e}")
    return list(dict.fromkeys(nums))


def dispatch_pr_bug_retest(repo, pr_number, pr_url, title):
    """PR 存在已打开 BUG issue 时派发重测任务给测试小队; 幂等: 已有活跃 retest 记录则跳过

    返回 {"bug_issues": [...], "dispatched": bool, ...}"""
    bug_nums = find_open_bug_issues(repo, pr_number)
    if not bug_nums:
        return {"bug_issues": [], "dispatched": False, "reason": "无已打开 BUG issue"}
    active = db_find_requirement(
        "gitcode_pr_id = %s AND requirement_type = 'retest' "
        "AND status IN ('dispatched','pr_created','in_progress') AND gitcode_issue_url LIKE %s",
        (str(pr_number), f"%/{repo}/%"),
    )
    if active:
        return {"bug_issues": bug_nums, "dispatched": False,
                "reason": f"已有活跃重测任务 {active.get('multica_issue_number')}"}
    m_title = f"[重测] PR #{pr_number} BUG 复测"
    m_body = (
        f"重测任务：PR #{pr_number} 存在已打开的 BUG issue，请按 huawei-cloud-skill-basictest 7.8 全量重测并更新 BUG issue。\n\n"
        f"PR 链接: {pr_url}\n"
        f"BUG issue 编号: {', '.join('#' + n for n in bug_nums)}\n"
        f"标记: 重测\n\n"
        f"规则：仍复现 → 在对应 BUG issue 留言'该 BUG 还未修复'；已修复 → 关闭该 BUG issue。"
    )
    m_issue_id, m_identifier = multica_issue_create(m_title, m_body, SQUAD_TEST)
    m_issue_url = f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"
    req_id = db_insert_requirement({
        "gitcode_issue_id": f"pr{pr_number}",
        "gitcode_issue_number": str(pr_number),
        "gitcode_issue_url": pr_url, "title": m_title, "body": m_body,
        "state": "opened", "author": "", "author_name": "",
        "created_at_gitcode": datetime.datetime.now(),
        "updated_at_gitcode": datetime.datetime.now(),
        "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
        "multica_issue_number": m_identifier, "status": "dispatched",
        "requirement_type": "retest", "gitcode_pr_id": str(pr_number),
    })
    db_insert_flow(req_id, f"pr{pr_number}", m_issue_id, {"node_test": ("in_progress", pr_url)})
    return {"bug_issues": bug_nums, "dispatched": True,
            "multica_issue_id": m_issue_id, "multica_issue_number": m_identifier}
```

- [ ] **Step 3: 在 handle_pr 中挂载重测派发**

在 `handle_pr` 内 PR 审查报告 gate 之后（约 2824 行 `if review_conclusion == "fail":` 块之后、`# 逻辑3:` 注释之前）插入：
```python
    # ---- PR update 重测派发(skill 测试 BUG 闭环) ----
    # 仅当该 PR 已存在 open 的 BUG issue 时派发重测; open 事件测试尚未跑必然无
    # BUG issue, 天然只作用于 update 事件; skills_bot 的 PR 不创建 BUG issue,
    # 双保险再过滤一次。幂等由 dispatch_pr_bug_retest 内部守卫。
    if ENABLE_PR_BUG_RETEST and repo and pr_number and author_login != "skills_bot":
        try:
            retest = dispatch_pr_bug_retest(repo, pr_number, pr_url, title)
            if retest.get("dispatched"):
                safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(
                    f"检测到该 PR 存在已打开的 BUG issue"
                    f"({', '.join('#' + n for n in retest['bug_issues'])}), 已触发重测, "
                    f"测试完成后将更新 BUG issue 状态。"))
        except Exception as e:
            logger.warning(f"[bug-retest] 派发重测失败: {e}")
```

- [ ] **Step 4: 语法编译检查**

Run: `python3 -m py_compile /root/.agents/skills/gitcode-event-handler/scripts/gitcode_event_handler.py`
Expected: 退出码 0，生成 `__pycache__/gitcode_event_handler.cpython-*.pyc`（可删除）。

- [ ] **Step 5: 单元级冒烟（只读，验证 find_open_bug_issues 不报错）**

Run:
```bash
python3 - <<'PY'
import os, sys, importlib.util
os.environ.setdefault("GITCODE_TOKEN", "ZKJa8ysnd83zTCSjHPHQ-5by")
os.environ.setdefault("MULTICA_TOKEN", "mul_02fc8ffce670ac588a8d1bc9cc75861240521886")
os.environ.setdefault("DB_PASS", "SkillsMysql@2026")
spec = importlib.util.spec_from_file_location("geh", "/root/.agents/skills/gitcode-event-handler/scripts/gitcode_event_handler.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print("open_bug_issues:", m.find_open_bug_issues("developer-skill/huaweicloud-skills-dev", 1))
PY
```
Expected: 打印 `open_bug_issues: []`（或已有 BUG issue 编号列表），无 traceback。

- [ ] **Step 6: 自检**

Run: `python3 /root/.agents/skills/gitcode-event-handler/scripts/gitcode_event_handler.py --test`
Expected: 数据库/GitCode/Multica/四小队 全部 OK（数据库自检可能依赖本机 DB 连通性，单条 FAIL 需人工确认非本次改动引入）。

## Task 5: 部署同步与最终校验

- [ ] **Step 1: 仓库源 ↔ 部署位一致性**

Run:
```bash
diff /root/skills_test/huawei-cloud-skill-basictest/SKILL.md /root/.agents/skills/huawei-cloud-skill-basictest/SKILL.md
diff /root/skills_test/huawei-cloud-skill-basictest/scripts/gitcode_api.sh /root/.agents/skills/huawei-cloud-skill-basictest/scripts/gitcode_api.sh
diff /root/skills_test/deploy/agent_test_instructions.md /tmp/check_agent_instructions.md
```
Expected: 前两条无输出；第三条先导出 agent 当前 instructions 再比对（若 deployed 指令已更新则一致）。

- [ ] **Step 2: 全量语法校验**

Run:
```bash
bash -n /root/skills_test/huawei-cloud-skill-basictest/scripts/gitcode_api.sh
bash -n /root/.agents/skills/huawei-cloud-skill-basictest/scripts/gitcode_api.sh
python3 -m py_compile /root/.agents/skills/gitcode-event-handler/scripts/gitcode_event_handler.py
```
Expected: 全部退出码 0。

- [ ] **Step 3: 清理临时产物**

Run: `rm -rf /root/.agents/skills/gitcode-event-handler/scripts/__pycache__`
Expected: 无报错。

## 验证清单（对 spec 逐条）

1. ✅ spec ①（测试时创建 BUG issue）→ Task 2/3
2. ✅ spec ②（gitcode_api.sh 三子命令）→ Task 1
3. ✅ spec ③（PR 创建人=skills_bot 豁免）→ Task 2 7.4
4. ✅ spec ④（复用已存在 BUG issue 不新建）→ Task 2 7.4 `find-by-pr`
5. ✅ spec ⑤（重测：仍复现留言/修复关闭）→ Task 2 7.8 + Task 4 派发
6. ✅ spec ⑥（PR update 事件驱动重测 + 幂等守卫）→ Task 4
7. ✅ spec ⑦（错误处理与两份同步）→ Task 5

**已知偏差（已在计划头声明）：** 标签用 `bug`（小写）而非 spec 中的 `BUG`，与代码库 `LABEL_BUG`/部署位一致；标题沿用 spec `测试发现BUG: {skill} (PR #{n})`，与既有过滤规则一致。
