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
    (re.compile(r"/root/grape-workspaces|/root/grape\b|/opt/grape\b"), "服务器内部绝对路径"),
    (re.compile(r"\b121\.36\.33\.\d{1,3}\b"), "生产公网 IP"),
    (re.compile(r"grape\.topxtopx\.com"), "内部域名"),
    (re.compile(r"systemctl (restart|start|stop|status) grape"), "运维操作记录"),
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


def main() -> int:
    d = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "--dir" else DEFAULT_DIR
    if len(sys.argv) > 1 and sys.argv[1] == "--dir":
        d = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DIR)
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