#!/usr/bin/env python3
"""Grape 特有架构约束检查脚本。

只读。标准库。检查 Grape 项目的核心架构约束是否被违反。

Usage:
    python grape_constraint_check.py [path] [--json]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".tox",
    "dist", "build", ".idea", ".vscode", "vendor", ".venv", "venv",
    "env", ".eggs", ".mypy_cache", ".ruff_cache", "htmlcov",
    "coverage", ".cache", "target", "bin", "obj", ".next",
    ".nuxt", "out", ".gradle", ".terraform", "wheels",
}

CODE_EXT = {".py", ".js", ".ts", ".vue", ".java", ".sh", ".yml", ".yaml",
            ".toml", ".ini", ".conf", ".json", ".html", ".css"}


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in CODE_EXT:
                yield p


def check_uvicorn_workers(root: Path):
    """Gate 1-1: 检查 uvicorn 启动参数是否为单 worker。"""
    findings = []
    patterns = [
        (r'--workers\s+[2-9]\d*', "uvicorn 多 worker 启动参数"),
        (r'--workers\s+1[0-9]+', "uvicorn 多 worker 启动参数"),
    ]
    for f in walk(root):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat, desc in patterns:
                if re.search(pat, line):
                    findings.append({
                        "gate": "1-1",
                        "severity": "BLOCKED",
                        "file": str(f.relative_to(root)),
                        "line": i,
                        "issue": desc,
                        "evidence": line.strip()[:200],
                        "risk": "db_lock 跨进程无效 + 定时任务重复",
                    })
    return findings


def check_conftest_setdefault(root: Path):
    """Gate 1-6: conftest.py 是否用 setdefault 而非强制覆盖。"""
    findings = []
    conftest = root / "tests" / "conftest.py"
    if not conftest.exists():
        return findings
    try:
        text = conftest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for i, line in enumerate(text.splitlines(), 1):
        if "setdefault" in line and ("DEVOPS_DB" in line or "DB_BACKEND" in line):
            findings.append({
                "gate": "1-6",
                "severity": "BLOCKED",
                "file": "tests/conftest.py",
                "line": i,
                "issue": "conftest.py 使用 setdefault 而非强制覆盖",
                "evidence": line.strip()[:200],
                "risk": ".env 已设置 mysql 时 setdefault 无效，测试可能连生产库",
            })
    # Gate 1-7: 检查是否清除 MySQL 参数
    has_clear = bool(re.search(r'DEVOPS_DB_HOST.*pop|DEVOPS_DB_HOST.*del', text))
    if not has_clear:
        findings.append({
            "gate": "1-7",
            "severity": "BLOCKED",
            "file": "tests/conftest.py",
            "line": 0,
            "issue": "conftest.py 未清除 DEVOPS_DB_HOST 等 MySQL 参数",
            "evidence": "未找到 pop/del DEVOPS_DB_HOST 的代码",
            "risk": ".env 指向生产 MySQL 时测试连生产库并清空数据",
        })
    return findings


def check_bare_delete(root: Path):
    """Gate 1-8: DELETE 语句是否带 WHERE 子句。"""
    findings = []
    delete_re = re.compile(r'DELETE\s+FROM\s+\w+', re.I)
    where_re = re.compile(r'\bWHERE\b', re.I)
    for f in walk(root):
        if f.suffix != ".py":
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if delete_re.search(line) and not where_re.search(line):
                # Check next few lines for WHERE
                context = "\n".join(lines[i-1:min(i+3, len(lines))])
                if not where_re.search(context):
                    findings.append({
                        "gate": "1-8",
                        "severity": "BLOCKED",
                        "file": str(f.relative_to(root)),
                        "line": i,
                        "issue": "DELETE 语句无 WHERE 子句",
                        "evidence": line.strip()[:200],
                        "risk": "可能误删非测试数据",
                    })
    return findings


def check_worker_manifest(root: Path):
    """Gate 1-5: Worker MANIFEST 是否包含必要文件。"""
    findings = []
    wp = root / "src" / "worker_package.py"
    if not wp.exists():
        return findings
    try:
        text = wp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    required = ["src/auth/", "src/token_service.py", "src/token_resolver.py"]
    for req in required:
        if req not in text:
            findings.append({
                "gate": "1-5",
                "severity": "BLOCKED",
                "file": "src/worker_package.py",
                "line": 0,
                "issue": f"Worker MANIFEST 缺少 {req}",
                "evidence": f"在 MANIFEST 列表中未找到 {req}",
                "risk": f"worker 包缺文件导致启动崩溃或功能异常",
            })
    return findings


def check_threading_lock_cross_process(root: Path):
    """Gate 1-2: 检查是否用 threading.Lock 处理跨进程数据（应走 db_lock）。"""
    findings = []
    for f in walk(root):
        if f.suffix != ".py":
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Skip lock.py itself and test files
        rel = str(f.relative_to(root))
        if rel.endswith("lock.py") or "/test" in rel:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if re.search(r'threading\.Lock\(\)|threading\.RLock\(\)', line):
                # Check if it's used for cross-process data (heuristic: nearby DB access)
                context = "\n".join(lines[max(0,i-5):min(i+5,len(lines))])
                if re.search(r'db\.|cursor|execute|commit', context):
                    findings.append({
                        "gate": "1-2",
                        "severity": "Medium",
                        "file": rel,
                        "line": i,
                        "issue": "可能在跨进程场景使用 threading.Lock 而非 db_lock",
                        "evidence": line.strip()[:200],
                        "risk": "跨进程互斥无效，可能导致数据竞争",
                        "note": "需人工复核：如果仅用于单进程内线程同步则不阻塞",
                    })
    return findings


def check_frontend_version_bump(root: Path, changed_files=None):
    """Gate 3-1: 检查前端 JS 改动后是否 bump 版本号。"""
    findings = []
    index_html = root / "src" / "api" / "static" / "index.html"
    if not index_html.exists():
        return findings
    try:
        text = index_html.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    # Extract current version numbers
    versions = {}
    for m in re.finditer(r'(app|issues|runtimes)\.js\?v=(\d+)', text):
        versions[m.group(1) + ".js"] = int(m.group(2))
    # If changed_files includes JS files, check if version was bumped
    if changed_files:
        for cf in changed_files:
            basename = os.path.basename(cf)
            if basename in versions:
                # Version exists in index.html, OK
                pass
            elif basename.endswith('.js') and basename in ('app.js', 'issues.js', 'runtimes.js'):
                findings.append({
                    "gate": "3-1",
                    "severity": "Critical",
                    "file": "src/api/static/index.html",
                    "line": 0,
                    "issue": f"{basename} 改动但未找到 ?v=N 版本号",
                    "evidence": f"index.html 中未找到 {basename}?v=...",
                    "risk": "浏览器缓存旧版 JS 导致功能异常",
                })
    return findings


def check_credentials_in_diff(root: Path, changed_files=None):
    """Gate 2-3: 检查 PR 是否引入真实凭据。"""
    findings = []
    patterns = [
        (r'AKIA[A-Z0-9]{16}', "AWS 密钥"),
        (r'ghp_[A-Za-z0-9]{36}', "GitHub PAT"),
        (r'glpat-[A-Za-z0-9_-]{20}', "GitLab PAT"),
        (r'BEGIN (RSA|OPENSSH|EC) PRIVATE KEY', "SSH 私钥"),
        (r'192\.168\.\d+\.\d+', "内网 IP"),
        (r'10\.\d+\.\d+\.\d+', "内网 IP"),
        (r'172\.(1[6-9]|2\d|3[01])\.\d+\.\d+', "内网 IP"),
    ]
    files_to_check = changed_files or [str(f) for f in walk(root)]
    for cf in files_to_check:
        fpath = root / cf if not os.path.isabs(cf) else Path(cf)
        if not fpath.exists():
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat, desc in patterns:
                if re.search(pat, line):
                    # Mask the value
                    masked = re.sub(pat, lambda m: m.group(0)[:4] + '****', line)
                    findings.append({
                        "gate": "2-3",
                        "severity": "BLOCKED",
                        "file": str(fpath.relative_to(root)) if fpath.is_relative_to(root) else str(fpath),
                        "line": i,
                        "issue": desc,
                        "evidence": masked.strip()[:200],
                        "risk": "凭据泄露/内网信息暴露",
                    })
    return findings


def run_all_checks(root: Path, changed_files=None):
    """运行所有检查，返回发现列表。"""
    all_findings = []
    all_findings.extend(check_uvicorn_workers(root))
    all_findings.extend(check_worker_manifest(root))
    all_findings.extend(check_conftest_setdefault(root))
    all_findings.extend(check_bare_delete(root))
    all_findings.extend(check_threading_lock_cross_process(root))
    all_findings.extend(check_frontend_version_bump(root, changed_files))
    all_findings.extend(check_credentials_in_diff(root, changed_files))
    return all_findings


def main():
    ap = argparse.ArgumentParser(description="Grape architecture constraint checker")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--changed-files", help="comma-separated list of changed files")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    changed = args.changed_files.split(",") if args.changed_files else None
    findings = run_all_checks(root, changed)

    if args.json:
        print(json.dumps({"root": str(root.resolve()), "findings": findings}, indent=2))
    else:
        print(f"GRAPE CONSTRAINT CHECK: {root}")
        print(f"\nFindings: {len(findings)}")
        sev_order = {"BLOCKED": 0, "Critical": 1, "Medium": 2, "Low": 3, "Informational": 4}
        findings.sort(key=lambda f: sev_order.get(f["severity"], 5))
        for f in findings:
            print(f"\n[{f['severity']}] Gate {f['gate']} — {f['issue']}")
            print(f"  {f['file']}:{f['line']}")
            print(f"  > {f['evidence']}")
            print(f"  Risk: {f['risk']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
