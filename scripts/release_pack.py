#!/usr/bin/env python3
"""Grape 系统发布包打包脚本（单包全功能模式）。

产物: grape-system-<YYYYMMDDHHMM>.tar
  ├── INSTALL.md          (agent 可读安装指南)
  ├── SHA256SUMS          (相对路径校验)
  ├── backend/            (grape 后端 src + 静态资源 + configs example)
  ├── database/           (MySQL DDL + 种子数据)
  ├── skills/             (grape-skills 全量，从独立仓库读取)
  ├── templates/          (grape-template 项目模板，从独立仓库读取)
  └── tools/              (worker 运行时 + install-worker.sh)

注:
  - admin 运营管理台已独立项目 (grape-dev/grape-admin)，单独打包为 grape-admin-<ts>.tar
  - skills 已独立项目 (grape-dev/grape-skills)，打包时从独立仓库读取
  - examples/templates 已独立项目 (grape-dev/grape-template)，打包时从独立仓库读取
"""
import hashlib, os, subprocess, sys, tarfile, io, shutil, datetime, gzip, json

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
OUT_DIR = "/root/release"
BUCKET = "obs-grape-release"
PREFIX = "release"

# 独立项目路径（可通过环境变量覆盖）
SKILLS_REPO = os.environ.get("GRAPE_SKILLS_ROOT", "/opt/grape-skills")
TEMPLATE_REPO = os.environ.get("GRAPE_TEMPLATE_ROOT", "/opt/grape-template")

# ---- 工具函数 ----

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
    """后端: src/ (含 api/static) + scripts/ + 仓库根文件 + configs example + requirements"""
    out = os.path.join(OUT_DIR, f"staging/backend/grape-backend-{ts}.tar.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    items = []
    # src/ 目录
    src_dir = os.path.join(ROOT, "src")
    items.append((src_dir, "src"))
    # scripts/ 目录 (install-worker.sh, sync_templates.sh, sync_skills.sh 等)
    scripts_dir = os.path.join(ROOT, "scripts")
    if os.path.isdir(scripts_dir):
        items.append((scripts_dir, "scripts"))
    # 仓库根文件 (start-worker.sh, Dockerfile, docker-compose.yml, README 等)
    for rf in ["start-worker.sh", "Dockerfile", "docker-compose.yml",
               "README.md", "README.zh.md", "requirements.txt",
               "requirements-worker.txt", ".gitignore"]:
        p = os.path.join(ROOT, rf)
        if os.path.isfile(p):
            items.append((p, rf))
    # configs (只保留 *.example.json + 种子 json)
    config_keep = [
        "agent-description.json", "agent-skill-mapping.json",
        "skill-description.json", "project-templates.json", "pipelines.json",
        "db-backend.example.json", "global-bot-token.example.json",
        "login-whitelist.example.json", "engine-selection.example.json",
        "server-config.example.json",
    ]
    for cf in config_keep:
        p = os.path.join(ROOT, "configs", cf)
        if os.path.isfile(p):
            items.append((p, f"configs/{cf}"))
    # requirements (顶层也放一份)
    for rf in ["requirements.txt", "requirements-worker.txt"]:
        p = os.path.join(ROOT, rf)
        if os.path.isfile(p):
            items.append((p, rf))
    # Dockerfile
    df = os.path.join(ROOT, "Dockerfile")
    if os.path.isfile(df):
        items.append((df, "Dockerfile"))
    make_subtar_gz(items, out)
    return out

def pack_database(ts):
    """数据库: MySQL DDL + 种子数据 (用 pymysql 导出, 无需 mysqldump)"""
    out = os.path.join(OUT_DIR, f"staging/database/grape-db-{ts}.sql.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # 从 configs/db-backend.json 读连接信息
    db_cfg = json.load(open(os.path.join(ROOT, "configs/db-backend.json")))
    mysql = db_cfg.get("mysql", {})
    host, port = mysql.get("host", "127.0.0.1"), mysql.get("port", 3306)
    user, password, database = mysql.get("user", "root"), mysql.get("password", ""), mysql.get("database", "grape-dev-db")

    import pymysql
    conn = pymysql.connect(host=host, port=port, user=user, password=password, database=database, charset="utf8mb4")
    cur = conn.cursor()

    # 种子表 — 导出 DDL + 数据
    seed_tables = [
        "admin_users", "admin_roles", "admin_permission_menus", "admin_role_menu_rel",
        "agent_type", "skill", "event_route_config", "global_im_bot_config",
        "flyway_schema_history",
    ]
    # 机密表 — 只导 DDL, 不导数据
    schema_only_tables = [
        "user_accounts", "user_credentials", "runtime_credentials", "managed_repos",
        "oauth_states", "repo_platform_tokens", "user_vms", "user_vm_repos",
        "unified_user_mapping", "repo_im_group_binding",
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

    lines = []
    lines.append(f"-- Grape Database Export {ts}")
    lines.append(f"-- Generated: {datetime.datetime.now().isoformat()}")
    lines.append("SET FOREIGN_KEY_CHECKS=0;")
    lines.append("SET NAMES utf8mb4;")
    lines.append("")

    all_tables = seed_tables + schema_only_tables + runtime_tables
    # 获取所有表名，确保不遗漏
    cur.execute("SHOW TABLES")
    db_tables = [r[0] for r in cur.fetchall()]
    for t in db_tables:
        if t not in all_tables:
            schema_only_tables.append(t)
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
                            escaped = str(v).replace("\\", "\\\\").replace("'", "\\'")
                            vals.append(f"'{escaped}'")
                    lines.append(f"INSERT INTO `{t}` ({col_str}) VALUES ({', '.join(vals)});")
                lines.append("")

    lines.append("SET FOREIGN_KEY_CHECKS=1;")
    conn.close()

    sql_content = "\n".join(lines)
    with gzip.open(out, "wb") as f:
        f.write(sql_content.encode("utf-8"))
    return out

def pack_skills(ts):
    """skills: 从独立仓库 grape-dev/grape-skills 读取全量"""
    out = os.path.join(OUT_DIR, f"staging/skills/grape-skills-{ts}.tar.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    skills_dir = os.path.join(SKILLS_REPO, "skills")
    if not os.path.isdir(skills_dir):
        print(f"[WARN] skills 目录不存在 ({SKILLS_REPO}/skills)，跳过")
        return None
    make_subtar_gz(skills_dir, out, base_dir="skills")
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
    """worker 运行时: src/runtime + src/agent + install-worker.sh + requirements-worker.txt"""
    out = os.path.join(OUT_DIR, f"staging/tools/grape-toolkit-{ts}.tar.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    items = []
    # worker 运行闭包（与 src/worker_package.py MANIFEST 一致）
    manifest_files = [
        "src/__init__.py", "src/config.py", "src/repo_key.py", "src/session_manager.py",
        "src/mysql_compat.py", "src/user/__init__.py", "src/user/db.py",
        "src/api/__init__.py", "src/api/routing.py", "src/api/time_utils.py",
        "src/api/event_store.py", "src/api/event_archive.py", "src/api/session_store.py",
        "src/api/knowledge.py",
    ]
    for f in manifest_files:
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            items.append((p, f))
    # 整目录
    for d in ["src/agent", "src/runtime"]:
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            items.append((p, d))
    # requirements-worker.txt
    rw = os.path.join(ROOT, "requirements-worker.txt")
    if os.path.isfile(rw):
        items.append((rw, "requirements-worker.txt"))
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
  └── runtime-agent   (python -m src.runtime.agent)

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
uv pip install --python .venv/bin/python3 -r requirements.txt

# 数据目录
mkdir -p data

# 配置 (复制 example, 填入真实凭据)
cp configs/db-backend.example.json configs/db-backend.json
cp configs/global-bot-token.example.json configs/global-bot-token.json
cp configs/login-whitelist.example.json configs/login-whitelist.json
# 编辑以上文件填入真实 MySQL/GitCode/Webhook 凭据
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

### 3.4 安装 Skills

```bash
# 解压 skills 到 grape 仓库根目录
cd /opt/grape
tar xzf /path/to/skills/grape-skills-{ts}.tar.gz
# skills 目录覆盖到 /opt/grape/skills/
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
.venv/bin/pip install -r requirements-worker.txt
bash install-worker.sh --token <rta_...> --server https://<grape主控域名>
```

## 5. 包内容清单

| 子包 | 说明 | 部署位置 |
|------|------|----------|
| backend/grape-backend-{ts}.tar.gz | 后端代码(src/api + static) + configs | /opt/grape/ |
| database/grape-db-{ts}.sql.gz | MySQL DDL + 种子数据 | MySQL |
| skills/grape-skills-{ts}.tar.gz | 官方技能 (60+ skills，独立仓库) | /opt/grape/skills/ |
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
    print(f"  -> {db_pkg} ({os.path.getsize(db_pkg) // 1024}KB)")

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
            if pkg_file and os.path.isfile(pkg_file):
                arcname = f"{pkg_dir}/{subdir}/{os.path.basename(pkg_file)}"
                tar.add(pkg_file, arcname=arcname)
                rel = f"{pkg_dir}/{subdir}/{os.path.basename(pkg_file)}"
                sha_lines.append(f"{sha256_file(pkg_file)}  {rel}")
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
    checks = {
        "INSTALL.md": any("INSTALL.md" in n for n in names),
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
    r = subprocess.run([
        "obsutil", "cp", pkg_path,
        f"obs://obs-grape-release/{prefix}/{pkg_name}", "-f",
        "-i=HPUAR0IGNKV5MR2M7J2D",
        "-k=rAb4k1mHBTV0V4TD4KC8zehanhqvKv9vdFoZVvBn",
        "-e=obs.cn-north-4.myhuaweicloud.com",
    ], capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        print(f"[pack] 上传成功 ✓")
        print(f"[pack] 验证: obsutil ls obs://obs-grape-release/{prefix}/ -s")
    else:
        print(f"[pack] 上传失败: {r.stderr}")
        sys.exit(1)

if __name__ == "__main__":
    main()
