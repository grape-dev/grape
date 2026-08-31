#!/usr/bin/env python3
"""Grape 运营管理台发布包打包脚本。

产物: grape-admin-<YYYYMMDDHHMM>.tar
  ├── INSTALL.md          (agent 可读安装指南)
  ├── SHA256SUMS
  ├── frontend/           (grape-admin 前端 dist)
  ├── backend/            (grape-admin 后端 jar + ai_hermes_query.py + deploy 脚本)
  └── database/           (admin 相关 MySQL DDL + 种子数据)
"""
import hashlib, os, subprocess, sys, tarfile, datetime, gzip, json

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
OUT_DIR = "/root/release"
BUCKET = "obs-grape-release"
PREFIX = "release"

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _tar_filter(ti):
    if "__pycache__" in ti.name or ti.name.endswith(".pyc") or ".git" in ti.name:
        return None
    return ti

def make_subtar_gz(items, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for src, arc in items:
            if os.path.exists(src):
                tar.add(src, arcname=arc, filter=_tar_filter)

def pack_frontend(ts):
    out = os.path.join(OUT_DIR, f"staging-admin/frontend/grape-admin-frontend-{ts}.tar.gz")
    dist_dir = os.path.join(ROOT, "admin/frontend/dist")
    if not os.path.isdir(dist_dir):
        print(f"[WARN] admin frontend dist 不存在"); return None
    items = [(dist_dir, "dist")]
    make_subtar_gz(items, out)
    return out

def pack_backend(ts):
    out = os.path.join(OUT_DIR, f"staging-admin/backend/grape-admin-backend-{ts}.tar.gz")
    items = []
    jar = os.path.join(ROOT, "admin/backend/target/admin-backend-0.1.0.jar")
    if os.path.isfile(jar):
        items.append((jar, "admin-backend-0.1.0.jar"))
    ai = os.path.join(ROOT, "admin/backend/ai_hermes_query.py")
    if os.path.isfile(ai):
        items.append((ai, "ai_hermes_query.py"))
    for sf in ["deploy.sh", "admin-backend.service", "nginx-grape-admin.conf"]:
        p = os.path.join(ROOT, "admin/deploy", sf)
        if os.path.isfile(p):
            items.append((p, f"deploy/{sf}"))
    make_subtar_gz(items, out)
    return out

def pack_database(ts):
    out = os.path.join(OUT_DIR, f"staging-admin/database/grape-admin-db-{ts}.sql.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    db_cfg = json.load(open(os.path.join(ROOT, "configs/db-backend.json")))
    mysql = db_cfg.get("mysql", {})
    import pymysql
    conn = pymysql.connect(host=mysql.get("host","127.0.0.1"), port=mysql.get("port",3306),
                          user=mysql.get("user","root"), password=mysql.get("password",""),
                          database=mysql.get("database","grape-dev-db"), charset="utf8mb4")
    cur = conn.cursor()
    # admin 相关表（DDL + 种子数据）
    seed_tables = ["admin_users", "admin_roles", "admin_permission_menus",
                   "admin_role_menu_rel", "flyway_schema_history"]
    lines = [f"-- Grape Admin Database Export {ts}",
             f"-- Generated: {datetime.datetime.now().isoformat()}",
             "SET FOREIGN_KEY_CHECKS=0;", "SET NAMES utf8mb4;", ""]
    # 获取所有 admin_ 开头的表
    cur.execute("SHOW TABLES")
    all_tables = [r[0] for r in cur.fetchall()]
    admin_tables = [t for t in all_tables if t.startswith("admin_")]
    for t in admin_tables + seed_tables:
        if t in (x for x in admin_tables): pass
        cur.execute(f"SHOW CREATE TABLE `{t}`")
        row = cur.fetchone()
        if row and row[1]:
            lines.append(f"-- Table: {t}")
            lines.append(f"DROP TABLE IF EXISTS `{t}`;")
            lines.append(row[1] + ";")
            lines.append("")
        if t in seed_tables or t.startswith("admin_"):
            cur.execute(f"SELECT * FROM `{t}`")
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                col_str = ", ".join(f"`{c}`" for c in cols)
                for r in rows:
                    vals = []
                    for v in r:
                        if v is None: vals.append("NULL")
                        elif isinstance(v, (int, float)): vals.append(str(v))
                        elif isinstance(v, bytes): vals.append(f"0x{v.hex()}")
                        else:
                            vals.append(f"'{str(v).replace(chr(92),chr(92)+chr(92)).replace(chr(39),chr(92)+chr(39))}'")
                    lines.append(f"INSERT INTO `{t}` ({col_str}) VALUES ({', '.join(vals)});")
                lines.append("")
    lines.append("SET FOREIGN_KEY_CHECKS=1;")
    conn.close()
    with gzip.open(out, "wb") as f:
        f.write("\n".join(lines).encode("utf-8"))
    return out

def gen_install_md(ts):
    pkg_name = f"grape-admin-{ts}.tar"
    obs_base = f"obs://obs-grape-release/{PREFIX}"
    return f"""# Grape 运营管理台安装指南 (agent 可读)

> 包版本: {ts}
> 包名: {pkg_name}
> OBS 下载: {obs_base}/{pkg_name}

## 1. 部署拓扑

```
Grape Admin (1 台, 与 grape-main 同机或独立机)
  ├── admin-backend   (Java jar, :8090)
  ├── admin-frontend  (Vue3 静态资源, nginx 托管)
  └── MySQL            (与 grape-main 共用或独立)
```

## 2. 前置条件

- Java: >= 11
- nginx (托管前端静态资源 + 反代后端)
- MySQL: >= 8.0 (与 grape-main 共用)

## 3. 安装步骤

### 3.1 下载解压

```bash
obsutil cp {obs_base}/{pkg_name} .
tar xf {pkg_name}
cd grape-admin-{ts}/
```

### 3.2 安装前端

```bash
mkdir -p /opt/grape-admin/frontend
tar xzf frontend/grape-admin-frontend-{ts}.tar.gz -C /opt/grape-admin/frontend/
# dist 内容直接放到 nginx 托管目录
cp -r /opt/grape-admin/frontend/dist/* /opt/grape-admin/frontend/
```

### 3.3 安装后端

```bash
mkdir -p /opt/grape-admin/backend
tar xzf backend/grape-admin-backend-{ts}.tar.gz -C /opt/grape-admin/backend/
# 部署 systemd 服务
cp /opt/grape-admin/backend/deploy/admin-backend.service /etc/systemd/system/
# nginx 配置
cp /opt/grape-admin/backend/deploy/nginx-grape-admin.conf /etc/nginx/conf.d/
```

### 3.4 导入数据库

```bash
gunzip -k database/grape-admin-db-{ts}.sql.gz
mysql -h<HOST> -u<USER> -p <DB> < grape-admin-db-{ts}.sql
```

### 3.5 配置

```bash
# /etc/grape/admin.env
cat > /etc/grape/admin.env << 'EOF'
DB_HOST=<MySQL_HOST>
DB_PORT=3306
DB_USER=root
DB_PASSWORD=<PASSWORD>
DB_NAME=<grape-db>
GITCODE_BASE_URL=https://gitcode.com
EOF
```

### 3.6 启动

```bash
systemctl daemon-reload
systemctl enable --now admin-backend
systemctl reload nginx
```

### 3.7 验证

```bash
curl -s http://127.0.0.1:8090/actuator/health
systemctl is-active admin-backend
```

## 4. 包内容清单

| 子包 | 说明 |
|------|------|
| frontend/ | Vue3 前端构建产物 (dist) |
| backend/ | Java jar + ai_hermes_query.py + deploy 脚本 |
| database/ | admin_ 表 DDL + 种子数据 |

## 5. 配置文件 (需现场填写)

| 文件 | 说明 |
|------|------|
| /etc/grape/admin.env | MySQL 连接 + GitCode 配置 |
| /etc/nginx/conf.d/grape-admin.conf | nginx 反代配置 |
"""

def main():
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M")
    if len(sys.argv) > 1 and sys.argv[1] == "--ts":
        ts = sys.argv[2]
    skip_upload = "--skip-upload" in sys.argv

    print(f"[pack] 时间戳: {ts}")
    os.makedirs(os.path.join(OUT_DIR, "staging-admin"), exist_ok=True)

    print("[pack] 1/3 构建前端子包...")
    fp = pack_frontend(ts)
    if fp: print(f"  -> {fp} ({os.path.getsize(fp)//1024}KB)")

    print("[pack] 2/3 构建后端子包...")
    bp = pack_backend(ts)
    if bp: print(f"  -> {bp} ({os.path.getsize(bp)//1024}KB)")

    print("[pack] 3/3 构建数据库子包...")
    dp = pack_database(ts)
    if dp: print(f"  -> {dp} ({os.path.getsize(dp)//1024}KB)")

    # INSTALL.md
    md = gen_install_md(ts)
    md_path = os.path.join(OUT_DIR, "staging-admin", "INSTALL.md")
    with open(md_path, "w") as f:
        f.write(md)

    # 打外层 tar
    pkg_name = f"grape-admin-{ts}.tar"
    pkg_path = os.path.join(OUT_DIR, pkg_name)
    pkg_dir = f"grape-admin-{ts}"
    sha_lines = []

    print(f"[pack] 打包 {pkg_name}...")
    with tarfile.open(pkg_path, "w") as tar:
        tar.add(md_path, arcname=f"{pkg_dir}/INSTALL.md")
        sha_lines.append(f"{sha256_file(md_path)}  {pkg_dir}/INSTALL.md")
        for subdir, pkg_file in [("frontend", fp), ("backend", bp), ("database", dp)]:
            if pkg_file and os.path.isfile(pkg_file):
                arc = f"{pkg_dir}/{subdir}/{os.path.basename(pkg_file)}"
                tar.add(pkg_file, arcname=arc)
                rel = f"{pkg_dir}/{subdir}/{os.path.basename(pkg_file)}"
                sha_lines.append(f"{sha256_file(pkg_file)}  {rel}")
        sha_path = os.path.join(OUT_DIR, "staging-admin", "SHA256SUMS")
        with open(sha_path, "w") as f:
            f.write("\n".join(sorted(sha_lines)) + "\n")
        tar.add(sha_path, arcname=f"{pkg_dir}/SHA256SUMS")

    print(f"[pack] 产物: {pkg_path} ({os.path.getsize(pkg_path)//1024//1024}MB)")

    # 自检
    with tarfile.open(pkg_path) as tar:
        names = tar.getnames()
    for check in ["INSTALL.md", "SHA256SUMS", "frontend/", "backend/", "database/"]:
        ok = any(check in n for n in names)
        print(f"  {check}: {'OK' if ok else 'MISSING'}")

    if skip_upload:
        print("[pack] --skip-upload, 跳过上传"); return

    print(f"[pack] 上传到 obs://obs-grape-release/{PREFIX}/{pkg_name}...")
    r = subprocess.run([
        "obsutil", "cp", pkg_path,
        f"obs://obs-grape-release/{PREFIX}/{pkg_name}", "-f",
        "-i=HPUAR0IGNKV5MR2M7J2D",
        "-k=rAb4k1mHBTV0V4TD4KC8zehanhqvKv9vdFoZVvBn",
        "-e=obs.cn-north-4.myhuaweicloud.com",
    ], capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        print(f"[pack] 上传成功 ✓")
    else:
        print(f"[pack] 上传失败: {r.stderr}"); sys.exit(1)

if __name__ == "__main__":
    main()
