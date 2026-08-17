#!/usr/bin/env python3
"""new-version2 本地部署预检脚本（开发机用，不碰生产）。

在项目根目录执行：python3 verify-local.py
- 用独立副本库验证迁移幂等 + WAL + 数据保全
- 校验前端编码（UTF-8 无 BOM、无替换符，防 DEPLOYMENT-REMOTE §6.1 事故）
"""
import os, sys, sqlite3, shutil, glob, tempfile

ROOT = os.path.abspath(".")
DB_SRC = os.path.join(ROOT, "data", "sessions.db")
PASS, FAIL = 0, 0

def ok(m): global PASS; PASS += 1; print(f"  [PASS] {m}")
def bad(m): global FAIL; FAIL += 1; print(f"  [FAIL] {m}")

print("=== 1. 数据库迁移预检（用副本，不碰 data/sessions.db）===")
tmp_db = os.path.join(ROOT, "data", "_deploy_precheck.db")
for f in glob.glob(tmp_db + "*"):
    os.remove(f)
if os.path.exists(DB_SRC):
    shutil.copy2(DB_SRC, tmp_db)
    print(f"  副本来源: {DB_SRC} ({os.path.getsize(DB_SRC)} bytes)")
else:
    print("  data/sessions.db 不存在，用空库预检")
    sqlite3.connect(tmp_db).close()

os.environ["DEVOPS_DB_PATH"] = tmp_db
sys.path.insert(0, ROOT)
from src.api.db_setup import init_database
init_database()
init_database()  # 幂等

conn = sqlite3.connect(tmp_db)
c = conn.cursor()
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
for t in ("agent_runtimes", "agent_tasks", "runtime_credentials"):
    ok(f"表 {t} 存在") if t in tables else bad(f"表 {t} 缺失")
ok("backend_agents 已移除") if "backend_agents" not in tables else bad("backend_agents 仍存在")

mode = c.execute("PRAGMA journal_mode").fetchone()[0]
ok(f"WAL 生效 ({mode})") if mode == "wal" else bad(f"WAL 未生效 ({mode})")

try:
    cnt = c.execute("SELECT COUNT(*) FROM agent_runtimes").fetchone()[0]
    ok(f"agent_runtimes 数据保全 ({cnt} 行)") if cnt >= 1 else bad("agent_runtimes 为空")
except Exception as e:
    bad(f"agent_runtimes 查询失败: {e}")

sess_cols = [r[1] for r in c.execute("PRAGMA table_info(devops_agent_session)").fetchall()]
ok("runtime_id 存在") if "runtime_id" in sess_cols else bad("runtime_id 缺失")
ok("backend_id 已移除") if "backend_id" not in sess_cols else bad("backend_id 仍存在")
conn.close()

print("=== 2. 前端编码校验（防乱码事故）===")
frontend_files = [
    "src/api/static/index.html",
    "src/api/static/js/app.js",
    "src/api/static/js/api.js",
    "src/api/static/js/pages/runtimes.js",
    "src/api/static/js/pages/chat.js",
    "src/api/static/js/pages/events.js",
    "src/api/static/js/pages/agents.js",
]
for f in frontend_files:
    p = os.path.join(ROOT, f)
    if not os.path.exists(p):
        bad(f"{f} 缺失")
        continue
    raw = open(p, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        bad(f"{f} 含 BOM")
        continue
    try:
        t = raw.decode("utf-8")
        if "\ufffd" in t:
            bad(f"{f} 含替换符（乱码）")
        else:
            ok(f"{f} UTF-8 干净")
    except UnicodeDecodeError:
        bad(f"{f} 非 UTF-8")

print("=== 3. 服务进程检查（本机）===")
import subprocess
try:
    r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10)
    for line in r.stdout.splitlines():
        if ":3000 " in line and "LISTENING" in line:
            print("  ⚠️ 本机 :3000 已有进程（可能是开发实例），部署预检不触碰它")
            break
except Exception:
    pass

print("=== 4. 前端引用完整性 ===")
import re
idx = open(os.path.join(ROOT, "src/api/static/index.html"), encoding="utf-8").read()
for m in re.finditer(r'src="(/static/js/pages/[^"?]+)', idx):
    path = os.path.join(ROOT, "src/api/static", m.group(1).replace("/static/", ""))
    ok(f"前端资源存在 {m.group(1)}") if os.path.exists(path) else bad(f"前端资源缺失 {m.group(1)}")

for f in glob.glob(tmp_db + "*"):
    try:
        os.remove(f)
    except PermissionError:
        print("  [WARN] 副本库句柄未释放（Windows），跳过删除")
for f in glob.glob(os.path.join(ROOT, "data/backups", "_deploy_precheck.db_pre-m0.*.bak")):
    try:
        os.remove(f)
    except OSError:
        pass

print(f"\n===== 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(1 if FAIL else 0)
