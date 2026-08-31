#!/bin/bash
# gitcode_api.sh — GitCode v5 API 统一封装脚本
# 所有与 GitCode 的交互统一通过本脚本，禁止散落直接 curl 调用
#
# 用法:
#   gitcode_api.sh issue create <owner> <repo> --title "..." --body "..."
#   gitcode_api.sh issue get <owner> <repo> <number>
#   gitcode_api.sh issue list <owner> <repo> [--labels <label>] [--per-page N]
#   gitcode_api.sh issue labels-set <owner> <repo> <number> <label1> [<label2>...]
#   gitcode_api.sh pr get <owner> <repo> <number>
#   gitcode_api.sh pr resolve-id <PR-URL>   # 从 PR URL 解析并返回 PR 全局 id（存库用）
#   gitcode_api.sh pr comment <owner> <repo> <number> --body "..."
#   gitcode_api.sh pr tester-assign <owner> <repo> <number> <username>
#   gitcode_api.sh pr tester-list <owner> <repo> <number>
#   gitcode_api.sh pr assignee-assign <owner> <repo> <number> <username>
#
# Token 读取（统一）: 环境变量 GITCODE_TOKEN > ~/.gitcode_token
#
# 退出码: 0 成功; 1 参数错误; 2 token缺失; 3 API错误/不生效

set -uo pipefail

API_BASE="https://api.gitcode.com/api/v5"

# --- 读取 token ---
get_token() {
    if [[ -n "${GITCODE_TOKEN:-}" ]]; then
        echo "$GITCODE_TOKEN"
        return 0
    fi
    if [[ -f "$HOME/.gitcode_token" ]]; then
        cat "$HOME/.gitcode_token"
        return 0
    fi
    return 1
}

TOKEN=$(get_token)
if [[ -z "$TOKEN" ]]; then
    echo "错误: 未找到 GITCODE_TOKEN（设置环境变量或 $HOME/.gitcode_token）" >&2
    exit 2
fi

# --- 请求辅助函数 ---
# req <method> <path> [data]
# 输出: 状态码写入固定路径文件（父 shell 可读），响应体到 stdout
req() {
    method="$1" path="$2" data="${3:-}"
    args=(-s -o /tmp/gitcode_api_body.$$ -w "%{http_code}" --connect-timeout 15 --max-time 60 \
        -X "$method" -H "PRIVATE-TOKEN: $TOKEN")
    if [[ -n "$data" ]]; then
        args+=(-H "Content-Type: application/json" -d "$data")
    fi
    code=$(curl "${args[@]}" "$API_BASE$path" 2>/dev/null || true)
    body=$(cat /tmp/gitcode_api_body.$$ 2>/dev/null || true)
    rm -f /tmp/gitcode_api_body.$$
    printf '%s' "${code:-000}" > /tmp/gitcode_api_lastcode.$$
    if [[ -n "$body" ]]; then
        printf '%s' "$body"
    fi
}

# 取最近一次请求的状态码（从固定路径读，避免子 shell 变量丢失）
req_code() {
    if [[ -f /tmp/gitcode_api_lastcode.$$ ]]; then
        cat /tmp/gitcode_api_lastcode.$$
    else
        echo "000"
    fi
}

# 校验请求状态码，非 2xx 时退出 3
req_ok() {
    local code="$1" ctx="$2" body="$3"
    if [[ "$code" != "200" && "$code" != "201" ]]; then
        echo "错误: $ctx 失败 (HTTP $code): ${body:0:300}" >&2
        exit 3
    fi
}

# --- 解析命令/sub/owner/repo ---
# pr resolve-id <PR-URL> 只有 3 个参数（URL 存于 owner 位置），单独放行
if [[ $# -lt 4 && ! ( $# -eq 3 && "$1" == "pr" && "$2" == "resolve-id" ) ]]; then
    echo "用法: gitcode_api.sh <command> <sub> <owner> <repo> ..." >&2
    exit 1
fi

CMD="$1"; SUB="$2"; OWNER="${3:-}"; REPO="${4:-}"; shift 4 2>/dev/null || true

case "$CMD" in
    issue)
        case "$SUB" in
            create)
                title="" body="" assignee="" assignee_from_pr="" pr_url=""
                while [[ $# -gt 0 ]]; do
                    case "$1" in
                        --title) title="$2"; shift 2 ;;
                        --body) body="$2"; shift 2 ;;
                        --body-file) body=$(cat "$2"); shift 2 ;;
                        --assignee) assignee="$2"; shift 2 ;;
                        --assignee-from-pr) assignee_from_pr="$2"; shift 2 ;;
                        --pr-url) pr_url="$2"; shift 2 ;;
                        *) echo "未知参数: $1" >&2; exit 1 ;;
                    esac
                done
                if [[ -z "$title" || -z "$body" ]]; then
                    echo "错误: issue create 需要 --title 和 --body/--body-file" >&2; exit 1
                fi
                # 把 PR 链接并入 body 的来源行（若 body 有"- 来源:"则在其后追加，否则在顶部插入）
                if [[ -n "$pr_url" ]]; then
                    body=$(python3 -c "
import sys
body=sys.argv[1]; url=sys.argv[2]
lines=body.splitlines()
found=False
for i,l in enumerate(lines):
    if l.startswith('- 来源'):
        if url not in l:
            lines[i]=l.rstrip()+f'，对应 PR：{url}'
        found=True
        break
if found:
    body='\n'.join(lines)
else:
    body=f'- 来源: {url}\n\n' + body
print(body)
" "$body" "$pr_url")
                fi
                # 自动追溯需求创建人：查 PR 关联的 issue，取非"测试发现BUG"标题的需求 issue 创建人
                if [[ -n "$assignee_from_pr" ]]; then
                    traced=$(req GET "/repos/$OWNER/$REPO/pulls/$assignee_from_pr/issues")
                    if [[ "$(req_code)" != "200" ]]; then
                        echo "  [追溯] 查询 PR #$assignee_from_pr 关联 issue 失败 (HTTP $(req_code))，负责人留空" >&2
                    else
                    traced=$(echo "$traced" | python3 -c "
import json,sys,re
def is_requirement(i):
    t=(i.get('title') or '').strip().lower()
    return not re.search(r'测试\s*发现\s*bug', t)
try:
    d=json.load(sys.stdin)
    items=d if isinstance(d,list) else d.get('data',[])
    for i in items:
        if not is_requirement(i):
            continue
        login=(i.get('user') or {}).get('login')
        if login:
            print(login)
            break
except Exception:
    pass
")
                    if [[ -n "$traced" ]]; then
                        assignee="$traced"
                        echo "  [追溯] PR #$assignee_from_pr 需求创建人 → $assignee" >&2
                    else
                        echo "  [追溯] 未找到需求创建人，负责人留空" >&2
                    fi
                    fi
                fi
                # 组装 payload（含 assignee）
                if [[ -n "$assignee" ]]; then
                    data=$(python3 -c "import json,sys; print(json.dumps({'title': sys.argv[1], 'body': sys.argv[2], 'assignee': sys.argv[3]}, ensure_ascii=False))" "$title" "$body" "$assignee")
                else
                    data=$(python3 -c "import json,sys; print(json.dumps({'title': sys.argv[1], 'body': sys.argv[2]}, ensure_ascii=False))" "$title" "$body")
                fi
                resp=$(req POST "/repos/$OWNER/$REPO/issues" "$data")
                req_ok "$(req_code)" "创建 issue" "$resp"
                echo "$resp"
                echo "$resp" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    assignee = d.get('assignee') or {}
    login = assignee.get('login') if isinstance(assignee, dict) else assignee
    print('number:', d.get('number'))
    print('url:', d.get('html_url'))
    print('assignee:', login)
except Exception as e:
    print(f'解析失败: {e}', file=sys.stderr)
    sys.exit(3)
" 2>/dev/null || exit 3
                ;;
            get)
                number="${1:-}"
                [[ -z "$number" ]] && { echo "错误: issue get 需要 number" >&2; exit 1; }
                resp=$(req GET "/repos/$OWNER/$REPO/issues/$number")
                req_ok "$(req_code)" "获取 issue #$number" "$resp"
                echo "$resp"
                echo
                ;;
            list)
                labels="" per_page="50" page="1"
                while [[ $# -gt 0 ]]; do
                    case "$1" in
                        --labels) labels="$2"; shift 2 ;;
                        --per-page) per_page="$2"; shift 2 ;;
                        --page) page="$2"; shift 2 ;;
                        *) echo "未知参数: $1" >&2; exit 1 ;;
                    esac
                done
                q=""
                if [[ -n "$labels" ]]; then
                    enc_labels=$(python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))" "$labels" 2>/dev/null || echo "$labels")
                    q="?labels=$enc_labels&"
                else
                    q="?"
                fi
                resp=$(req GET "/repos/$OWNER/$REPO/issues${q}per_page=$per_page&page=$page")
                req_ok "$(req_code)" "获取 issue 列表" "$resp"
                echo "$resp"
                echo
                ;;
            labels-set)
                number="${1:-}"; shift
                [[ -z "$number" ]] && { echo "错误: labels-set 需要 number" >&2; exit 1; }
                replace=""
                if [[ "${1:-}" == "--replace" ]]; then replace="1"; shift; fi
                labels=("$@")
                [[ ${#labels[@]} -eq 0 ]] && { echo "错误: labels-set 需要至少一个标签名" >&2; exit 1; }
                # 判断指定标签是否已挂在该 issue 上（用 ?labels= 过滤查询，GitCode issue GET 的 labels 字段不填充）
                label_exists() {
                    local lbl="$1"
                    local resp
                    resp=$(req GET "/repos/$OWNER/$REPO/issues?labels=$lbl&per_page=100")
                    req_ok "$(req_code)" "过滤查询标签 $lbl" "$resp"
                    echo "$resp" | python3 -c "
import json,sys
target='$number'
try:
    d=json.load(sys.stdin)
    items=d if isinstance(d,list) else d.get('data',[])
    found=any(str(i.get('number'))==target for i in items)
    print('yes' if found else 'no')
except Exception:
    print('no')
"
                }
                # --replace 模式：PUT 完整目标集；否则只添加缺失的（用过滤查询判断是否已存在）
                missing=""
                if [[ -z "$replace" ]]; then
                    for lab in "${labels[@]}"; do
                        if [[ "$(label_exists "$lab")" != "yes" ]]; then
                            missing="$missing $lab"
                        fi
                    done
                else
                    missing=" ${labels[*]}"
                fi
                missing=$(echo "$missing" | xargs)
                if [[ -z "$missing" ]]; then
                    echo "✅ 标签已存在，无需重复添加: ${labels[*]}"
                    exit 0
                fi
                # 组装 body（裸数组）
                data=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" $missing)
                method="POST"
                [[ -n "$replace" ]] && method="PUT"
                resp=$(curl -s -o /tmp/gitcode_api_resp.$$ -w "%{http_code}" --connect-timeout 15 --max-time 60 \
                    -X "$method" \
                    -H "PRIVATE-TOKEN: $TOKEN" -H "Content-Type: application/json;charset=UTF-8" \
                    -d "$data" "$API_BASE/repos/$OWNER/$REPO/issues/$number/labels")
                resp_body=$(cat /tmp/gitcode_api_resp.$$ 2>/dev/null); rm -f /tmp/gitcode_api_resp.$$
                if [[ "$resp" == "200" || "$resp" == "201" ]]; then
                    # 验证是否真正生效（用过滤查询确认，GitCode issue GET 的 labels 字段恒空）
                    verified="yes"
                    for lab in "${labels[@]}"; do
                        if [[ "$(label_exists "$lab")" != "yes" ]]; then
                            verified="no"
                            break
                        fi
                    done
                    if [[ "$verified" != "yes" ]]; then
                        echo "⚠ 标签已请求但未全部生效（用 ?labels= 过滤确认，未命中的标签: ${labels[*]}）" >&2
                        exit 3
                    fi
                    echo "✅ 标签已设置: ${labels[*]}"
                else
                    echo "错误: 设置标签失败 (HTTP $resp): $resp_body" >&2
                    exit 3
                fi
                ;;
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
            bugs-by-pr)
                pr_number="${1:-}"
                [[ -z "$pr_number" ]] && { echo "错误: bugs-by-pr 需要 PR 编号" >&2; exit 1; }
                OWNER="$OWNER" REPO="$REPO" PR_NUMBER="$pr_number" TOKEN="$TOKEN" python3 <<'PYEOF'
import json, os, re, sys, urllib.parse, urllib.request
owner, repo, pr = os.environ["OWNER"], os.environ["REPO"], os.environ["PR_NUMBER"]
token = os.environ["TOKEN"]
api = "https://api.gitcode.com/api/v5"
pr_re = re.compile(r"PR\s*#?\s*%s(?![0-9])" % re.escape(str(pr)), re.IGNORECASE)
url_re = re.compile(r"(pull|pulls|merge_requests)/%s\b" % re.escape(str(pr)), re.IGNORECASE)
total = 0
closed = 0
seen = set()
def has_bug_label(it):
    labels = it.get("labels") or []
    names = [l.get("name") if isinstance(l, dict) else str(l) for l in labels]
    return any(str(n).lower() == "bug" for n in names)
def count(it):
    global total, closed
    num = str(it.get("number"))
    if not num or num in seen:
        return
    seen.add(num)
    total += 1
    if str(it.get("state", "")).lower() == "closed":
        closed += 1
def get(path):
    req = urllib.request.Request(f"{api}{path}", headers={"PRIVATE-TOKEN": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())
# 该 PR 直接关联的 issue（可能是需求 issue 或 BUG issue）—— 仅统计带 bug 标签的，避免需求 issue 被误计
try:
    related = get(f"/repos/{owner}/{repo}/pulls/{pr}/issues")
    if isinstance(related, dict):
        related = related.get("data") or []
    for it in related or []:
        title = it.get("title") or ""
        body = it.get("body") or ""
        if (pr_re.search(title) or url_re.search(body)) and has_bug_label(it):
            count(it)
except Exception:
    pass
# 全量 BUG issue（labels=bug，open + closed）
for state in ("open", "closed"):
    page = 1
    while True:
        qs = urllib.parse.urlencode({"state": state, "labels": "bug", "per_page": 100, "page": page})
        try:
            items = get(f"/repos/{owner}/{repo}/issues?{qs}")
        except Exception as e:
            print('  [bugs-by-pr] 查询失败: %s' % e, file=sys.stderr)
            sys.exit(3)
        if not items:
            break
        if isinstance(items, dict):
            items = items.get("data") or []
        for it in items or []:
            title = it.get("title") or ""
            body = it.get("body") or ""
            if pr_re.search(title) or url_re.search(body):
                count(it)
        if len(items) < 100:
            break
        page += 1
print(json.dumps({"total": total, "closed": closed}))
PYEOF
                ;;
            get-by-pr)
                pr_number="${1:-}"
                [[ -z "$pr_number" ]] && { echo "错误: get-by-pr 需要 PR 编号" >&2; exit 1; }
                body=$(req GET "/repos/$OWNER/$REPO/pulls/$pr_number/issues")
                req_ok "$(req_code)" "查询 PR #$pr_number 关联 issue" "$body"
                echo "$body" | python3 -c "
import json,sys,re
def is_requirement(i):
    t=(i.get('title') or '').strip().lower()
    return not re.search(r'测试\s*发现\s*bug', t)
try:
    d=json.load(sys.stdin)
    items=d if isinstance(d,list) else d.get('data',[])
    for i in items:
        if not is_requirement(i):
            continue
        out={'id':str(i.get('id') or ''),'number':str(i.get('number') or ''),
             'title':i.get('title') or '','html_url':i.get('html_url') or '',
             'user':(i.get('user') or {}).get('login') or ''}
        print(json.dumps(out,ensure_ascii=False))
        break
    else:
        print('{}')
except Exception:
    print('{}')
"
                ;;
            close-by-pr)
                pr_number="${1:-}"
                [[ -z "$pr_number" ]] && { echo "错误: close-by-pr 需要 PR 编号" >&2; exit 1; }
                # 查 PR 关联的 issue，排除"测试发现BUG"评测产物，只关闭需求 issue
                targets=$(req GET "/repos/$OWNER/$REPO/pulls/$pr_number/issues")
                req_ok "$(req_code)" "查询 PR #$pr_number 关联 issue" "$targets"
                targets=$(echo "$targets" | python3 -c "
import json,sys,re
def is_requirement(i):
    t=(i.get('title') or '').strip().lower()
    return not re.search(r'测试\s*发现\s*bug', t)
try:
    d=json.load(sys.stdin)
    items=d if isinstance(d,list) else d.get('data',[])
    nums=[]
    for i in items:
        if not is_requirement(i):
            continue
        n=i.get('number')
        if n is not None:
            nums.append(str(n))
    print(' '.join(nums))
except Exception:
    pass
")
                if [[ -z "$targets" ]]; then
                    echo "  [关闭] PR #$pr_number 未找到需关闭的需求 issue（可能无关联或均为评测产物）" >&2
                    exit 0
                fi
                closed=0
                for n in $targets; do
                    rc=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 15 --max-time 60 -X PATCH \
                        -H "PRIVATE-TOKEN: $TOKEN" -H "Content-Type: application/json" \
                        -d '{"state":"close"}' "$API_BASE/repos/$OWNER/$REPO/issues/$n")
                    if [[ "$rc" == "200" ]]; then
                        echo "  [关闭] issue #$n 已关闭" >&2
                        closed=$((closed + 1))
                    else
                        echo "  [关闭] issue #$n 关闭失败 (HTTP $rc)" >&2
                    fi
                done
                total_targets=$(echo "$targets" | wc -w | tr -d ' ')
                if [[ "$closed" -lt "$total_targets" ]]; then
                    echo "错误: 仅关闭 $closed/$total_targets 个需求 issue（PR #$pr_number）" >&2
                    exit 3
                fi
                echo "✅ 已关闭 $closed 个需求 issue（PR #$pr_number）"
                ;;
            url-by-pr)
                pr_number="${1:-}"
                [[ -z "$pr_number" ]] && { echo "错误: url-by-pr 需要 PR 编号" >&2; exit 1; }
                # 查 PR 关联的 issue，排除"测试发现BUG"评测产物，取需求 issue 的 html_url
                url=$(req GET "/repos/$OWNER/$REPO/pulls/$pr_number/issues")
                req_ok "$(req_code)" "查询 PR #$pr_number 关联 issue" "$url"
                url=$(echo "$url" | python3 -c "
import json,sys,re
def is_requirement(i):
    t=(i.get('title') or '').strip().lower()
    return not re.search(r'测试\s*发现\s*bug', t)
try:
    d=json.load(sys.stdin)
    items=d if isinstance(d,list) else d.get('data',[])
    for i in items:
        if not is_requirement(i):
            continue
        u=i.get('html_url') or i.get('url') or ''
        if u:
            print(u)
            break
except Exception:
    pass
")
                if [[ -n "$url" ]]; then
                    echo "$url"
                    exit 0
                else
                    echo "  [url-by-pr] PR #$pr_number 未找到需求 issue 链接" >&2
                    exit 0
                fi
                ;;
            *)
                echo "未知 issue 子命令: $SUB" >&2; exit 1 ;;
        esac
        ;;
    pr)
        case "$SUB" in
            resolve-id)
                # 从 PR URL 解析 owner/repo/编号并返回 PR 全局 id（存库用）
                # 用法: gitcode_api.sh pr resolve-id <PR-URL>
                #   URL 支持 https://gitcode.com/{owner}/{repo}/pull|pulls|merge_requests/{n}
                pr_url="$OWNER"  # URL 存于 owner 位置
                parsed=$(python3 - "$pr_url" <<'PYEOF'
import re, sys
url = sys.argv[1].strip()
m = re.search(r"gitcode\.com/([^/]+)/([^/]+)/(?:pull|pulls|merge_requests)/(\d+)", url)
if not m:
    m = re.match(r"^/?([^/]+)/([^/]+)/(?:pull|pulls|merge_requests)/(\d+)$", url)
print(f"{m.group(1)} {m.group(2)} {m.group(3)}" if m else "")
PYEOF
)
                read -r P_OWNER P_REPO P_NUM <<< "$parsed"
                if [[ -z "$P_NUM" ]]; then
                    echo "错误: 无法从 URL 解析 owner/repo/PR编号: $pr_url" >&2
                    exit 1
                fi
                resp=$(req GET "/repos/$P_OWNER/$P_REPO/pulls/$P_NUM")
                req_ok "$(req_code)" "获取 PR #$P_NUM id" "$resp"
                echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('id',''))
"
                ;;
            get)
                number="${1:-}"; [[ -z "$number" ]] && { echo "错误: pr get 需要 number" >&2; exit 1; }
                resp=$(req GET "/repos/$OWNER/$REPO/pulls/$number")
                req_ok "$(req_code)" "获取 PR #$number" "$resp"
                echo "$resp"; echo ;;
             comment)
                number="${1:-}"; shift
                [[ -z "$number" ]] && { echo "错误: pr comment 需要 number" >&2; exit 1; }
                body=""
                prepend=""
                while [[ $# -gt 0 ]]; do
                    case "$1" in
                        --body) body="$2"; shift 2 ;;
                        --body-file) body=$(cat "$2"); shift 2 ;;
                        --prepend) prepend="$2"; shift 2 ;;
                        *) echo "未知参数: $1" >&2; exit 1 ;;
                    esac
                done
                [[ -z "$body" ]] && { echo "错误: pr comment 需要 --body" >&2; exit 1; }
                if [[ -n "$prepend" ]]; then
                    body="${prepend}"$'\n\n'"${body}"
                fi
                data=$(python3 -c "import json,sys; print(json.dumps({'body': sys.argv[1]}, ensure_ascii=False))" "$body")
                resp=$(req POST "/repos/$OWNER/$REPO/pulls/$number/comments" "$data")
                req_ok "$(req_code)" "提交 PR #$number 评论" "$resp"
                echo "$resp"
                echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print('comment id:', d.get('id'))" 2>/dev/null || exit 3
                ;;
            tester-assign)
                number="${1:-}"; username="${2:-}"
                [[ -z "$number" || -z "$username" ]] && { echo "错误: tester-assign 需要 number 和 username" >&2; exit 1; }
                # 文档第29节: POST /pulls/{number}/testers, body 字段为 testers（逗号分隔 username）
                data=$(python3 -c "import json,sys; print(json.dumps({'testers': sys.argv[1]}))" "$username")
                resp=$(curl -s -o /tmp/gitcode_api_resp.$$ -w "%{http_code}" --connect-timeout 15 --max-time 60 -X POST \
                    -H "PRIVATE-TOKEN: $TOKEN" -H "Content-Type: application/json;charset=UTF-8" \
                    -d "$data" "$API_BASE/repos/$OWNER/$REPO/pulls/$number/testers")
                resp_body=$(cat /tmp/gitcode_api_resp.$$ 2>/dev/null); rm -f /tmp/gitcode_api_resp.$$
                if [[ "$resp" == "200" || "$resp" == "201" ]]; then
                    # 验证是否真正生效（PR testers 字段，精确匹配）
                    cur=$(req GET "/repos/$OWNER/$REPO/pulls/$number")
                    req_ok "$(req_code)" "验证 PR #$number testers" "$cur"
                    verified=$(CUR="$cur" USER="$username" python3 -c "
import json,sys,os
try:
    d=json.loads(os.environ['CUR'])
    testers=[u.get('login','') for u in (d.get('testers') or [])]
except Exception:
    testers=[]
print('ok' if os.environ['USER'] in testers else 'fail')
")
                    if [[ "$verified" != "ok" ]]; then
                        echo "⚠ 测试人已请求但未生效（HTTP 200 但 testers 未更新，可能该用户无测试人指派资格）" >&2
                        exit 3
                    fi
                    echo "✅ 测试人已指派: $username"
                else
                    echo "错误: 指派测试人失败 (HTTP $resp): $resp_body" >&2
                    exit 3
                fi
                ;;
            tester-list)
                number="${1:-}"; [[ -z "$number" ]] && { echo "错误: tester-list 需要 number" >&2; exit 1; }
                resp=$(req GET "/repos/$OWNER/$REPO/pulls/$number/option/approval/testers")
                req_ok "$(req_code)" "获取 PR #$number 测试人列表" "$resp"
                echo "$resp"; echo ;;
            assignee-assign)
                number="${1:-}"; username="${2:-}"
                [[ -z "$number" || -z "$username" ]] && { echo "错误: assignee-assign 需要 number 和 username" >&2; exit 1; }
                data=$(python3 -c "import json,sys; print(json.dumps({'assignees': sys.argv[1]}))" "$username")
                resp=$(curl -s -o /tmp/gitcode_api_resp.$$ -w "%{http_code}" --connect-timeout 15 --max-time 60 -X POST \
                    -H "PRIVATE-TOKEN: $TOKEN" -H "Content-Type: application/json" \
                    -d "$data" "$API_BASE/repos/$OWNER/$REPO/pulls/$number/assignees")
                resp_body=$(cat /tmp/gitcode_api_resp.$$ 2>/dev/null); rm -f /tmp/gitcode_api_resp.$$
                if [[ "$resp" == "200" || "$resp" == "201" ]]; then
                    cur=$(req GET "/repos/$OWNER/$REPO/pulls/$number")
                    req_ok "$(req_code)" "验证 PR #$number assignees" "$cur"
                    verified=$(CUR="$cur" USER="$username" python3 -c "
import json,sys,os
try:
    d=json.loads(os.environ['CUR'])
    assignees=[u.get('login','') for u in (d.get('assignees') or [])]
except Exception:
    assignees=[]
print('ok' if os.environ['USER'] in assignees else 'fail')
")
                    if [[ "$verified" != "ok" ]]; then
                        echo "⚠ 审查人已请求但未生效（HTTP 200 但 assignees 未更新，平台限制）" >&2
                        exit 3
                    fi
                    echo "✅ 审查人已指派: $username"
                else
                    echo "错误: 指派审查人失败 (HTTP $resp): $resp_body" >&2
                    exit 3
                fi
                ;;
            *)
                echo "未知 pr 子命令: $SUB" >&2; exit 1 ;;
        esac
        ;;
    *)
        echo "未知命令: $CMD（支持 issue / pr）" >&2; exit 1 ;;
esac
