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


# ---------------------------------------------------------------------------
# 规则 2.0 静态可检项（Issue #467）：A6 表名 / B1 SQL 双模式 / D1 i18n
# ---------------------------------------------------------------------------

# A6: 存量表名白名单（grape 主仓既有表，规则 2.0 只约束新增表）
LEGACY_TABLES = {
    "admin_accounts", "agent_runtimes", "agent_runtimes_new", "agent_shared_knowledge",
    "agent_tasks", "agent_type", "agent_type_config", "agent_type_runtime_pool",
    "api_latency_log", "api_tokens", "audit_logs", "autopilot_executions",
    "autopilot_subscriptions", "autopilot_tasks", "backend_agents", "binding_tickets",
    "bot_seen_messages", "chat_agent_session", "chat_agent_session_new", "chat_conversations",
    "chat_messages", "community_comments", "community_post_likes", "community_posts",
    "devops_agent_session", "devops_pipeline_scene", "devops_pipeline_scene_new", "distributed_locks",
    "engine_capabilities", "event_action_filter", "event_active_guard", "event_route_config",
    "event_skipped", "event_thread", "events", "exchange_codes",
    "external_identities", "featured_templates", "feishu_onboard_task", "global_im_bot_config",
    "grape_accounts", "grape_im_instance", "issue", "issue_agent_session",
    "issue_state", "knowledge_write_queue", "login_failures", "login_whitelist",
    "managed_repos", "meta", "oauth_states", "obs_alert_events",
    "obs_alert_rules", "obs_llm_usage", "obs_llm_usage_archive", "obs_log",
    "obs_metric_rollup", "obs_runtime_samples", "obs_slo", "obs_slo_snapshots",
    "org_invitation", "org_member", "organization", "outbound_content_ledger",
    "repo_im_group_binding", "runtime_credentials", "runtime_health_scores", "scene_category",
    "scene_engine_detach_log", "scene_knowledge_ref", "scene_likes", "scene_market",
    "scene_skill_ref", "session_api_tokens", "signup_whitelist", "skill",
    "skill_category", "skill_likes", "task_messages", "unified_user_mapping",
    "uploaded_files", "user_accounts", "user_credentials", "user_feedback",
    "user_repos", "verification_codes", "vod_conversations",
}

# A6: 合法新表名前缀（主控系统 / 运营管理台）
TABLE_PREFIX_RULE = re.compile(r"^(grape_t_|grape_admin_t_)[a-z][a-z0-9_]*$")


def check_table_naming(root: Path, changed_files=None):
    """规则 2.0 A6【Critical】: 新增数据库表名规则（grape_t_xxx / grape_admin_t_xxx）。

    只检查 PR 变更文件（或全仓 py/sql 文件）中的 CREATE TABLE 语句；
    存量表名（LEGACY_TABLES 白名单）不报。
    """
    findings = []
    create_re = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\\[]?([A-Za-z_][A-Za-z0-9_]*)", re.I)
    if changed_files:
        files = []
        for cf in changed_files:
            p = root / cf if not os.path.isabs(cf) else Path(cf)
            if p.suffix.lower() in (".py", ".sql", ".sh", ".js") and p.exists():
                files.append(p)
    else:
        files = [f for f in walk(root) if f.suffix.lower() in (".py", ".sql")]
    for f in files:
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        # 测试夹具表（临时 fixture）、翻译层内部语句与技能自身脚本不参与生产表名规则
        if "/test" in rel or rel.startswith("test") or ".grape/" in rel or "conftest" in rel or rel.endswith("mysql_compat.py"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        for i, line in enumerate(text.splitlines(), 1):
            m = create_re.search(line)
            if not m:
                continue
            table = m.group(1)
            # 误报过滤：IF 为 "CREATE TABLE IF NOT EXISTS" 字面量碎片；含 <占位符>/... 的为文档示例 DDL
            if table.lower() == "if":
                continue
            if "<" in line[m.end():m.end() + 40] or "..." in line:
                continue
            if table.lower() in LEGACY_TABLES:
                continue
            if not TABLE_PREFIX_RULE.match(table.lower()):
                findings.append({
                    "gate": "A6",
                    "severity": "Critical",
                    "file": rel,
                    "line": i,
                    "issue": f"新增表名 {table} 不符合 2.0 命名规则（主控 grape_t_xxx / 运营管理台 grape_admin_t_xxx）",
                    "evidence": line.strip()[:200],
                    "risk": "表名体系混乱，主控/管理台边界不可辨识，影响持续维护",
                    "note": "存量表在白名单内；如确属存量改名请人工复核",
                })
    return findings


# B1: 单边方言标记（翻译层 src/mysql_compat.py 未覆盖时需人工复核）
SQLITE_ONLY = re.compile(r"\bON\s+CONFLICT\b", re.I)
MYSQL_ONLY = re.compile(r"\bON\s+DUPLICATE\s+KEY\b|\bENGINE\s*=\s*InnoDB\b|\bAUTO_INCREMENT\b", re.I)


def check_sql_dialect_compat(root: Path, changed_files=None):
    """规则 2.0 B1【BLOCKED】: 新增 SQL 需同时支持 sqlite 和 mysql 两种模式。

    静态可检部分：新增 SQL 语句使用单边方言且未走翻译层（非 mysql_compat.py 内部）。
    输出为候选，需人工复核（翻译层覆盖情况以 src/mysql_compat.py 为准）。
    """
    findings = []
    if changed_files:
        files = []
        for cf in changed_files:
            p = root / cf if not os.path.isabs(cf) else Path(cf)
            if p.suffix.lower() in (".py", ".sql") and p.exists():
                files.append(p)
    else:
        files = [f for f in walk(root) if f.suffix.lower() == ".py"]
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        # 翻译层自身、测试文件与技能自身脚本跳过
        if rel.endswith("mysql_compat.py") or "/test" in rel or "conftest" in rel or ".grape/" in rel:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if SQLITE_ONLY.search(line):
                findings.append({
                    "gate": "B1",
                    "severity": "BLOCKED",
                    "file": rel,
                    "line": i,
                    "issue": "SQL 使用 SQLite 单边方言 ON CONFLICT，未确认 MySQL 兼容",
                    "evidence": line.strip()[:200],
                    "risk": "MySQL 后端执行报语法错误，破坏 sqlite/mysql 双模式",
                    "note": "需人工复核：若语句经 mysql_compat.py 翻译层覆盖则为误报",
                })
            if MYSQL_ONLY.search(line):
                findings.append({
                    "gate": "B1",
                    "severity": "BLOCKED",
                    "file": rel,
                    "line": i,
                    "issue": "SQL 使用 MySQL 单边方言（ON DUPLICATE KEY / ENGINE=InnoDB / AUTO_INCREMENT）",
                    "evidence": line.strip()[:200],
                    "risk": "SQLite 后端执行报语法错误，破坏 sqlite/mysql 双模式",
                    "note": "需人工复核：若为 MySQL 专用初始化脚本或经翻译层则为误报",
                })
    return findings


def check_i18n_keys(root: Path, changed_files=None):
    """规则 2.0 D1【BLOCKED】: 前端新增用户可见文案需中英双语 key 化。

    静态可检部分：1) 变更 JS 文件中的疑似硬编码中文文案（未走 i18n key）；
    2) locales/zh 与 locales/en 语言包 key 集合不一致。
    输出为候选，需人工复核。
    """
    findings = []
    # --- part 1: 变更 JS 中的硬编码中文文案 ---
    if changed_files:
        zh_re = re.compile(r"[\u4e00-\u9fff]{2,}")
        for cf in changed_files:
            p = root / cf if not os.path.isabs(cf) else Path(cf)
            if p.suffix.lower() not in (".js", ".vue", ".ts") or not p.exists():
                continue
            rel = str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
            if "locales" in rel or "vendor" in rel:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith(("//", "*", "/*")):
                    continue  # 注释不检查
                for m in zh_re.finditer(line):
                    # 排除 i18n key 定义（data-i18n / t('key') / I18N 等）
                    ctx = line[max(0, m.start() - 30):min(len(line), m.end() + 10)]
                    if re.search(r"data-i18n|i18n\.|I18N|t\(['\"]", ctx):
                        continue
                    findings.append({
                        "gate": "D1",
                        "severity": "BLOCKED",
                        "file": rel,
                        "line": i,
                        "issue": "前端硬编码中文文案，未走 locales 语言包 key 化",
                        "evidence": line.strip()[:200],
                        "risk": "英文环境显示中文，违反中英双语国际化要求（规则 2.0 D1）",
                        "note": "需人工复核：确认文案是否用户可见；数据/调试字符串为误报",
                    })
                    break
    # --- part 2: zh/en 语言包 key 集合一致性（对齐仓库 scripts/check-i18n-keys.py 机制）---
    # 实际结构：locales/zh/{mod}.json + locales/en/{mod}.json
    locales_dir = root / "src" / "api" / "static" / "locales"
    if locales_dir.is_dir():
        def flat_keys(obj, prefix=""):
            keys = set()
            for k, v in obj.items():
                full = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    keys |= flat_keys(v, full)
                else:
                    keys.add(full)
            return keys
        zh_dir = locales_dir / "zh"
        if zh_dir.is_dir():
            for zh_file in sorted(zh_dir.glob("*.json")):
                mod = zh_file.stem
                en_file = locales_dir / "en" / f"{mod}.json"
                if not en_file.exists():
                    findings.append({
                        "gate": "D1",
                        "severity": "BLOCKED",
                        "file": f"src/api/static/locales/en/{mod}.json",
                        "line": 0,
                        "issue": f"语言包缺 en/{mod}.json（中英双语不齐）",
                        "evidence": f"zh/{mod}.json 存在但 en/{mod}.json 缺失",
                        "risk": "英文环境缺整个模块文案",
                    })
                    continue
                try:
                    zh_keys = flat_keys(json.loads(zh_file.read_text(encoding="utf-8")))
                    en_keys = flat_keys(json.loads(en_file.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
                for k in sorted(zh_keys - en_keys):
                    findings.append({
                        "gate": "D1",
                        "severity": "BLOCKED",
                        "file": f"src/api/static/locales/en/{mod}.json",
                        "line": 0,
                        "issue": f"key `{k}` 缺英文翻译",
                        "evidence": f"zh 有而 en 无：{k}",
                        "risk": "英文环境回退显示中文 key",
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
    all_findings.extend(check_table_naming(root, changed_files))
    all_findings.extend(check_sql_dialect_compat(root, changed_files))
    all_findings.extend(check_i18n_keys(root, changed_files))
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
