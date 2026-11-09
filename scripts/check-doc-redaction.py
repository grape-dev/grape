#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docs 开源合规敏感值检查（防回归）。

扫描 docs/** 下的 markdown/文本，命中内部凭据/账号/资产/运维信息即报错并给出 文件:行。
对齐 scripts/check-*.py 风格；由 scripts/check-all.sh 汇总调用。

用法：python scripts/check-doc-redaction.py [--dir docs]
"""
import os
import re
import sys

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")

# 命中即判定泄漏的内部值模式（逐个精评，避免误伤架构常量）
PATTERNS = [
    (re.compile(r"oc_[0-9a-fA-F]{16,}"), "飞书群/会话 chat_id"),
    (re.compile(r"\bAgentL_[0-9A-Za-z]+\b"), "引擎凭据名"),
    (re.compile(r"opencode-cred\d*|codearts-cred\d*"), "引擎凭据名"),
    (re.compile(r"/root/grape-workspaces|/root/grape\b|/opt/grape\b|/home/[A-Za-z0-9_.-]+/"), "服务器内部绝对路径"),
    (re.compile(r"\b121\.36\.33\.\d{1,3}\b"), "生产公网 IP"),
    # 弹性公网 EIP / 私网段（2026-11-09 PR #1077 评审补：原规则只覆盖单一生产公网 IP）
    (re.compile(r"\b124\.70\.\d{1,3}\.\d{1,3}\b"), "弹性公网 IP（EIP）"),
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), "内网 IP（RFC1918）"),
    (re.compile(r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"), "内网 IP（RFC1918）"),
    (re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "内网 IP（RFC1918）"),
    # 资产编号（主机别名 0001~0999）：避免真实机器编号进公开文档
    # 机器资产编号 0001-0010（主机别名）；允许 worker-0001 / hosts/0001 这类分隔写法，
    # 同时排除 chmod 0755/0600、0022 之类非资产数字（QA 第 5 轮健壮性项）。
    (re.compile(r"(?<!\d)00(?:0[1-9]|10)(?!\d)"), "资产编号（应改为 master-N / worker-N）"),
    (re.compile("topx" "topx" r"\.com"), "内部域名（规则表内拼装，源码不留明文）"),
    (re.compile(r"systemctl (restart|start|stop|status) grape"), "运维操作记录"),
    # 磁盘容量/使用率等运维事实
    (re.compile(r"磁盘[^。\n]{0,12}([0-9]{2,3}\s*%|\d+\s*(GB|TB))"), "磁盘容量/使用率运维事实"),
    (re.compile(r"\b#\s?2337\b|\b#\s?2338\b"), "内部事故编号"),
    (re.compile(r"1ccb996a[0-9a-f\-]*"), "真实 conversation id"),
]


def scan(path: str) -> list:
    hits = []
    for root, _dirs, files in os.walk(path):
        for fn in files:
            if not (fn.endswith(".md") or fn.endswith(".txt")):
                continue
            fp = os.path.join(root, fn)
            if _exempt(fp):
                continue
            try:
                with open(fp, encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception:
                continue
            for ln, line in enumerate(lines, 1):
                for rx, desc in PATTERNS:
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(fp, path)}:{ln}  [{desc}]  {line.strip()[:100]}")
    return hits


SCAN_EXTS = (".md", ".txt", ".js", ".py", ".sh", ".html", ".json", ".yaml", ".yml")

# 规则表自身含待检字符串（正则片段、示例编号），扫描时豁免守卫脚本，避免门禁自伤。
SELF_EXEMPT = ("scripts/check-doc-redaction.py",)


def _exempt(path: str) -> bool:
    p = path.replace("\\", "/")
    return any(p.endswith(x) or x in p for x in SELF_EXEMPT)


def scan_files(files: list) -> list:
    """按显式文件清单扫描（PR 门禁用：只查本次变更的代码与文档）。"""
    hits = []
    for fp in files:
        if not fp.endswith(SCAN_EXTS) or _exempt(fp):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue
        for ln, line in enumerate(lines, 1):
            for rx, desc in PATTERNS:
                if rx.search(line):
                    hits.append(f"{fp}:{ln}  [{desc}]  {line.strip()[:100]}")
    return hits


def scan_diff(base: str) -> list:
    """只扫描 git diff base..HEAD 的新增行（PR 门禁口径：只审本次引入内容）。

    全文件扫描会把文件里的存量行也算违规（历史代码本来就是内部仓库口径），
    反而让门禁无法接入 CI。这里用 `git diff -U0` 取新增行做精确判定。
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "diff", "-U0", base, "--", "."],
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception as e:  # pragma: no cover
        # 不能静默返回空：浅克隆/base 写错时会假阴性（QA 第 5 轮健壮性项）。
        print(f"git diff failed for base '{base}': {e}", file=sys.stderr)
        raise
    hits = []
    cur_file = ""
    new_ln = 0
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            cur_file = line[6:]
            continue
        if line.startswith("@@"):
            # @@ -a,b +c,d @@
            try:
                new_ln = int(line.split("+")[1].split(",")[0].split(" ")[0])
            except Exception:
                new_ln = 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if _exempt(cur_file):
                new_ln += 1
                continue
            content = line[1:]
            for rx, desc in PATTERNS:
                if rx.search(content):
                    hits.append(f"{cur_file}:{new_ln}  [{desc}]  {content.strip()[:100]}")
            new_ln += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_ln += 1
    return hits


def main() -> int:
    args = sys.argv[1:]
    # PR 门禁用法（推荐）：check-doc-redaction.py --diff <base-sha>  只扫新增行
    if args and args[0] == "--diff":
        base = args[1] if len(args) > 1 else "HEAD~1"
        try:
            hits = scan_diff(base)
        except Exception:
            print("docs-redaction: FAILED (cannot compute diff; check base ref / shallow clone)",
                  file=sys.stderr)
            return 2
    # 定向文件用法：check-doc-redaction.py --files docs/a.md ...
    elif args and args[0] == "--files":
        files = args[1:]
        if not files:
            print("--files 需要至少一个文件路径", file=sys.stderr)
            return 2
        hits = scan_files(files)
    else:
        d = DEFAULT_DIR
        if len(args) >= 2 and args[0] == "--dir":
            d = os.path.abspath(args[1])
        elif len(args) == 1 and args[0] != "--dir":
            d = args[0]
        hits = scan(d)
    if hits:
        print("docs 敏感值检查未通过（禁止将下列内容合入开源分支）：")
        for h in hits:
            print("  " + h)
        print(f"\n共 {len(hits)} 处，请脱敏（占位符）或移出公开分支后重跑。")
        return 1
    print("docs-redaction: OK（无内部凭据/账号/资产/运维信息）")
    return 0


if __name__ == "__main__":
    sys.exit(main())