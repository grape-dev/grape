#!/usr/bin/env python3
"""Grape 系统发布包打包脚本（单包全功能模式）。

产物: grape-system-<YYYYMMDDHHMM>.tar
  ├── INSTALL.md          (agent 可读安装指南)
  ├── SHA256SUMS          (相对路径校验)
  ├── backend/            (grape 后端 src + 静态资源 + configs example)
  ├── database/           (MySQL DDL + 种子数据)
  ├── skills/             (grape-skills 全量，从独立仓库读取；部署时解压到 .grape/skills/)
  ├── templates/          (grape-template 项目模板，从独立仓库读取)
  └── tools/              (worker 运行时 + install-worker.sh)

注:
  - admin 运营管理台已独立项目 (grape-dev/grape-admin)，单独打包为 grape-admin-<ts>.tar
  - skills 已独立项目 (grape-dev/grape-skills)，打包时从独立仓库读取
  - examples/templates 已独立项目 (grape-dev/grape-template)，打包时从独立仓库读取
"""
import hashlib, os, re, subprocess, sys, tarfile, io, shutil, datetime, gzip, json

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
# 输出目录可用环境变量覆盖；默认放在仓库同级目录（不在脚本里写死部署机绝对路径）
OUT_DIR = os.environ.get(
    "GRAPE_RELEASE_OUT_DIR",
    os.path.abspath(os.path.join(ROOT, os.pardir, "grape-release-out")),
)
BUCKET = "obs-grape-release"
PREFIX = "release"

# 独立项目路径（可通过环境变量覆盖）
SKILLS_REPO = os.environ.get("GRAPE_SKILLS_ROOT", "/opt/grape-skills")
TEMPLATE_REPO = os.environ.get("GRAPE_TEMPLATE_ROOT", "/opt/grape-template")

# ---- 工具函数 ----

def _sql_escape_str(v):
    """SQL 字符串字面量转义（导出种子 INSERT 用）：反斜杠与单引号。

    库内 value 含单引号/反斜杠时，未转义的 f-string 拼接会破坏导出 SQL
    语法（QA 审查 Required 发现）；所有 INSERT 值生成统一复用本函数。
    """
    return str(v).replace("\\", "\\\\").replace("'", "\\'")

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def add_to_tar(tar, src_path, arcname):
    tar.add(src_path, arcname=arcname, filter=_tar_filter)

def _tar_filter(ti):
    if "__pycache__" in ti.name or ti.name.endswith(".pyc"):
        return None
    if ".git" in ti.name:
        return None
    if ".venv" in ti.name:
        return None
    # Vue 前端依赖目录：npm ci 产物，体积大且与平台无关——打包跳过（static-dist 构建产物保留，运行时要 /static-vue）
    if "/node_modules/" in ti.name or ti.name.endswith("/node_modules"):
        return None
    # help/api/openapi.json 是生成物（服务启动时落盘，gitignore）——打包跳过
    if ti.name.endswith("help/api/openapi.json"):
        return None
    return ti

def make_subtar_gz(src_dir_or_files, out_path, base_dir=""):
    """将目录或文件列表打包成 .tar.gz"""
    with tarfile.open(out_path, "w:gz") as tar:
        if isinstance(src_dir_or_files, str):
            for item in os.listdir(src_dir_or_files):
                s = os.path.join(src_dir_or_files, item)
                a = os.path.join(base_dir, item) if base_dir else item
                tar.add(s, arcname=a, filter=_tar_filter)
        elif isinstance(src_dir_or_files, list):
            for s, a in src_dir_or_files:
                tar.add(s, arcname=a, filter=_tar_filter)

# ---- 子包构建 ----

def pack_backend(ts):
    """后端: src/ (含 api/frontend shell 前端) + scripts/ + 仓库根文件 + configs example + requirements"""
    out = os.path.join(OUT_DIR, f"staging/backend/grape-backend-{ts}.tar.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    items = []
    # packaging/ 目录：worker 安装包不是预先打好的静态文件——src/kernel/worker_package.py
    # 在【部署根】读 packaging/legacy-entry-src/ 现拼 grape-worker.tar.gz。纯解包部署
    # （没有 git 检出、只解本包）时若包内无此目录，worker 安装接口直接 500
    # 「legacy 入口 shim 源缺失」，Windows/Linux worker 全部装不上，且只在
    # data/cache/worker-package 被重建后暴露（升级首日必现、平时被旧缓存掩盖）。
    # 故这里 fail-loud，并整目录打进去（含 legacy-entry-src 全部 shim）。
    packaging_dir = os.path.join(ROOT, "packaging")
    _legacy_required = [
        "legacy-entry-src/runtime/__init__.py",
        "legacy-entry-src/runtime/agent.py",
        "legacy-entry-src/runtime/cloud_worker.py",
    ]
    if not os.path.isdir(packaging_dir):
        print(f"[FATAL] 缺少 packaging/ 目录：{packaging_dir}")
        print("        该目录承载 worker 安装包的 legacy 入口 shim，缺它 GET /api/runtime/grape-worker.tar.gz 会 500")
        sys.exit(1)
    _missing = [f for f in _legacy_required if not os.path.isfile(os.path.join(packaging_dir, f))]
    if _missing:
        print(f"[FATAL] packaging/ 缺 {len(_missing)} 个 legacy 入口 shim 源：")
        for f in _missing:
            print(f"        packaging/{f}")
        sys.exit(1)
    items.append((packaging_dir, "packaging"))
    # requirements*.txt：Issue #699 Phase 0 起依赖已并入 pyproject.toml，文件通常不存在；
    # 存在时顺手带上（老部署脚本仍有按 requirements 装依赖的路径）。
    for rf in ("requirements.txt", "requirements-worker.txt"):
        p = os.path.join(ROOT, rf)
        if os.path.isfile(p):
            items.append((p, rf))
    # src/ 目录
    src_dir = os.path.join(ROOT, "src")
    # R-2：Vue 前端产物（src/api/frontend/static-dist/）是本目录的一部分，但它由
    # `npm run build` 生成且被 .gitignore 忽略——干净 checkout/构建机必须先构建。
    # 缺它出包不会报错，只会让 shell/index.html 引用的 /static-vue/** 全部 404
    # （页面退回空壳），属静默缺件，故这里 fail-loud。
    dist_index = os.path.join(ROOT, "src", "api", "frontend", "static-dist", "index.html")
    if not os.path.isfile(dist_index):
        print("[FATAL] 缺少 Vue 前端构建产物: src/api/frontend/static-dist/index.html")
        print("        打包前请先构建：cd src/api/frontend/app && npm ci && npm run build")
        sys.exit(1)
    # N-3：不止断言 index.html —— shell/index.html 引用的每个 /static-vue/** 都必须
    # 在 static-dist 里存在，否则陈旧构建（缺新 chunk）会静默打出「半套 UI」的包。
    dist_root = os.path.join(ROOT, "src", "api", "frontend", "static-dist")
    shell_index = os.path.join(ROOT, "src", "api", "frontend", "shell", "index.html")
    if os.path.isfile(shell_index):
        with open(shell_index, encoding="utf-8") as fh:
            refs = set(re.findall(r'(?:src|href)="(/static-vue/[^"?]+)', fh.read()))
        missing = sorted(r for r in refs
                         if not os.path.isfile(os.path.join(dist_root, r[len("/static-vue/"):])))
        if missing:
            print(f"[FATAL] static-dist 缺 {len(missing)} 个被 shell 引用的产物（构建过期）：")
            for r in missing[:10]:
                print(f"        {r}")
            print("        请重新构建：cd src/api/frontend/app && npm ci && npm run build")
            sys.exit(1)
    items.append((src_dir, "src"))
    # scripts/ 目录 (install-worker.sh, sync_templates.sh, sync_skills.sh 等)
    scripts_dir = os.path.join(ROOT, "scripts")
    if os.path.isdir(scripts_dir):
        items.append((scripts_dir, "scripts"))
    # help/ 帮助文档源目录（usage + api + reference + developer markdown）
    # ⚠️ 必须打入：服务重启时 src/kernel/help_page_gen.py 依据 help/ 渲染
    #    src/api/frontend/shell/help.html；缺失则升级后文档无法再生成（一直用渲染旧内容的
    #    help.html，升级总缺新文档的根因，2026-10-12 修复）。
    #    仅 help/api/openapi.json（启动时落盘的生成物、gitignore）被 _tar_filter 跳过；
    #    shell/openapi.json 是入仓的部署即用副本，正常打入。
    help_dir = os.path.join(ROOT, "help")
    if os.path.isdir(help_dir):
        items.append((help_dir, "help"))
    # 仓库根文件 (pyproject.toml, README 等)
    # ⚠️ start-worker.sh 不打入服务端包：install-worker.sh 会按实际安装目录动态生成，
    #    打入反而可能把模板/脏文件覆盖到 worker 本地脚本（历史事故：硬编码本机绝对路径版）。
    # Issue #699 Phase 0: requirements*.txt merged into pyproject.toml;
    #   Dockerfile/docker-compose.yml/nginx moved to deploy/
    for rf in ["pyproject.toml", "Makefile",
               "README.md", "README.zh.md", ".gitignore"]:
        p = os.path.join(ROOT, rf)
        if os.path.isfile(p):
            items.append((p, rf))
    # deploy/ (Dockerfile, docker-compose.yml, nginx-grape.conf, alerts.yml)
    deploy_dir = os.path.join(ROOT, "deploy")
    if os.path.isdir(deploy_dir):
        items.append((deploy_dir, "deploy"))
    # configs (只保留 *.example.json + 种子 json)
    # 2026-10-14 账号体系重构补充：security / oauth-providers / email 三个 example 模板——
    # 账号系统（登录/OAuth/凭据加密/邮件验证）依赖 security.json + oauth-providers.json，
    # 缺模板则新装环境无从配置（服务端读不到即全部走默认开发模式，生产不可用）。
    config_keep = [
        "agent-description.json", "agent-skill-mapping.json",
        "skill-description.json", "project-templates.json", "pipelines.json",
        "db-backend.example.json", "global-bot-token.example.json",
        "login-whitelist.example.json", "engine-selection.example.json",
        "server-config.example.json",
        "security.example.json", "oauth-providers.example.json", "email.example.json",
    ]
    for cf in config_keep:
        p = os.path.join(ROOT, "configs", cf)
        if os.path.isfile(p):
            items.append((p, f"configs/{cf}"))
    # pyproject.toml (顶层也放一份; Issue #699 Phase 0: replaces requirements*.txt)
    pt = os.path.join(ROOT, "pyproject.toml")
    if os.path.isfile(pt):
        items.append((pt, "pyproject.toml"))
    # Dockerfile (Issue #699 Phase 0: moved to deploy/)
    df = os.path.join(ROOT, "deploy", "Dockerfile")
    if os.path.isfile(df):
        items.append((df, "Dockerfile"))
    make_subtar_gz(items, out)
    return out

# 种子表 — 导出 DDL + 数据
# 2026-10-14 账号体系/场景/模板补充：scene_category + skill_category（场景/技能市场分类，
# 缺失则市场页空）、agent_type_runtime_pool（引擎池映射）、featured_templates（精选模板）、
# meta 仅 execution_mode/pipeline_mode 两个系统键（运行模式，其余为环境私有数据不入包）。
# 只对 mgmt 域生效：runtime/insight 域只导 DDL，避免把某个环境的运行数据带进发布包。
SEED_TABLES = [
    "admin_users", "admin_roles", "admin_permission_menus", "admin_role_menu_rel",
    "agent_type", "skill", "event_route_config", "global_im_bot_config",
    "flyway_schema_history",
    "scene_category", "skill_category", "agent_type_runtime_pool",
    "featured_templates",
]

def _parse_dsn(dsn):
    """解析 mysql+pymysql://user:pass@host:port/db（也认 mysql://）为连接参数。"""
    from urllib.parse import unquote
    m = re.match(r"^mysql(?:\+pymysql)?://([^:/@]+)(?::([^@]*))?@([^:/]+)(?::(\d+))?/([^?/]+)", dsn or "")
    if not m:
        raise ValueError(f"无法解析 DSN（期望 mysql+pymysql://user:pass@host:3306/db）：{dsn!r}")
    user, password, host, port, database = m.groups()
    return {
        "host": host,
        "port": int(port or 3306),
        "user": unquote(user),
        "password": unquote(password or ""),
        "database": database,
    }

# 连接超时：复用项目既有约定 src/kernel/mysql_compat.py 的 connect_timeout=10（该文件 4 处一致），
# 只用于打包时把「域实例不可达」快速暴露；dev build-007 实测三域连接 7.8~10ms（同 VPC，3 次采样），
# 10s 约为实测 p100 的 1000 倍，纯粹是失败上限而非正常路径预算。可用环境变量覆盖。
DB_CONNECT_TIMEOUT = int(os.environ.get("GRAPE_PACK_DB_CONNECT_TIMEOUT", "10"))

def _ownership_map():
    """读 configs/table_ownership.json（唯一权威）：表名 -> 归属域。"""
    p = os.path.join(ROOT, "configs", "table_ownership.json")
    if not os.path.isfile(p):
        return {}
    try:
        return json.load(open(p)).get("tables", {}) or {}
    except Exception as e:
        print(f"[WARN] table_ownership.json 解析失败（按 mgmt 归属处理）：{e}")
        return {}

def _seed_tables_for(dom, ownership):
    """种子表按 owner 域分发（A11 口径）；归属缺失的表按 mgmt。"""
    return [t for t in SEED_TABLES if ownership.get(t, "mgmt") == dom]

def _mask_host(h):
    """日志/文件头里不打完整内网地址（合规审查 §1.1-1-5）。"""
    parts = str(h).split(".")
    return parts[0] + ".x.x.x" if len(parts) == 4 else "***"

def _three_db_cfgs():
    """三库模式：从运行期同一套 DSN 环境变量取（/etc/grape/grape-3db.env 的 GRAPE_DSN_*）。

    三库是 2.3.x 的部署事实（mgmt/runtime/insight 可分三台实例），发布包必须能分别导出；
    未设置时返回空 dict，走老的 configs/db-backend.json 单库路径。
    """
    cfgs = {}
    for dom in ("mgmt", "runtime", "insight"):
        dsn = os.environ.get(f"GRAPE_DSN_{dom.upper()}", "").strip()
        if dsn:
            cfg = _parse_dsn(dsn)
            cfg["domain"] = dom
            cfg["database"] = os.environ.get(f"GRAPE_DB_NAME_{dom.upper()}", cfg["database"]) or cfg["database"]
            cfgs[dom] = cfg
    return cfgs

def _export_domain(cfg, out, ts, seed_tables, meta_seed=False, views_out=None):
    """导出单个域库：全表 DDL + 指定种子表数据（其余只导结构）。"""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 3306))
    user, password = cfg.get("user", "root"), cfg.get("password", "")
    database = cfg.get("database") or cfg.get("name") or "grape-dev-db"
    domain = cfg.get("domain", "mgmt")

    import pymysql
    try:
        conn = pymysql.connect(host=host, port=port, user=user, password=password,
                               database=database, charset="utf8mb4", connect_timeout=DB_CONNECT_TIMEOUT)
    except Exception as e:
        print(f"[FATAL] {domain} 域连不上 MySQL: {user}@{host}:{port}/{database} -> {e}")
        sys.exit(1)
    cur = conn.cursor()

    if seed_tables is None:
        seed_tables = []
    # 机密表 — 只导 DDL, 不导数据
    # 2026-10-14 账号体系重构补充：grape_accounts / external_identities（D32 账号核心表）、
    # api_tokens / session_api_tokens / grape_t_task_proxy_tokens（会话与 API 凭据）、
    # verification_codes / login_failures / exchange_codes（注册验证与登录安全）、
    # grape_t_user_llm_credentials（用户模型凭据）。全部含用户数据/密钥，只导结构。
    schema_only_tables = [
        "user_accounts", "user_credentials", "runtime_credentials", "managed_repos",
        "oauth_states", "repo_platform_tokens", "user_vms", "user_vm_repos",
        "unified_user_mapping", "repo_im_group_binding",
        "grape_accounts", "external_identities",
        "api_tokens", "session_api_tokens", "grape_t_task_proxy_tokens",
        "verification_codes", "login_failures", "exchange_codes",
        "grape_t_user_llm_credentials", "user_repos",
    ]
    # 运行时大表 — 只导 DDL
    runtime_tables = [
        "events", "event_thread", "event_skipped", "agent_tasks", "agent_runtimes",
        "task_messages", "issue_state", "devops_agent_runtime_binding",
        "devops_agent_session", "issue_agent_session", "chat_conversations",
        "chat_messages", "chat_agent_session", "agent_shared_knowledge",
        "bot_seen_messages", "backup_agent_runtimes_revoked_20260817",
        "backup_devops_agent_runtime_binding_20260817",
        "backup_devops_agent_runtime_binding_20260827_234600",
        "backup_devops_agent_session_20260817",
        "backup_runtime_credentials_revoked_20260817",
        "vod_conversations", "feishu_onboard_task",
        "admin_agent_alert", "admin_ai_chat_message", "admin_ai_chat_session",
        "admin_ai_history", "admin_ai_template", "admin_audit_log",
    ]
    # engine_capabilities 也只导 DDL
    schema_only_tables.append("engine_capabilities")
    schema_only_tables.append("agent_type_runtime_pool")

    # 2026-10-14：meta 表系统键种子（execution_mode/pipeline_mode）——运行模式决定
    # plugin/legacy 广播行为，新装环境缺这两个键会回落默认值（与发布验证环境不一致）。
    # meta 其余键为环境私有（会话指针等），不随包分发。

    lines = []
    lines.append(f"-- Grape Database Export {ts} (domain: {domain}, schema: {database})")
    lines.append(f"-- Generated: {datetime.datetime.now().isoformat()}")
    lines.append("SET FOREIGN_KEY_CHECKS=0;")
    lines.append("SET NAMES utf8mb4;")
    lines.append("")

    # 只枚举【基表】：SHOW TABLES 会把跨域视图一起列出来（mysql 8 实测），对视图发
    # DROP TABLE 会 1347「is not BASE TABLE」，在 CREATE VIEW 前写 DROP TABLE 还会让
    # 空库导入撞 1146（视图引用的他域基表尚未导入）。视图另存一份 *-views.sql.gz。
    cur.execute("SHOW FULL TABLES WHERE Table_type='BASE TABLE'")
    db_tables = [r[0] for r in cur.fetchall()]
    cur.execute("SHOW FULL TABLES WHERE Table_type='VIEW'")
    db_views = [r[0] for r in cur.fetchall()]
    _known = list(seed_tables) + schema_only_tables + runtime_tables
    # 去重（agent_type_runtime_pool 同时出现在种子表与 schema_only 清单里）
    seen = set()
    all_tables = []
    for t in [t for t in _known if t in db_tables] + db_tables:
        if t not in seen:
            seen.add(t)
            all_tables.append(t)

    for t in all_tables:
        # DDL
        cur.execute(f"SHOW CREATE TABLE `{t}`")
        row = cur.fetchone()
        if row and row[1]:
            lines.append(f"-- Table: {t}")
            lines.append(f"DROP TABLE IF EXISTS `{t}`;")
            lines.append(row[1] + ";")
            lines.append("")
        # 数据（仅种子表）
        if t in seed_tables:
            cur.execute(f"SELECT * FROM `{t}`")
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                col_str = ", ".join(f"`{c}`" for c in cols)
                for r in rows:
                    vals = []
                    for v in r:
                        if v is None:
                            vals.append("NULL")
                        elif isinstance(v, (int, float)):
                            vals.append(str(v))
                        elif isinstance(v, bytes):
                            vals.append(f"0x{v.hex()}")
                        else:
                            escaped = _sql_escape_str(v)
                            vals.append(f"'{escaped}'")
                    lines.append(f"INSERT INTO `{t}` ({col_str}) VALUES ({', '.join(vals)});")
                lines.append("")

    # 2026-10-14：meta 系统键种子（execution_mode / pipeline_mode）——从当前库读取，
    # 缺失时用代码默认（plugin + auto）。保证新装环境与发布验证环境的运行模式一致。
    # 仅 mgmt 域写入：runtime/insight 的 meta（若存在）不是运行模式的真源。
    if meta_seed:
        try:
            _mode_defaults = {"execution_mode": "auto", "pipeline_mode": "plugin"}
            for _mk, _default in _mode_defaults.items():
                cur.execute("SELECT `value` FROM meta WHERE `key`=%s", (_mk,))
                _mr = cur.fetchone()
                _mv = (_mr[0] if _mr and _mr[0] else _default)
                _mv_escaped = _sql_escape_str(_mv)
                lines.append(f"INSERT INTO `meta` (`key`, `value`) VALUES ('{_mk}', '{_mv_escaped}');")
            lines.append("")
        except Exception as _me:
            # meta 表缺失等场景：跳过（db_setup 启动时会建表，运行模式回落代码默认）
            pass

    lines.append("SET FOREIGN_KEY_CHECKS=1;")

    # 视图单独导出（不混在基表 SQL 里）：跨域视图引用他域基表，三份基表库都导完之后才能建，
    # 所以单列 *-views.sql.gz；用 DROP VIEW（不是 DROP TABLE），且绝不对视图生成 INSERT。
    v_sql = None
    if views_out and db_views:
        vlines = [f"-- Grape View Export {ts} (domain: {domain}, schema: {database})",
                  "-- 导入建议：mysql -f（遇错继续）+ 重跑一次（幂等）；跨域视图互引时以 scripts/sync_domain_views.py 为准。",
                  "SET FOREIGN_KEY_CHECKS=0;",
                  "SET NAMES utf8mb4;", ""]
        for v in db_views:
            cur.execute(f"SHOW CREATE VIEW `{v}`")
            row = cur.fetchone()
            if row and row[1]:
                # 去掉 DEFINER=`user`@`host`：口令无关，但会把打包机的内部账号/IP 带进发布包，
                # 且换环境后该账号多半不存在（导入时 DEFINER 需要 SUPER/SET_USER_ID 权限）。
                stmt = re.sub(r"DEFINER=`[^`]*`@`[^`]*`", "", row[1]).lstrip()
                vlines.append(f"DROP VIEW IF EXISTS `{v}`;")
                vlines.append(stmt + ";")
                vlines.append("")
        vlines.append("SET FOREIGN_KEY_CHECKS=1;")
        v_sql = "\n".join(vlines)
    conn.close()

    with gzip.open(out, "wb") as f:
        f.write("\n".join(lines).encode("utf-8"))
    if v_sql is not None:
        os.makedirs(os.path.dirname(views_out), exist_ok=True)
        with gzip.open(views_out, "wb") as f:
            f.write(v_sql.encode("utf-8"))
    return out, (views_out if v_sql is not None else None)

def pack_database(ts):
    """数据库子包：单库（老布局）或三库（2.3.x，mgmt/runtime/insight 各一份）。

    三库模式下沿用运行期的 DSN 环境变量（source /etc/grape/grape-3db.env 即可），
    文件名带域名后缀：grape-db-<ts>-{mgmt,runtime,insight}.sql.gz；同时复制一份
    grape-db-<ts>.sql.gz（= mgmt），让老 INSTALL/解包习惯与新三库部署都能用。
    种子数据只进 mgmt，runtime/insight 只导 DDL。
    """
    os.makedirs(os.path.join(OUT_DIR, "staging/database"), exist_ok=True)
    three = _three_db_cfgs()
    if three:
        outs = []
        ownership = _ownership_map()
        for dom in ("mgmt", "runtime", "insight"):
            cfg = three.get(dom)
            if not cfg:
                print(f"[FATAL] 三库模式缺 {dom} 域 DSN（GRAPE_DSN_{dom.upper()}）——"
                      f"拒绝打出只带部分库的包；请 source /etc/grape/grape-3db.env")
                sys.exit(1)
            out = os.path.join(OUT_DIR, f"staging/database/grape-db-{ts}-{dom}.sql.gz")
            views_out = os.path.join(OUT_DIR, f"staging/database/grape-db-{ts}-{dom}-views.sql.gz")
            seeds = _seed_tables_for(dom, ownership)
            _, vfile = _export_domain(cfg, out, ts, seeds, meta_seed=(ownership.get("meta", "mgmt") == dom),
                                      views_out=views_out)
            outs.append(out)
            if vfile:
                outs.append(vfile)
            print(f"     {dom}: {cfg['user']}@{_mask_host(cfg['host'])}/{cfg['database']} "
                  f"-> {os.path.basename(out)} ({os.path.getsize(out) // 1024}KB)"
                  + (f" + views ({os.path.getsize(vfile) // 1024}KB)" if vfile else "")
                  + f"  种子表 {len(seeds)}")
        alias = os.path.join(OUT_DIR, f"staging/database/grape-db-{ts}.sql.gz")
        mgmt_dump = os.path.join(OUT_DIR, f"staging/database/grape-db-{ts}-mgmt.sql.gz")
        shutil.copyfile(mgmt_dump, alias)
        return [alias] + outs

    cfg_path = os.path.join(ROOT, "configs/db-backend.json")
    if not os.path.isfile(cfg_path):
        print(f"[FATAL] 既没有 GRAPE_DSN_* 三库 DSN，也没有 {cfg_path}")
        sys.exit(1)
    mysql = json.load(open(cfg_path)).get("mysql", {})
    cfg = {
        "host": mysql.get("host", "127.0.0.1"),
        "port": mysql.get("port", 3306),
        "user": mysql.get("user", "root"),
        "password": mysql.get("password", ""),
        "database": mysql.get("database", "grape-dev-db"),
        "domain": "mgmt",
    }
    out = os.path.join(OUT_DIR, f"staging/database/grape-db-{ts}.sql.gz")
    print("[WARN] 单库模式（未设置 GRAPE_DSN_MGMT/RUNTIME/INSIGHT）：只导出主库；"
          "2.3.x 三库部署请先 source /etc/grape/grape-3db.env 再打包")
    views_out = os.path.join(OUT_DIR, f"staging/database/grape-db-{ts}-views.sql.gz")
    _, vfile = _export_domain(cfg, out, ts, SEED_TABLES, meta_seed=True, views_out=views_out)
    return [out] + ([vfile] if vfile else [])

def pack_skills(ts):
    """skills: 从独立仓库 grape-dev/grape-skills 读取全量（skills + scenes）"""
    out = os.path.join(OUT_DIR, f"staging/skills/grape-skills-{ts}.tar.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    skills_dir = os.path.join(SKILLS_REPO, "skills")
    scenes_dir = os.path.join(SKILLS_REPO, "scenes")
    if not os.path.isdir(skills_dir):
        print(f"[WARN] skills 目录不存在 ({SKILLS_REPO}/skills)，跳过")
        return None
    # Issue #699 Phase 0: skills → .grape/skills/, scenes → configs/scenes/
    items = []
    if os.path.isdir(skills_dir):
        for item in os.listdir(skills_dir):
            s = os.path.join(skills_dir, item)
            a = os.path.join(".grape", "skills", item)
            items.append((s, a))
    if os.path.isdir(scenes_dir):
        for item in os.listdir(scenes_dir):
            s = os.path.join(scenes_dir, item)
            a = os.path.join("configs", "scenes", item)
            items.append((s, a))
    else:
        print(f"[WARN] scenes 目录不存在 ({SKILLS_REPO}/scenes)，仅打包 skills")
    make_subtar_gz(items, out)
    return out

def pack_templates(ts):
    """templates: 从独立仓库 grape-dev/grape-template 读取项目模板"""
    out = os.path.join(OUT_DIR, f"staging/templates/grape-templates-{ts}.tar.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if not os.path.isdir(TEMPLATE_REPO):
        print(f"[WARN] template 目录不存在 ({TEMPLATE_REPO})，跳过")
        return None
    make_subtar_gz(TEMPLATE_REPO, out, base_dir="templates")
    return out

def pack_tools(ts):
    """worker 运行时: src/domains/runtime/engine + src/domains/runtime/agents + install-worker.sh + pyproject.toml"""
    out = os.path.join(OUT_DIR, f"staging/tools/grape-toolkit-{ts}.tar.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    items = []
    # worker 运行闭包：S112 根治——由 tools_manifest_files() 从
    # src.kernel.worker_package.MANIFEST 派生（字面量块与 MANIFEST 漂移会漏目录，
    # 曾导致 release 包缺 src/domains/runtime/vcs/，worker 启动即 ModuleNotFoundError）
    for f in tools_manifest_files():
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            items.append((p, f))
        elif os.path.isdir(p):
            items.append((p, f))  # 目录项整目录打包（只 isfile 会静默跳过）
    # 整目录
    # Issue #2010: 原顶层模块的 canonical 实现都在 src/kernel/，旧路径（src.config 等）
    # 由 src/__init__.py 的别名一跳解析到这里；与 worker MANIFEST 目录粒度一致。
    for d in ["src/kernel", "src/domains/runtime/agents", "src/domains/runtime/engine"]:
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            items.append((p, d))
    # pyproject.toml (Issue #699 Phase 0: replaces requirements-worker.txt)
    pt = os.path.join(ROOT, "pyproject.toml")
    if os.path.isfile(pt):
        items.append((pt, "pyproject.toml"))
    # install-worker.sh
    iw = os.path.join(ROOT, "scripts/install-worker.sh")
    if os.path.isfile(iw):
        items.append((iw, "install-worker.sh"))
    make_subtar_gz(items, out)
    return out

def gen_install_md(ts, sub_pkgs):
    """生成 agent 可读 INSTALL.md"""
    git_ver = ""
    try:
        r = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            git_ver = r.stdout.strip()
    except Exception:
        pass

    pkg_name = f"grape-system-{ts}.tar"
    obs_base = f"obs://obs-grape-release/{PREFIX}"

    md = f"""# Grape 系统安装指南 (agent 可读)

> 包版本: {ts} (git: {git_ver})
> 包名: {pkg_name}
> OBS 下载: {obs_base}/{pkg_name}

## 1. 部署拓扑

Grape 是 Server-Worker 架构:

```
Grape Server (1 台, 部署全部服务端组件)
  ├── grape-main      (uvicorn src.api.main:app, :8000)
  ├── grape-webhook   (uvicorn src.api.run_webhook:app, :8080)
  ├── MySQL            (RDS 或本地, :3306)
  └── nginx            (:80/:443 TLS, 反代 main+webhook)

Remote Workers (N 台, 仅装 worker 运行时)
  └── runtime-agent   (python -m src.domains.runtime.engine.agent)

注: grape-admin 运营管理台为独立项目，单独部署
```

## 2. 前置条件

- OS: Linux aarch64/x86_64 (推荐 Huawei Cloud EulerOS/openEuler)
- Python: >= 3.10 (推荐 3.12, 用 uv 安装)
- MySQL: >= 8.0
- nginx (可选, 生产推荐)
- 工具: curl, git, uv (Python 包管理)
- Hermes Agent: `pip install hermes-agent[acp]` (含 ACP 支持, 装完 `hermes acp --check` 验证)

## 3. 安装步骤 (服务端)

### 3.1 下载解压

```bash
# 用 obsutil 下载 (桶私有, 需凭据)
obsutil cp {obs_base}/{pkg_name} .
# 或 wget (如果前缀已开放公网读)
# wget https://obs-grape-release.obs.cn-north-4.myhuaweicloud.com/{PREFIX}/{pkg_name}

tar xf {pkg_name}
cd grape-system-{ts}/
```

### 3.2 安装后端 (grape-main + grape-webhook)

```bash
# 解压后端子包
mkdir -p /opt/grape && cd /opt/grape
tar xzf /path/to/backend/grape-backend-{ts}.tar.gz

# Python 环境
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
uv venv --python 3.12 --clear .venv
uv pip install --python .venv/bin/python3 -e ".[all]"

# 数据目录
mkdir -p data

# 配置 (复制 example, 填入真实凭据)
cp configs/db-backend.example.json configs/db-backend.json
cp configs/global-bot-token.example.json configs/global-bot-token.json
cp configs/login-whitelist.example.json configs/login-whitelist.json
# 账号体系（2026-10 重构后必须配置，缺省=开发模式默认密钥，生产不安全）:
cp configs/security.example.json configs/security.json
cp configs/oauth-providers.example.json configs/oauth-providers.json
cp configs/email.example.json configs/email.json
# 编辑以上文件:
#   - db-backend.json: MySQL 连接
#   - global-bot-token.json: GitCode/GitHub bot token + OAuth client 凭据
#   - security.json: env=production + JWT 密钥池 + 凭据加密密钥（生产必改，否则启动安全检查告警）
#   - oauth-providers.json: GitCode/GitHub OAuth client_id/secret（账号登录依赖）
#   - email.json: SMTP/Resend（注册邮箱验证码发送）
#   - login-whitelist.json: 登录白名单（按需）
```

### 3.3 导入数据库

```bash
# 解压数据库包
gunzip -k /path/to/database/grape-db-{ts}.sql.gz

# 建库
mysql -h<HOST> -u<USER> -p -e "CREATE DATABASE IF NOT EXISTS <DB> CHARACTER SET utf8mb4;"

# 导入
mysql -h<HOST> -u<USER> -p <DB> < grape-db-{ts}.sql
```

> **2.3.x 三库（mgmt/runtime/insight 分实例）**：包内可能另带
> `database/grape-db-{ts}-runtime.sql.gz` 与 `database/grape-db-{ts}-insight.sql.gz`，
> 分别导入 runtime 实例的 runtime 库、insight 实例的 insight 库；`grape-db-{ts}.sql.gz` 恒等于 mgmt 域。
> 各域种子数据按 `configs/table_ownership.json` 落到 owner 库（`skill`/`agent_type`/`scene_category`/
> `global_im_bot_config`/`agent_type_runtime_pool` 属 runtime，`meta` 运行模式键也在 runtime）。
> 三库 DSN 写进 `/etc/grape/grape-3db.env`。
>
> **含视图的域**（生产三库共 100+ 个跨域视图）另出一份 `database/grape-db-{ts}-<domain>-views.sql.gz`：
> 视图引用他域基表，**必须三份基表库全部导完之后再导对应的 views 文件**，建议用 `mysql -f`（遇错继续）
> 并重跑一遍（文件自身 `DROP VIEW IF EXISTS`，幂等），最终以 `information_schema.views` 计数核对；
> 跨域视图互相引用时以 `scripts/sync_domain_views.py` 重建为准。基表 dump 里不会出现 `DROP TABLE` 打在视图上的情况。

### 3.4 安装 Skills

```bash
# 解压 skills + scenes 到 grape 仓库根目录
cd /opt/grape
tar xzf /path/to/skills/grape-skills-{ts}.tar.gz
# .grape/skills/ 目录覆盖到 /opt/grape/.grape/skills/
# configs/scenes/ 目录覆盖到 /opt/grape/configs/scenes/
```

### 3.5 systemd 服务

```bash
# grape-main.service
cat > /etc/systemd/system/grape-main.service << 'EOF'
[Unit]
Description=Grape Main API
After=network.target

[Service]
User=root
WorkingDirectory=/opt/grape
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/grape/grape.env
ExecStart=/opt/grape/.venv/bin/uvicorn src.api.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# grape-webhook.service
cat > /etc/systemd/system/grape-webhook.service << 'EOF'
[Unit]
Description=Grape Webhook
After=network.target

[Service]
User=root
WorkingDirectory=/opt/grape
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/grape/grape.env
ExecStart=/opt/grape/.venv/bin/uvicorn src.api.run_webhook:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now grape-main grape-webhook
```

### 3.6 验证

```bash
curl -s http://127.0.0.1:8000/health    # {{"status":"ok"}}
curl -s http://127.0.0.1:8080/health    # {{"status":"ok","service":"webhook"}}
systemctl is-active grape-main grape-webhook
```

## 4. Worker 安装 (远程机器)

Worker 机只需安装运行时子包:

```bash
# 从主控一键安装 (推荐)
curl -fsSL https://<grape主控域名>/api/runtime/install | bash -s -- --token <rta_...> --server https://<grape主控域名>

# 或手动安装 tools 子包
tar xzf /path/to/tools/grape-toolkit-{ts}.tar.gz -C /opt/grape
cd /opt/grape
python3.12 -m venv .venv
.venv/bin/pip install aiohttp bcrypt click httpx
bash install-worker.sh --token <rta_...> --server https://<grape主控域名>
```

## 5. 包内容清单

| 子包 | 说明 | 部署位置 |
|------|------|----------|
| backend/grape-backend-{ts}.tar.gz | 后端代码(src/api + static) + configs | /opt/grape/ |
| database/grape-db-{ts}.sql.gz | MySQL DDL + 种子数据（= mgmt 域） | MySQL |
| database/grape-db-{ts}-runtime.sql.gz / grape-db-{ts}-insight.sql.gz | 三库模式下的另两域（只导 DDL） | 各自实例 |
| skills/grape-skills-{ts}.tar.gz | 官方技能 (60+ skills) + 官方场景 (8 scenes，独立仓库) | /opt/grape/.grape/skills/ + /opt/grape/configs/scenes/ |
| templates/grape-templates-{ts}.tar.gz | 项目模板 (独立仓库) | /opt/grape/examples/ |
| tools/grape-toolkit-{ts}.tar.gz | worker 运行时 + 注册脚本 | /opt/grape/ (worker 机) |

注: admin 运营管理台 (grape-admin) 为独立项目，单独打包部署

## 6. 配置文件 (需现场填写, 不在包内)

| 文件 | 位置 | 说明 |
|------|------|------|
| db-backend.json | /opt/grape/configs/ | MySQL 连接信息 |
| global-bot-token.json | /opt/grape/configs/ | GitCode API token + webhook 配置 |
| login-whitelist.json | /opt/grape/configs/ | 登录白名单 |
| grape.env | /etc/grape/ | 环境变量 (模型 API key 等) |
| admin.env | /etc/grape/ | admin 后端配置 |
"""
    return md

# ---- 主流程 ----

def main():
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M")
    if len(sys.argv) > 1 and sys.argv[1] == "--ts":
        ts = sys.argv[2]

    skip_upload = "--skip-upload" in sys.argv
    prefix = "release"
    if "--prefix" in sys.argv:
        prefix = sys.argv[sys.argv.index("--prefix") + 1]

    print(f"[pack] 时间戳: {ts}")
    print(f"[pack] 前缀: {prefix}")
    os.makedirs(os.path.join(OUT_DIR, "staging"), exist_ok=True)

    # 1. 构建子包
    print("[pack] 1/5 构建后端子包...")
    backend_pkg = pack_backend(ts)
    print(f"  -> {backend_pkg} ({os.path.getsize(backend_pkg) // 1024}KB)")

    print("[pack] 2/5 构建数据库子包...")
    db_pkg = pack_database(ts)
    for _db in (db_pkg if isinstance(db_pkg, (list, tuple)) else [db_pkg]):
        print(f"  -> {_db} ({os.path.getsize(_db) // 1024}KB)")

    print("[pack] 3/5 构建 skills 子包...")
    skills_pkg = pack_skills(ts)
    if skills_pkg:
        print(f"  -> {skills_pkg} ({os.path.getsize(skills_pkg) // 1024}KB)")

    print("[pack] 4/5 构建 templates 子包...")
    templates_pkg = pack_templates(ts)
    if templates_pkg:
        print(f"  -> {templates_pkg} ({os.path.getsize(templates_pkg) // 1024}KB)")

    print("[pack] 5/5 构建 worker toolkit 子包...")
    tools_pkg = pack_tools(ts)
    print(f"  -> {tools_pkg} ({os.path.getsize(tools_pkg) // 1024}KB)")

    # 2. 收集子包（admin/skills/templates 已独立项目）
    sub_pkgs = {
        "backend": backend_pkg,
        "database": db_pkg,
        "skills": skills_pkg,
        "templates": templates_pkg,
        "tools": tools_pkg,
    }

    # 3. 生成 INSTALL.md
    install_md = gen_install_md(ts, sub_pkgs)
    install_md_path = os.path.join(OUT_DIR, "staging", "INSTALL.md")
    with open(install_md_path, "w") as f:
        f.write(install_md)
    print(f"[pack] INSTALL.md -> {install_md_path}")

    # 4. 生成 SHA256SUMS + 打外层 tar
    pkg_name = f"grape-system-{ts}.tar"
    pkg_path = os.path.join(OUT_DIR, pkg_name)
    pkg_dir = f"grape-system-{ts}"

    print(f"[pack] 打包 {pkg_name}...")
    sha_lines = []
    with tarfile.open(pkg_path, "w") as tar:
        # INSTALL.md
        tar.add(install_md_path, arcname=f"{pkg_dir}/INSTALL.md")
        sha_lines.append(f"{sha256_file(install_md_path)}  {pkg_dir}/INSTALL.md")
        # 子包
        for subdir, pkg_file in sub_pkgs.items():
            pkg_files = pkg_file if isinstance(pkg_file, (list, tuple)) else [pkg_file]
            for _pf in pkg_files:
                if not (_pf and os.path.isfile(_pf)):
                    continue
                arcname = f"{pkg_dir}/{subdir}/{os.path.basename(_pf)}"
                tar.add(_pf, arcname=arcname)
                rel = f"{pkg_dir}/{subdir}/{os.path.basename(_pf)}"
                sha_lines.append(f"{sha256_file(_pf)}  {rel}")
        # SHA256SUMS
        sha_content = "\n".join(sorted(sha_lines)) + "\n"
        sha_path = os.path.join(OUT_DIR, "staging", "SHA256SUMS")
        with open(sha_path, "w") as f:
            f.write(sha_content)
        tar.add(sha_path, arcname=f"{pkg_dir}/SHA256SUMS")

    pkg_size = os.path.getsize(pkg_path) // 1024 // 1024
    print(f"[pack] 产物: {pkg_path} ({pkg_size}MB)")

    # 5. 自检
    print("[pack] 自检...")
    with tarfile.open(pkg_path) as tar:
        names = tar.getnames()
    _db_files = [p for p in (db_pkg if isinstance(db_pkg, (list, tuple)) else [db_pkg]) if p]
    _db_in_tar = [n for n in names if "/database/grape-db-" in n]
    # 基表 dump 里混进 CREATE VIEW = 视图被当基表导出（会对视图写 DROP TABLE → 导入 1347/1146）
    _views_leak = []
    for _f in _db_files:
        if _f.endswith("-views.sql.gz"):
            continue
        try:
            with gzip.open(_f, "rb") as fh:
                if b" VIEW `" in fh.read():
                    _views_leak.append(os.path.basename(_f))
        except Exception:
            pass
    checks = {
        "INSTALL.md": any("INSTALL.md" in n for n in names),
        "database(files=%d/%d)" % (len(_db_in_tar), len(_db_files)): len(_db_in_tar) == len(_db_files) and len(_db_files) > 0,
        "db-base-only(no views mixed in)": not _views_leak,
        "SHA256SUMS": any("SHA256SUMS" in n for n in names),
        "backend": any("backend/" in n for n in names),
        "database": any("database/" in n for n in names),
        "skills": any("skills/" in n for n in names),
        "templates": any("templates/" in n for n in names),
        "tools": any("tools/" in n for n in names),
    }
    for k, v in checks.items():
        status = "OK" if v else "MISSING"
        print(f"  {k}: {status}")
    if not all(checks.values()):
        print("[pack] 自检失败! 缺少子包")
        sys.exit(1)
    print("[pack] 自检通过 ✓")

    # 6. 上传
    if skip_upload:
        print("[pack] --skip-upload, 跳过上传")
        return

    print(f"[pack] 上传到 obs://obs-grape-release/{prefix}/{pkg_name}...")
    # 凭据不在代码里（2026-10-21 起）：读环境变量或非入库配置，见 scripts/obs_credentials.py
    # 与守卫 scripts/check-secrets.py（禁止把 AK/SK 明文写回本文件）。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from obs_credentials import release_obs_credentials
        _ak, _sk = release_obs_credentials()
    except RuntimeError as e:
        print(f"[pack] 上传凭据缺失，终止：\n{e}")
        sys.exit(1)
    r = subprocess.run([
        "obsutil", "cp", pkg_path,
        f"obs://obs-grape-release/{prefix}/{pkg_name}", "-f",
        f"-i={_ak}",
        f"-k={_sk}",
        "-e=obs.cn-north-4.myhuaweicloud.com",
    ], capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        print(f"[pack] 上传成功 ✓")
        print(f"[pack] 验证: obsutil ls obs://obs-grape-release/{prefix}/ -s")
    else:
        print(f"[pack] 上传失败: {r.stderr}")
        sys.exit(1)

def tools_manifest_files():
    """worker 运行闭包清单：从 src.kernel.worker_package.MANIFEST 派生（S112）。

    脚本方式运行时 sys.path[0]=scripts/，直接 import 会 ModuleNotFoundError，
    因此先把仓库根放进 sys.path（发布链路没有统一 PYTHONPATH 约定）。
    """
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from src.kernel.worker_package import MANIFEST
    return [x for x in MANIFEST if x != "requirements-worker.txt"]


if __name__ == "__main__":
    main()
