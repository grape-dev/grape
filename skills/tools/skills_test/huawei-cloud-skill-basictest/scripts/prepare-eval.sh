#!/bin/bash
# prepare-eval.sh — 准备+分析+用例生成 整合脚本（替代模型 ~10 分钟的逐条思考）
#
# 从被测 skill 的 SKILL.md 自动提取命令与触发词，生成:
#   1) 正反用例集.md（P 用例 = 每个命令一条正例；N 用例 = 标准越界/输入校验反例）
#   2) cases-commands.json（每条用例的命令 + 判定规则，供 run-eval-batch.sh 消费）
#
# 目的：把"读 skill → 分析能力边界 → 逐条生成 P/N 用例"的 ~69 次模型推理，
#   压缩为"跑一次本脚本 + 模型审阅修正（1-2 次）"。
#
# 用法:
#   bash prepare-eval.sh --skill-dir <skill目录> [--skill-name <名>] \
#       [--output-dir <输出目录>] [--resource-check <只读命令>]
#
# 输出:
#   <output-dir>/正反用例集.md
#   <output-dir>/cases-commands.json
#
# 退出码: 0 成功; 1 参数错误; 2 SKILL.md 缺失/未提取到命令

set -uo pipefail

SKILL_DIR=""
SKILL_NAME=""
OUTPUT_DIR=""
RESOURCE_CHECK=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skill-dir)     SKILL_DIR="${2:-}"; shift 2 ;;
        --skill-name)    SKILL_NAME="${2:-}"; shift 2 ;;
        --output-dir)    OUTPUT_DIR="${2:-}"; shift 2 ;;
        --resource-check) RESOURCE_CHECK="${2:-}"; shift 2 ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

[[ -n "$SKILL_DIR" ]] || { echo "错误: 需要 --skill-dir" >&2; exit 1; }
SM="$SKILL_DIR/SKILL.md"
[[ -f "$SM" ]] || { echo "错误: SKILL.md 不存在: $SM" >&2; exit 1; }
[[ -n "$OUTPUT_DIR" ]] || OUTPUT_DIR="$SKILL_DIR/case-run/$(date +%Y-%m-%d)"
mkdir -p "$OUTPUT_DIR"

# 解析 SKILL.md：名称/描述/触发词/命令
SKILL_DIR="$SKILL_DIR" SKILL_NAME="$SKILL_NAME" RESOURCE_CHECK="$RESOURCE_CHECK" OUTPUT_DIR="$OUTPUT_DIR" TIMING_LOG="${EVAL_TIMING_LOG:-/tmp/eval_timing.log}" python3 <<'PY'
import json, os, re, subprocess, sys, time, datetime

t0 = time.perf_counter()

sm = os.path.join(os.environ["SKILL_DIR"], "SKILL.md")
text = open(sm, encoding="utf-8").read()

# 1. skill 名称
name = os.environ.get("SKILL_NAME") or ""
if not name:
    m = re.search(r"^name:\s*(\S+)", text, re.M)
    name = m.group(1) if m else ""
if not name:
    m = re.search(r"^#\s+(huawei-cloud-\S+)", text, re.M)
    name = m.group(1) if m else ""
if not name:
    name = os.path.basename(os.environ["SKILL_DIR"])

# 2. 描述/触发词
desc = ""
m = re.search(r"^description:\s*\|?\s*\n?(.*?)(?=\n\w+:|$)", text, re.M | re.S)
if m:
    desc = " ".join(m.group(1).split())[:300]
trigger = ""
for kw in ("触发词", "Triggers", "trigger", "trigger_words"):
    m = re.search(r"^%s[:\s]*\|?\s*(.+)$" % re.escape(kw), text, re.M)
    if m:
        trigger = m.group(1).strip()[:200]
        break
if not trigger:
    m = re.search(r'Triggers?\s*include:\s*"([^"]+)"', text)
    trigger = m.group(1) if m else ""

# 3. 提取命令（代码块内以 hcloud/python3/curl/bash 开头，或含 --cli-/API 调用特征）
raw_cmds = []
in_code = False
for line in text.splitlines():
    if line.strip().startswith("```"):
        in_code = not in_code
        continue
    s = line.strip()
    if re.match(r"^(hcloud |python3 |curl |bash |obsutil |export |source )", s):
        # 去掉尾部注释
        s = re.sub(r"\s*#.*$", "", s).strip()
        # 过滤纯环境设置
        if re.match(r"^(export|source)\s", s):
            continue
        if s and len(s) > 5:
            raw_cmds.append(s)
    elif in_code and re.search(r"(--cli-region|ListInstances|list_|\.list\(|client\.)", s) and not s.startswith(("#", "//", "|")):
        s = re.sub(r"\s*#.*$", "", s).strip()
        if s and len(s) > 5:
            raw_cmds.append(s)

# 去重保序
seen = set(); cmds = []
for c in raw_cmds:
    if c not in seen:
        seen.add(c); cmds.append(c)

if not cmds:
    print("错误: 未从 SKILL.md 提取到任何命令（检查命令是否以 hcloud/python3/curl 开头或在代码块内）", file=sys.stderr)
    sys.exit(2)

# 4. 可选：资源探测（跑第一条只读命令并缓存，供预期行为/commands 引用）
resource_info = ""
if os.environ.get("RESOURCE_CHECK"):
    try:
        r = subprocess.run(["bash", "-c", os.environ["RESOURCE_CHECK"]],
                           capture_output=True, text=True, timeout=60)
        resource_info = (r.stdout or r.stderr or "").strip()[:800]
    except Exception:
        resource_info = ""

# 5. 生成正例：每个命令一条 P 用例
pos = []
for i, c in enumerate(cmds, 1):
    cid = f"P{i}"
    verb = "查询" if re.search(r"List|list|Get|get|查询|查看", c) else "执行"
    desc_line = c[:70].replace("|", " ")
    pos.append({
        "id": cid, "title": f"{verb}: {desc_line}",
        "input": f"{verb}当前账号的{name.replace('huawei-cloud-','')}资源列表" if "List" in c or "list" in c else f"执行{desc_line}",
        "trigger": trigger or f"{name}",
        "expected": f"命令执行成功（退出码 0）并输出结果；{('当前资源: ' + resource_info[:200]) if resource_info else '按 skill 规范输出'}",
        "cmd": c, "expect": "rc0",
    })

# 6. 标准反例（越界/输入校验）
neg_tpls = [
    ("创建/删除类越界操作", f"请创建（删除）{name}相关资源", "错误: 该操作为越界操作，skill 仅支持查询/排障，应明确拒绝", "error"),
    ("无效 region 参数", f"用无效 region 查询{name}", "错误: 无效 region 参数，应明确报错", "error"),
    ("不存在的资源 ID", f"查询一个不存在的资源", "错误: 资源不存在，应明确报错", "error"),
    ("缺少凭证", f"无 AK/SK 查询{name}", "错误: 缺少凭证，应明确提示", "error"),
]
neg = []
base_cmd = cmds[0] if cmds else "true"
for i, (title, inp, expected, expect) in enumerate(neg_tpls, 1):
    cid = f"N{i}"
    variant = base_cmd
    if "region" in title:
        variant = re.sub(r"--cli-region=\S+", "--cli-region=invalid-region-xyz", base_cmd)
    elif "不存在" in title:
        variant = re.sub(r"(--instance_id|--id|--volume_id)=\S+", r"\1=00000000-0000-0000-0000-000000000000", base_cmd)
    neg.append({"id": cid, "title": title, "input": inp, "trigger": "（反例）",
                "expected": expected, "cmd": variant, "expect": expect})

cases = pos + neg

# 7. 写 正反用例集.md
md = [f"# 正反用例集: {name}", "", f"- 被测 skill: {name}", f"- 评测日期: {__import__('datetime').date.today().isoformat()}",
      f"- 能力边界: 由 SKILL.md 提取（{len(cmds)} 条命令）", ""]
def section(md, cases, label):
    md.append(f"## {label}")
    for c in cases:
        md.extend(["", f"### {c['id']}: {c['title']}", "",
               f"- 用户原话: {c['input']}", f"- 命中触发词: {c['trigger'] or '（自动提取）'}",
               f"- 预期行为: {c['expected']}", "- 触发方式: 中文/英文"])
    md.append("")
section(md, pos, "正例 (Positive Cases)")
section(md, neg, "反例 (Negative Cases)")

# 8. 写 cases-commands.json
json.dump({c["id"]: {"cmd": c["cmd"], "expect": c["expect"]} for c in cases},
          open(os.path.join(os.environ.get("OUTPUT_DIR", "."), "cases-commands.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

mdpath = os.path.join(os.environ.get("OUTPUT_DIR", "."), "正反用例集.md")
open(mdpath, "w", encoding="utf-8").write("\n".join(md))
print(f"✅ 用例集: {mdpath}（{len(cases)} 条: 正例 {len(pos)} / 反例 {len(neg)}）")
print(f"✅ 命令映射: {os.path.join(os.environ.get('OUTPUT_DIR','.'), 'cases-commands.json')}")
print(f"   提取命令 {len(cmds)} 条，示例: {cmds[0][:80]}")
if resource_info:
    print(f"   资源探测缓存: {resource_info[:100]}")

# 耗时埋点
timing = open(os.environ.get("TIMING_LOG", "/tmp/eval_timing.log"), "a", encoding="utf-8")
timing.write(f"{datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}|{name}|prepare-eval|end|用例集+commands生成|{(time.perf_counter()-t0)*1000:.0f}\n")
timing.close()
PY
exit $?