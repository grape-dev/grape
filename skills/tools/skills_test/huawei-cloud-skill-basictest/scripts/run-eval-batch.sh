#!/bin/bash
# run-eval-batch.sh — 批量执行正反用例集（一次调用跑完全部用例）
#
# 目的：把"逐条 register→执行→complete"的模型多轮驱动，压缩为
#   1) 生成 commands JSON（1 次模型调用）
#   2) 运行本脚本（1 次模型调用）
# 从而大幅减少实测验证阶段的模型推理轮次。
#
# 用法:
#   bash run-eval-batch.sh --skill <skill名> \
#       --cases <正反用例集.md> \
#       --commands <cases-commands.json> \
#       [--resource-tag <tag>] [--archive-dir <归档基路径>] [--interactive]
#
# commands JSON 格式（模型生成一次）:
#   {
#     "P1": {"cmd": "bash -c '...'",          "expect": "rc0"},
#     "P2": {"cmd": "python3 - <<'PY' ...",   "expect": "contains:网格"},
#     "N1": {"cmd": "...",                    "expect": "error"},
#     ...
#   }
#   expect 取值:
#     rc0           命令退出码 0 → pass，否则 fail
#     error         命令退出码非 0 或输出含错误关键字 → pass（反例期望优雅报错），否则 fail
#     contains:TEXT 输出包含 TEXT → pass，否则 fail
#     not_contains:TEXT 输出不包含 TEXT → pass，否则 fail
#   （未在 JSON 中登记的用例 → warn，注明"未提供执行命令"）
#
# 退出码: 0 全部跑完; 1 参数错误; 2 用例集/命令文件缺失

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$SCRIPT_DIR/skill-eval-gate.sh"
STATE_DIR="$HOME/.skill-eval-gate"

SKILL=""
CASES_FILE=""
COMMANDS_FILE=""
RESOURCE_TAG=""
ARCHIVE_DIR=""
AUTO_APPROVE="1"

usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skill)        SKILL="${2:-}"; shift 2 ;;
        --cases)        CASES_FILE="${2:-}"; shift 2 ;;
        --commands)     COMMANDS_FILE="${2:-}"; shift 2 ;;
        --resource-tag) RESOURCE_TAG="${2:-}"; shift 2 ;;
        --archive-dir)  ARCHIVE_DIR="${2:-}"; shift 2 ;;
        --interactive)  AUTO_APPROVE=""; shift ;;
        -h|--help)      usage ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

[[ -n "$SKILL" && -n "$CASES_FILE" && -n "$COMMANDS_FILE" ]] || { echo "错误: 需要 --skill / --cases / --commands" >&2; exit 1; }
[[ -f "$CASES_FILE" ]]    || { echo "错误: 用例集不存在: $CASES_FILE" >&2; exit 1; }
[[ -f "$COMMANDS_FILE" ]] || { echo "错误: 命令文件不存在: $COMMANDS_FILE" >&2; exit 1; }
[[ -x "$GATE" ]]          || { echo "错误: 缺少门禁脚本: $GATE" >&2; exit 1; }

# 解析用例集 + 命令文件，输出每条的字段（Python，argv 传值防注入）
CASES_JSON=$(SKILL="$SKILL" CASES_FILE="$CASES_FILE" COMMANDS_FILE="$COMMANDS_FILE" python3 <<'PY'
import json, os, re, sys
cases_file = os.environ["CASES_FILE"]
commands_file = os.environ["COMMANDS_FILE"]
try:
    commands = json.load(open(commands_file, encoding="utf-8"))
except Exception as e:
    print(f"错误: commands JSON 解析失败: {e}", file=sys.stderr); sys.exit(1)

text = open(cases_file, encoding="utf-8").read()
cases = []
for m in re.finditer(r"^###\s+([PN]\d+):\s*(.+)$", text, re.M):
    cid, title = m.group(1), m.group(2).strip()
    seg = text[m.end():]
    nxt = re.search(r"^###\s+[PN]\d+:", seg, re.M)
    body = seg[:nxt.start()] if nxt else seg
    def field(label):
        mm = re.search(r"^- %s:\s*(.+)$" % re.escape(label), body, re.M)
        return mm.group(1).strip() if mm else ""
    cases.append({
        "id": cid, "title": title,
        "input": field("用户原话"), "trigger": field("命中触发词"),
        "expected": field("预期行为"), "type": "positive" if cid.startswith("P") else "negative",
        "cmd": (commands.get(cid) or {}).get("cmd", ""),
        "expect": (commands.get(cid) or {}).get("expect", ""),
    })
print(json.dumps(cases, ensure_ascii=False))
PY
) || exit 1

TOTAL=$(echo "$CASES_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
[[ "$TOTAL" -gt 0 ]] || { echo "错误: 用例集未解析到任何用例（需 ### P{n}: / ### N{n}: 段落）" >&2; exit 1; }

echo "=========================================================="
echo " 批量执行: ${SKILL}  共 ${TOTAL} 条用例（$([ -n "$AUTO_APPROVE" ] && echo auto-approve || echo interactive)）"
echo "=========================================================="

T_BATCH_START=$(date +%s%N)

ARCHIVE_DAY=""
if [[ -n "$ARCHIVE_DIR" ]]; then
    ARCHIVE_DAY="$ARCHIVE_DIR/case-run/$(date +%Y-%m-%d)"
    mkdir -p "$ARCHIVE_DAY"
fi

SUMMARY=""
PASS=0; WARN=0; FAIL=0; SKIP=0
declare -a FAILED_CASES=()

# 逐条：register → 执行 → 判定 → complete → 归档（顺序门禁在循环内满足）
CASES_JSON="$CASES_JSON" SKILL="$SKILL" RESOURCE_TAG="$RESOURCE_TAG" GATE="$GATE" AUTO_APPROVE="$AUTO_APPROVE" ARCHIVE_DAY="$ARCHIVE_DAY" TIMING_LOG="${EVAL_TIMING_LOG:-/tmp/eval_timing.log}" python3 <<'PY'
import json, os, re, subprocess, sys, datetime, time

cases = json.loads(os.environ["CASES_JSON"])
skill = os.environ["SKILL"]
gate = os.environ["GATE"]
tag = os.environ.get("RESOURCE_TAG", "")
auto = os.environ.get("AUTO_APPROVE", "1")
archive_day = os.environ.get("ARCHIVE_DAY", "")
today = datetime.date.today().isoformat()
ERR_HINTS = ["error", "错误", "Error", "fail", "不支持", "unsupported", "不存在", "无权限", "Unauthorized", "400", "401", "403", "404"]
PASS = 0; WARN = 0; FAIL = 0; SKIP = 0
FAILED_CASES = []
SUMMARY = ""

def run(cmd, timeout=60):
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "")[:2000] + ("\n" + r.stderr[:500] if r.stderr else "")

def _log_timing(case, stage, detail, dur_ms):
    timing = open(os.environ.get("TIMING_LOG", "/tmp/eval_timing.log"), "a", encoding="utf-8")
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    timing.write(f"{ts}|{skill}|run-eval-batch|{stage}|{case}:{detail}|{dur_ms:.0f}\n")
    timing.close()

def judge(expect, rc, output):
    expect = (expect or "").strip()
    if expect == "rc0":
        return "pass" if rc == 0 else "fail", f"rc={rc}"
    if expect == "error":
        low = output.lower()
        if rc != 0 or any(h in output for h in ERR_HINTS):
            return "pass", f"期望报错已出现 rc={rc}"
        return "fail", f"期望报错但 rc=0 且无错误信息"
    if expect.startswith("contains:"):
        k = expect[len("contains:"):]
        return ("pass" if k in output else "fail"), f"含「{k}」" if k in output else f"缺「{k}」"
    if expect.startswith("not_contains:"):
        k = expect[len("not_contains:"):]
        return ("pass" if k not in output else "fail"), f"未含「{k}」" if k not in output else f"含「{k}」"
    return "warn", f"expect 未定义({expect or '空'})，仅记录"

def archive(case, result, output):
    if not archive_day:
        return
    status_map = {"pass": "通过", "warn": "告警", "fail": "失败"}
    safe_title = re.sub(r'[\\/:*?"<>| ]+', "_", case["title"])[:40]
    d = os.path.join(archive_day, f"{case['id']}_{safe_title}_{status_map[result]}")
    os.makedirs(d, exist_ok=True)
    md = f"""# 用例说明: {case['id']}

- 用例类型: {case['type']}
- 用例编号: {case['id']}
- 测试日期: {today}
- 状态: {status_map[result]}

## 用户原话

{case['input'] or '（空）'}

## 触发验证

{case['trigger'] or '（未提供）'}

## 预期行为

{case['expected'] or '（未提供）'}

## 实际行为

```
{output[:1500] or '（无输出）'}
```

## 报错质量

{('良好' if result == 'pass' else '一般，见测试结果')}

## 测试结果

{status_map[result]}
"""
    with open(os.path.join(d, "用例说明.md"), "w", encoding="utf-8") as fh:
        fh.write(md)

for i, c in enumerate(cases):
    cid = c["id"]
    print(f"\n==> [{i+1}/{len(cases)}] {cid} ({c['type']}) {c['title']}")
    # register
    reg = ["bash", gate, "--register", "--skill", skill, "--case", cid,
           "--input", c["input"] or cid, "--trigger", c.get("trigger") or "",
           "--type", "positive" if c["type"] == "positive" else "negative",
           "--resource-tag", tag]
    if auto:
        reg.append("--auto-approve")
    t0 = time.perf_counter()
    r1 = subprocess.run(reg, capture_output=True, text=True)
    t_reg = (time.perf_counter() - t0) * 1000
    if r1.returncode != 0:
        print(f"    注册失败: {(r1.stderr or r1.stdout)[:300]}")
        SUMMARY = SUMMARY + f"{cid}\tregister_error\t-\t-\n"
        SKIP += 1
        _log_timing(cid, "register", f"register_error rc={r1.returncode}", t_reg)
        continue
    # execute
    if not c["cmd"]:
        result, why = "warn", "未提供执行命令(commands JSON 缺该用例)"
        output = ""
        _log_timing(cid, "execute", f"warn:no-cmd", 0)
    else:
        try:
            t0 = time.perf_counter()
            rc, output = run(c["cmd"])
            t_exec = (time.perf_counter() - t0) * 1000
            result, why = judge(c["expect"], rc, output)
            _log_timing(cid, "execute", f"rc={rc} {result} ({why})", t_exec)
        except subprocess.TimeoutExpired:
            result, why, output = "fail", "命令超时(>60s)", ""
            _log_timing(cid, "execute", f"fail:timeout>60s", 60000)
        except Exception as e:
            result, why, output = "fail", f"执行异常: {e}", ""
            _log_timing(cid, "execute", f"fail:exception {e}", 0)
    # complete
    t0 = time.perf_counter()
    subprocess.run(["bash", gate, "--complete", "--skill", skill, "--case", cid, "--result", result],
                   capture_output=True, text=True)
    _log_timing(cid, "complete", result, (time.perf_counter() - t0) * 1000)
    # archive
    archive(c, result, output)
    if result == "pass": PASS += 1
    elif result == "warn": WARN += 1
    else:
        FAIL += 1; FAILED_CASES.append(f"{cid}({why})")
    print(f"    结果: {result} | {why}")
    if output.strip():
        print(f"    输出片段: {output.strip()[:200]}")

print("\n" + "=" * 60)
print(f" 汇总: 总 {len(cases)} | pass {PASS} | warn {WARN} | fail {FAIL} | skip {SKIP}")
if FAILED_CASES:
    print(" 失败用例: " + ", ".join(FAILED_CASES))
print("=" * 60)
PY

# 批量整体耗时埋点
T_BATCH_END=$(date +%s%N)
TOTAL_MS=$(( (T_BATCH_END - T_BATCH_START) / 1000000 ))
TIMING_LOG="${EVAL_TIMING_LOG:-/tmp/eval_timing.log}"
mkdir -p "$(dirname "$TIMING_LOG")"
printf '%s|%s|%s|%s|%s|%s\n' \
    "$(date +%Y-%m-%dT%H:%M:%S.%3N)" "$SKILL" "run-eval-batch" "end" "批量执行全流程" "$TOTAL_MS" \
    >> "$TIMING_LOG"

# 审计 + 清理门禁状态
bash "$GATE" --audit --skill "$SKILL" >/dev/null 2>&1 || true
bash "$GATE" --cleanup --skill "$SKILL" >/dev/null 2>&1 || true
echo ""
echo "✅ 批量执行完成（门禁记录已清理）"
exit 0
