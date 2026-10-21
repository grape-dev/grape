#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全仓明文密钥扫描（防回归）。

2026-10-21 审计起因：脚本里明文硬编码的 OBS AK/SK 随开源清洗分支 weekly-07-clean 与
发布包外泄，而此前的 docs 脱敏检查（check-doc-redaction.py）只扫 docs/**、也没有密钥
模式，所以**没有任何守卫**拦住它。本脚本把扫描面扩到全仓跟踪文件，专查密钥类明文。

扫描范围：git 跟踪文件（默认），可用 --dir 指定目录；跳过二进制/产物目录。
豁免：行内注释 `secrets-check: allow`（用于文档/测试里的"模式本身"或占位示例）。

用法：
    python scripts/check-secrets.py            # 扫全仓跟踪文件
    python scripts/check-secrets.py --dir docs # 只扫某目录
退出码：0 = 通过；1 = 命中（逐条打印 文件:行）。
"""
import argparse
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 命中即判定为明文密钥（逐条精评，避免误伤普通常量/哈希）
PATTERNS = [
    (re.compile(r"\bHPU[A-Z0-9]{17}\b"), "华为云 Access Key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "AWS 临时 Access Key"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "GitHub 细粒度 PAT"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥块"),
    # 显式赋值形态：-k=<40>、secret_key/secretAccessKey/sk = <32+>
    (re.compile(r"-k=[A-Za-z0-9/+]{32,}"), "obsutil -k 明文 Secret Key"),
    (re.compile(r"(?:\bsecret[_-]?key\b|\bsecretAccessKey\b|\bsk\b)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9/+]{32,}",
                re.IGNORECASE), "Secret Key 明文赋值"),
    # access_key = <20 位大写数字串>（华为 AK 常见形态）
    (re.compile(r"(?:\baccess[_-]?key(?:_id)?\b|\bak\b)['\"]?\s*[:=]\s*['\"]?[A-Z0-9]{20}\b",
                re.IGNORECASE), "Access Key 明文赋值"),
]

ALLOW_MARK = "secrets-check: allow"
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "release", "staging", ".pytest_cache"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf",
            ".zip", ".gz", ".tar", ".whl", ".pyc", ".pdf", ".so", ".bin", ".map"}
MAX_BYTES = 2 * 1024 * 1024


def tracked_files() -> list:
    try:
        out = subprocess.run(["git", "-C", REPO_ROOT, "ls-files"],
                             capture_output=True, text=True, check=True).stdout
        return [f for f in out.split("\n") if f.strip()]
    except Exception:
        return []


def walk_files(root: str) -> list:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            files.append(os.path.relpath(fp, REPO_ROOT))
    return files


def scan(rel_paths: list) -> list:
    hits = []
    for rel in rel_paths:
        if os.path.splitext(rel)[1].lower() in SKIP_EXT:
            continue
        fp = os.path.join(REPO_ROOT, rel)
        try:
            if os.path.getsize(fp) > MAX_BYTES:
                continue
            with open(fp, encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()
        except Exception:
            continue
        for idx, line in enumerate(lines, 1):
            if ALLOW_MARK in line:
                continue
            for rx, desc in PATTERNS:
                m = rx.search(line)
                if m:
                    snippet = line.strip()
                    # 命中片段脱敏：只留前缀，避免扫描输出本身把密钥打全
                    tok = m.group(0)
                    masked = tok[:10] + "…" if len(tok) > 12 else tok
                    hits.append((rel, idx, desc, masked, snippet[:80]))
                    break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="全仓明文密钥扫描")
    ap.add_argument("--dir", action="append", default=None,
                    help="只扫描指定目录（可多次）；默认扫描 git 跟踪文件全量")
    args = ap.parse_args()

    if args.dir:
        rels = []
        for d in args.dir:
            root = os.path.join(REPO_ROOT, d)
            if os.path.isdir(root):
                rels.extend(walk_files(root))
            else:
                rels.append(d)
    else:
        rels = tracked_files()

    hits = scan(rels)
    if hits:
        print("secrets-check 未通过（禁止把明文密钥合入任何分支）：")
        for rel, idx, desc, masked, snippet in hits:
            print(f"  [{desc}] {rel}:{idx}  {masked}  | {snippet}")
        print(f"\n共 {len(hits)} 处。修法：改为读环境变量或非入库配置"
              f"（见 scripts/obs_credentials.py）；确属模式/占位示例请加行内标记 `{ALLOW_MARK}`。")
        return 1
    print(f"secrets-check: OK（{len(rels)} 个文件无明文密钥）")
    return 0


if __name__ == "__main__":
    sys.exit(main())