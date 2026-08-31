#!/usr/bin/env python3
"""insert_test_detail.py — 评测结果写入 hd_skill_test_detail 表

评测收尾时调用，将本次评测结果写入数据库。

用法:
  python3 insert_test_detail.py \
    --skill <skill_name> \
    --case-run <日期目录> \
    --report-url <OBS回源URL> \
  --pr <PR编号> \
  --owner <负责人> \
  [--bug-issue-url <BUG issue URL>]   # 有 BUG 且已创建时优先记录 BUG issue URL，否则回退需求 issue URL

字段来源:
  skill_name       → 被测 skill 名
  cloud_service    → 从 skill 名提取服务代码（huawei-cloud-{svc}-xxx → svc）
  cloud_service_name → skill-service-map.md 映射（缺省用服务代码大写）
  test_time        → 当前时间
  passed           → 由问题单解决情况决定：resolved_count == total_count → 1，否则 0（无 BUG 时 0==0 → 1）
  issue_id/issue_name → 优先反推 PR 关联的需求 issue（issue get-by-pr）；反推不到则用 PR 全局 id（pr resolve-id，格式 pr:{PR全局id}）
  total_count/resolved_count → 从问题单统计 BUG 数量 / 已修复数量（问题单解析不到时兜底 gitcode_api.sh issue bugs-by-pr）
  issue_url        → 优先 --bug-issue-url（BUG issue 的 html_url）；否则回退需求 issue 的 html_url
  owner            → 有需求 issue 用 issue 创建人，无 issue 用 PR 创建人（--owner 兜底）
  report_url       → OBS 回源下载 URL

UPSERT 以 issue_id 为唯一键（issue_id 有唯一索引），重复执行同一 PR 会更新该行。
"""

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import pymysql
except ImportError:
    print("错误: 缺少 pymysql，请安装: pip install pymysql", file=sys.stderr)
    sys.exit(1)

def load_mysql_config():
    """从 ~/mysql.txt 读取数据库配置（支持 冒号/全角冒号/空格 分隔）"""
    config = {}
    mysql_file = os.path.join(os.path.expanduser("~"), "mysql.txt")
    if os.path.exists(mysql_file):
        try:
            with open(mysql_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    # 尝试多种分隔符
                    key = val = None
                    for sep in ("：", ":", " "):
                        if sep in line:
                            k, _, v = line.partition(sep)
                            key, val = k.strip(), v.strip()
                            break
                    if not key or not val:
                        continue
                    if "Ip" in key or "IP" in key or "地址" in key:
                        config["host"] = val
                    elif "用户" in key:
                        config["user"] = val
                    elif "密码" in key:
                        config["password"] = val
                    elif "数据库名" in key or "库名" in key:
                        config["name"] = val
        except Exception:
            pass
    return config


# 数据库配置：环境变量优先（TEST_DB_*），否则从 ~/mysql.txt 读取，无硬编码默认库名
_mysql_cfg = load_mysql_config()
DB_HOST = os.environ.get("TEST_DB_HOST") or _mysql_cfg.get("host") or "127.0.0.1"
DB_USER = os.environ.get("TEST_DB_USER") or _mysql_cfg.get("user") or "root"
DB_PASS = os.environ.get("TEST_DB_PASS") or _mysql_cfg.get("password") or ""
DB_NAME = os.environ.get("TEST_DB_NAME") or _mysql_cfg.get("name") or ""

if not DB_PASS or not DB_NAME:
    print("错误: 数据库配置缺失（库名或密码）。请设置 TEST_DB_PASS/TEST_DB_NAME 环境变量，或在 ~/mysql.txt 配置", file=sys.stderr)
    sys.exit(1)


def extract_cloud_service(skill_name: str) -> str:
    """从 skill 名提取服务代码：huawei-cloud-rds-list → rds。"""
    m = re.match(r"huawei-cloud-([a-z0-9]+)", skill_name)
    return m.group(1) if m else skill_name


def resolve_service_name(skill_name: str, case_run_dir: Path) -> str:
    """从 skill-service-map.md 查服务显示名。"""
    service = extract_cloud_service(skill_name)
    map_file = Path(__file__).parent.parent / "references" / "skill-service-map.md"
    if map_file.exists():
        for line in map_file.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*([\w-]+)\s*\|\s*(.+?)\s*\|", line)
            if m and m.group(1) == skill_name:
                return m.group(2).strip()
    return service.upper()


def fetch_issue_by_pr(owner: str, repo: str, pr_number: str) -> dict:
    """通过 gitcode_api.sh issue get-by-pr 反推 PR 关联的需求 issue（排除'测试发现BUG'评测产物）。
    返回 {'id','number','title','html_url','user'} 或 {}（反推失败）。"""
    script = Path(__file__).parent / "gitcode_api.sh"
    try:
        proc = subprocess.run(
            [str(script), "issue", "get-by-pr", owner, repo, pr_number],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json
        return _json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
    except Exception:
        return {}


def fetch_issue_body(owner: str, repo: str, issue_number: str) -> str:
    """获取 GitCode issue 的 body（bug 类型 issue 的问题单常内嵌在 body 里）。"""
    script = Path(__file__).parent / "gitcode_api.sh"
    try:
        proc = subprocess.run(
            [str(script), "issue", "get", owner, repo, str(issue_number)],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json
        d = _json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
        return d.get("body") or ""
    except Exception:
        return ""


def parse_bugs_from_text(text: str) -> list:
    """从任意文本（问题单 md 或 issue body）解析 BUG 列表，解析不到返回 []。"""
    if not text:
        return []
    return _extract_bugs(text)


def merge_bugs(bugs1: list, bugs2: list) -> list:
    """合并两个智能体的 BUG 集（双智能体测试，按 problem 归一化去重）。

    - 相同 problem（归一化后）视为同一 BUG，只保留一份（队长 bug1 优先）
    - 不同 problem → 保留 skills_test2 的，并分配连续 seq_no（从 bug1 最大序号后递增）
    返回合并后的新列表（不修改入参）。
    """
    def norm(b):
        return re.sub(r"\s+", " ", (b.get("problem") or "")).strip().lower()

    merged = [dict(b) for b in bugs1]
    seen = {norm(b) for b in merged if norm(b)}
    max_seq = 0
    for b in merged:
        m = re.search(r"(\d+)$", b.get("seq_no") or "")
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    next_seq = max_seq + 1
    for b2 in bugs2:
        n = norm(b2)
        if n and n in seen:
            continue
        if n:
            seen.add(n)
        b = dict(b2)
        b["seq_no"] = f"BUG-{next_seq:03d}"
        next_seq += 1
        merged.append(b)
    return merged


def fetch_bug_stats(owner: str, repo: str, pr_number: str) -> dict:
    """通过 gitcode_api.sh issue bugs-by-pr 统计该 PR 的 BUG issue。"""
    script = Path(__file__).parent / "gitcode_api.sh"
    try:
        proc = subprocess.run(
            [str(script), "issue", "bugs-by-pr", owner, repo, pr_number],
            capture_output=True, text=True, timeout=60,
        )
        import json as _json
        data = _json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
        return {"total": int(data.get("total") or 0), "closed": int(data.get("closed") or 0)}
    except Exception:
        return {"total": 0, "closed": 0}


def parse_issue_stats(case_dir: Path) -> tuple:
    """从问题单统计 BUG 数量与已修复数量（优先于 GitCode BUG issue 统计）。

    问题单格式（basictest 归档规范）:
      - 问题数量: 2（首测发现）
      - 状态: **2/2 已修复并通过重测验证** ✅
      ## 汇总
      | ... | 状态 |
      | ... | ✅ 已修复 |

    计数统一走 _extract_bugs（兼容标题块 `## 问题 N:` 与表格 `| BUG-00X |`），
    保证主表 total/resolved 与子表 hd_skill_bug_detail 一致；
    解析不到时用"问题数量/原 N 个问题"表头兜底。

    返回 (total, resolved)：问题数量 / 已修复数量。
    无问题单文件或解析不到 → (0, 0)。
    """
    candidates = sorted(case_dir.glob("*问题单*.md")) + [case_dir / "问题单.md"]
    for f in candidates:
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        bugs = _extract_bugs(text)
        if bugs:
            total = len(bugs)
            resolved = sum(1 for b in bugs if b.get("resolved"))
            return total, resolved
        # 兜底：表头计数
        total = 0
        m = re.search(r"原\s*(\d+)\s*个问题", text)
        if m:
            total = int(m.group(1))
        else:
            m = re.search(r"问题数量[:：]\s*(\d+)", text)
            total = int(m.group(1)) if m else 0
        if total == 0:
            total = len(re.findall(r"^#{2,3}\s*问题\s*\d+:", text, re.M))
        resolved = 0
        m2 = re.search(r"状态[:：]\s*[*\s]*(\d+)\s*[/／]\s*(\d+)\s*已修复", text)
        if m2:
            resolved = int(m2.group(1))
            if total == 0:
                total = int(m2.group(2))
        else:
            resolved = len(re.findall(r"✅\s*已修复", text))
        if resolved == 0 and "全部修复" in text and total > 0:
            resolved = total
        # 重测轮："问题数量: 0（首测 N 个 BUG 均已修复）" → total/resolved = N
        if total == 0:
            m3 = re.search(r"首测\s*(\d+)\s*个(?:\s*BUG|bug)?(?:\s*问题)?(?:均已修复|全部修复)", text)
            if m3:
                total = int(m3.group(1))
                resolved = total if re.search(r"均已修复|全部修复", text) else resolved
        return total, resolved
    return 0, 0


_SEVERITY_MAP = {
    "HIGH": "高", "MEDIUM": "中", "LOW": "低",
    "阻断": "高", "严重": "高", "一般": "中", "提示": "低",
    "高": "高", "中": "中", "低": "低",
}


def _extract_bugs(text: str) -> list:
    """从问题单提取 BUG 列表，兼容两种格式，返回 [{seq_no, severity, problem, resolved}]。

    格式 A（标题块）:
      ## 问题 1: {标题}
      | 严重级别 | HIGH |
      | 编号 | {SKILL}-ISSUE-{序号} |
      - 状态: ✅ 已修复

    格式 B（表格行，verify 轮常见）:
      | BUG-004 | 🔴 阻断 | 问题描述 | 复测结果 | ✅ 已修复 |
      | BUG-004 | HIGH（阻断） | 逻辑错误 | 问题描述 | — | ✅ 已修复 |

    去重规则：同 seq_no 出现多次时，resolved 取任一为真、problem 取最完整（最长）者。
    """
    bugs = {}
    seen_heads = set()

    def add_bug(seq_no, severity, problem, resolved):
        if not seq_no:
            return
        seq_no = seq_no.strip()
        if not re.fullmatch(r"BUG-\d+", seq_no):
            return
        prev = bugs.get(seq_no)
        if not prev:
            bugs[seq_no] = {"seq_no": seq_no, "severity": severity or "低",
                            "problem": problem or "", "resolved": 1 if resolved else 0}
        else:
            prev["resolved"] = 1 if (resolved or prev["resolved"]) else 0
            if problem and len(problem) > len(prev.get("problem") or ""):
                prev["problem"] = problem
            if not prev.get("severity") or prev["severity"] == "低":
                prev["severity"] = severity or prev["severity"]

    # --- 格式 A：标题块 ---
    for block in re.split(r"\n## 问题 \d+:", text)[1:]:
        idx = len(seen_heads) + 1
        seen_heads.add(idx)
        lines = block.strip().splitlines()
        title = lines[0].strip() if lines else f"问题{idx}"
        sev_m = re.search(r"\|\s*严重级别\s*\|\s*(\w+)", block)
        severity = _SEVERITY_MAP.get((sev_m.group(1) if sev_m else "").upper(), "低")
        resolved = 1 if re.search(r"状态\s*[:：]\s*✅?\s*已修复|✅\s*已修复|复测结果[^\n]*已修复", block) else 0
        no_m = re.search(r"\|\s*编号\s*\|\s*[^|]*-ISSUE-(\d+)", block)
        seq_no = f"BUG-{no_m.group(1)}" if no_m else f"BUG-{idx:03d}"
        add_bug(seq_no, severity, title, resolved)

    # --- 格式 B：表格行（按表头列名取值，兼容 verify/汇总 两种表） ---
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # 表头 = |...| 行 + 紧随其后的 |---| 分隔行
        if line.startswith("|") and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if re.fullmatch(r"\|[\s\-|]+\|", nxt) or re.fullmatch(r"\|[\s\-|]+\|", nxt.replace(" ", "")):
                header_cells = [c.strip() for c in line.strip("|").split("|")]
                j = i + 2
                while j < len(lines):
                    row = lines[j].strip()
                    if not (row.startswith("|") and row.endswith("|")):
                        break
                    cells = [c.strip() for c in row.strip("|").split("|")]
                    if cells:
                        # 编号：BUG-00X 或 *ISSUE-00X（重测结论表常见 HUAWEI-CLOUD-XXX-ISSUE-001）
                        m_seq = re.fullmatch(r"BUG-(\d+)", cells[0])
                        m_issue = re.fullmatch(r".*-ISSUE-(\d+)(.*)", cells[0], re.S)
                        if not m_seq and not m_issue:
                            j += 1
                            continue
                        cell_map = {h: (cells[k] if k < len(cells) else "") for k, h in enumerate(header_cells)}
                        seq_no = f"BUG-{m_seq.group(1)}" if m_seq else f"BUG-{m_issue.group(1)}"
                        sev_raw = cell_map.get("严重级别", "") or ""
                        severity = "低"
                        for k, v in (("HIGH", "高"), ("MEDIUM", "中"), ("LOW", "低"),
                                     ("阻断", "高"), ("严重", "高"), ("一般", "中"), ("提示", "低"),
                                     ("高", "高"), ("中", "中"), ("低", "低")):
                            if k in sev_raw.upper():
                                severity = v
                                break
                        status_cell = cell_map.get("状态", "") or ""
                        resolved = 1 if re.search(r"✅|已修复", status_cell or row) else 0
                        problem = ""
                        # 问题列：标题/问题/首测 BUG 等；*ISSUE-00X 格式的问题藏在编号单元格后半段
                        if m_issue and m_issue.group(2).strip():
                            problem = m_issue.group(2).strip()
                        for pk in ("问题", "verify 发现的问题", "verify发现的问题", "首测 BUG", "原问题"):
                            if pk in cell_map and cell_map.get(pk, "").strip():
                                cand = re.sub(r"^.*?-ISSUE-\d+", "", cell_map[pk].strip()).strip()
                                if cand and len(cand) > len(problem):
                                    problem = cand
                        add_bug(seq_no, severity, problem, resolved)
                    j += 1
                i = j
                continue
        i += 1

    return list(bugs.values())


def parse_bug_details(case_dir: Path) -> list:
    """从问题单解析每个 BUG 详情（兼容标题块与表格格式）。

    返回 [{seq_no, severity, problem, resolved}, ...]。
    无问题单或解析不到 → []。
    """
    candidates = sorted(case_dir.glob("*问题单*.md")) + [case_dir / "问题单.md"]
    for f in candidates:
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        bugs = _extract_bugs(text)
        if bugs:
            return bugs
    return []


def upsert_bug_details(cur, issue_id: str, bugs: list) -> tuple:
    """将 bug 详情逐条 upsert 到 hd_skill_bug_detail。

    按 (issue_id, seq_no) 匹配：存在则 UPDATE，不存在则 INSERT。
    任一行失败仅跳过该条（记原因 continue），不阻塞其余子表行。
    返回 (inserted, updated) 计数。"""
    inserted = updated = 0
    for b in bugs:
        try:
            cur.execute(
                "SELECT id FROM hd_skill_bug_detail WHERE issue_id=%s AND seq_no=%s",
                (issue_id, b["seq_no"]),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """UPDATE hd_skill_bug_detail
                       SET severity=%s, problem=%s, resolved=%s
                       WHERE id=%s""",
                    (b["severity"], b["problem"], b["resolved"], row[0]),
                )
                updated += 1
            else:
                cur.execute(
                    """INSERT INTO hd_skill_bug_detail
                       (issue_id, seq_no, severity, problem, resolved)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (issue_id, b["seq_no"], b["severity"], b["problem"], b["resolved"]),
                )
                inserted += 1
        except Exception as e:
            print(f"  ⚠️ 子表 BUG {b['seq_no']} 入库失败（跳过该条）: {e}", file=sys.stderr)
    return inserted, updated


def fetch_pr_title(owner: str, repo: str, pr_number: str) -> str:
    """获取 PR 标题（反推不到需求 issue 时，PR 当 issue 用）。"""
    script = Path(__file__).parent / "gitcode_api.sh"
    try:
        proc = subprocess.run(
            [str(script), "pr", "get", owner, repo, pr_number],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json
        d = _json.loads(proc.stdout) if proc.stdout.strip() else {}
        return d.get("title") or ""
    except Exception:
        return ""


def fetch_pr_id(owner: str, repo: str, pr_number: str) -> str:
    """获取 PR 全局 id（反推不到需求 issue 时，以 PR 自身为记录单元，避免用仓库内编号跨仓歧义）。"""
    script = Path(__file__).parent / "gitcode_api.sh"
    try:
        proc = subprocess.run(
            [str(script), "pr", "resolve-id", f"https://gitcode.com/{owner}/{repo}/pull/{pr_number}"],
            capture_output=True, text=True, timeout=30,
        )
        return (proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "") or ""
    except Exception:
        return ""


def fetch_pr_user(owner: str, repo: str, pr_number: str) -> str:
    """获取 PR 创建人 login（无需求 issue 时用 PR 创建人作为 owner）。"""
    script = Path(__file__).parent / "gitcode_api.sh"
    try:
        proc = subprocess.run(
            [str(script), "pr", "get", owner, repo, pr_number],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json
        d = _json.loads(proc.stdout) if proc.stdout.strip() else {}
        login = (d.get("user") or {}).get("login") or ""
        if not login:
            print(f"  ⚠️ 未获取到 PR #{pr_number} 创建人，owner 将回退 --owner 兜底", file=sys.stderr)
        return login
    except Exception as e:
        print(f"  ⚠️ 获取 PR #{pr_number} 创建人失败（owner 将回退 --owner 兜底）: {e}", file=sys.stderr)
        return ""


def main():
    t0 = time.perf_counter()
    parser = argparse.ArgumentParser(description="评测结果写入 hd_skill_test_detail")
    parser.add_argument("--skill", required=True, help="被测 skill 名")
    parser.add_argument("--case-run", required=True, help="case-run 日期目录")
    parser.add_argument("--report-url", default="", help="OBS 回源下载 URL")
    parser.add_argument("--pr", default="", help="PR 编号（用于追溯需求 issue URL）")
    parser.add_argument("--repo", default="developer-skill/huaweicloud-skills-dev", help="仓库 owner/repo")
    parser.add_argument("--owner", default="skills_bot", help="负责人")
    parser.add_argument("--bug-issue-url", default="", help="BUG issue URL（有 BUG 且已创建时优先使用，否则回退到需求 issue URL）")
    parser.add_argument("--second-case-run", default="", help="第二个智能体的 case-run 目录（双智能体测试结果不一致时传入，合并两边 BUG 入库）")
    args = parser.parse_args()

    case_dir = Path(args.case_run)
    if not case_dir.is_dir():
        print(f"错误: 目录不存在: {case_dir}", file=sys.stderr)
        sys.exit(1)

    cloud_service = extract_cloud_service(args.skill)
    service_name = resolve_service_name(args.skill, case_dir)

    issue_url = args.bug_issue_url
    issue_id = ""
    issue_name = ""
    total_count = 0
    resolved_count = 0

    issue_owner = ""
    owner, _, repo = args.repo.partition("/")
    if args.pr:
        # 反推需求 issue
        req_issue = fetch_issue_by_pr(owner, repo, args.pr)
        if req_issue:
            issue_id = req_issue["id"]
            issue_name = req_issue["title"]
            issue_owner = req_issue.get("user") or ""
            if not issue_url:
                issue_url = req_issue["html_url"]
        else:
            # 反推不到需求 issue → 用 PR 全局 id 作为记录单元（避免仓库内编号跨仓歧义）
            pr_id = fetch_pr_id(owner, repo, args.pr)
            issue_id = f"pr:{pr_id}" if pr_id else f"pr:{args.pr}"
            issue_name = fetch_pr_title(owner, repo, args.pr)
            issue_owner = fetch_pr_user(owner, repo, args.pr)
            if not issue_url:
                pr_url = f"https://gitcode.com/{owner}/{repo}/merge_requests/{args.pr}"
                issue_url = pr_url
        # 统计 BUG（问题单优先；解析不到时用子表 bug 详情计数兜底，再兜底 GitCode BUG issue）
        total_count, resolved_count = parse_issue_stats(case_dir)
        bugs_all = []
        if total_count == 0 and resolved_count == 0:
            bugs = parse_bug_details(case_dir)
            if not bugs and req_issue:
                # 回退: bug 类型 issue 的问题单常内嵌在 issue body(无 case_run 文件时)
                body = fetch_issue_body(owner, repo, req_issue.get("number"))
                bugs = parse_bugs_from_text(body)
            if bugs:
                total_count = len(bugs)
                resolved_count = sum(1 for b in bugs if b.get("resolved"))
                bugs_all = bugs
            elif args.pr:
                stats = fetch_bug_stats(owner, repo, args.pr)
                total_count = stats["total"]
                resolved_count = stats["closed"]
        else:
            bugs_all = parse_bug_details(case_dir)

        # 双智能体测试结果不一致时，合并第二个智能体的 BUG 入库（以队长报告为主）
        if args.second_case_run:
            second_dir = Path(args.second_case_run)
            bugs2 = parse_bug_details(second_dir)
            if not bugs2 and second_dir.is_dir():
                # 回退: 直接读 {skill}_问题单.md
                for f in sorted(second_dir.glob(f"{args.skill}_问题单.md")) + [second_dir / "问题单.md"]:
                    if f.is_file():
                        bugs2 = parse_bugs_from_text(f.read_text(encoding="utf-8"))
                        break
            if bugs2:
                merged = merge_bugs(bugs_all, bugs2)
                if merged:
                    print(f"  [双智能体] 合并 BUG: 队长 {len(bugs_all)} 条 + 第二智能体新增 {len(merged)-len(bugs_all)} 条 → 共 {len(merged)} 条")
                    bugs_all = merged
                    total_count = len(merged)
                    resolved_count = sum(1 for b in merged if b.get("resolved"))
                else:
                    print("  ⚠️ 第二智能体问题单解析不到 BUG，仅以队长报告入库", file=sys.stderr)

    owner_field = issue_owner or args.owner
    # bug 类型 issue（基本功评测/测试发现BUG/评测发现问题）: 责任人 = 审查人(GITCODE_PR_ASSIGNEE)
    # 与 gitcode-event-handler 入库规则一致（bug issue 创建人≠修复人, 责任人指向 skill 历史提交人/审查人）
    if re.search(r"基本功评测发现|测试发现BUG|评测发现|发现\d+条问题|问题单", issue_name):
        reviewer = os.environ.get("GITCODE_PR_ASSIGNEE", "lccode").strip()
        if reviewer:
            owner_field = reviewer
    passed = 1 if resolved_count == total_count else 0

    try:
        conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME, charset="utf8mb4", connect_timeout=10)
    except pymysql.MySQLError as e:
        print(f"错误: 数据库连接失败 ({DB_HOST}/{DB_NAME}): {e}", file=sys.stderr)
        sys.exit(1)
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO hd_skill_test_detail
               (cloud_service, cloud_service_name, skill_name, issue_id, issue_name,
                test_time, passed, issue_url, total_count, resolved_count, owner, report_url)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                 cloud_service=VALUES(cloud_service),
                 cloud_service_name=VALUES(cloud_service_name),
                 skill_name=VALUES(skill_name),
                 issue_name=VALUES(issue_name),
                 test_time=VALUES(test_time),
                 passed=VALUES(passed),
                 issue_url=VALUES(issue_url),
                 total_count=VALUES(total_count),
                 resolved_count=VALUES(resolved_count),
                 owner=VALUES(owner),
                 report_url=VALUES(report_url)""",
            (cloud_service, service_name, args.skill, issue_id, issue_name,
             datetime.now(), passed, issue_url, total_count, resolved_count, owner_field, args.report_url),
        )
        conn.commit()  # 主表提交
        new_id = cur.lastrowid  # ← 在这里取主表 id（必须在子表块之前，否则会被子表自增 id 覆盖）

        # 子表：BUG 详情入库（问题单解析 + 逐条 upsert；双智能体合并时用 bugs_all）
        try:
            if args.second_case_run and bugs_all:
                bugs = bugs_all
                print(f"  [双智能体] 子表按合并结果写入 {len(bugs)} 条")
            else:
                bugs = parse_bug_details(case_dir)
                if not bugs and req_issue:
                    # 回退: bug 类型 issue 的问题单内嵌在 body（无 case_run 文件时）
                    body = fetch_issue_body(owner, repo, req_issue.get("number"))
                    bugs = parse_bugs_from_text(body)
            if bugs and issue_id:
                ins, upd = upsert_bug_details(cur, issue_id, bugs)
                conn.commit()
                print(f"  BUG 详情: 写入 {ins} 条 / 更新 {upd} 条")
            elif bugs:
                print("  ⚠️ 未解析到 issue_id，跳过子表 BUG 详情入库", file=sys.stderr)
        except Exception as e:
            print(f"  ⚠️ 子表 BUG 详情入库失败（不阻塞主表）: {e}", file=sys.stderr)
            try:
                conn.rollback()
            except Exception:
                pass
        print(f"✅ 已写入 hd_skill_test_detail id={new_id}")
        print(f"  skill_name: {args.skill}")
        print(f"  issue_id: {issue_id or '(空)'}")
        print(f"  issue_name: {issue_name or '(空)'}")
        print(f"  cloud_service: {cloud_service} ({service_name})")
        print(f"  passed: {passed} (resolved {resolved_count} / total {total_count})")
        print(f"  issue_url: {issue_url or '(空)'}")
        print(f"  owner: {owner_field}")
        print(f"  report_url: {args.report_url}")
        try:
            timing = open(os.environ.get("EVAL_TIMING_LOG", "/tmp/eval_timing.log"), "a", encoding="utf-8")
            timing.write(f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}|{args.skill}|insert_test_detail|end|DB入库|{(time.perf_counter()-t0)*1000:.0f}\n")
            timing.close()
        except Exception:
            pass
    except Exception as e:
        print(f"错误: 写入 hd_skill_test_detail 失败: {e}", file=sys.stderr)
    finally:
        if cur:
            cur.close()
        conn.close()


if __name__ == "__main__":
    main()
