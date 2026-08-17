#!/usr/bin/env python
"""Rebind chat config rows stuck on local-direct runtime (credential_id=0 / dead rows)
to a healthy remote engine, using the code's own resolver so it matches what
set_chat_config would write now. Also cleans queued blackhole tasks on runtime 1.
"""
import sys, json
sys.path.insert(0, '/opt/grape')
import pymysql
from src.session_manager import _resolve_chat_runtime_id

cfg = json.load(open('/opt/grape/configs/db-backend.json'))['mysql']
conn = pymysql.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'],
                       password=cfg['password'], database=cfg['database'],
                       charset='utf8mb4', autocommit=True, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

# 1. 目标引擎 = 当前代码对裸引擎名 'opencode' 的解析结果（queue_remote → 远端 worker）
target = _resolve_chat_runtime_id('opencode')
print(f"resolver target for 'opencode': runtime_id={target}")

cur.execute("SELECT id, name, status, healthy, installed, credential_id, last_seen_at FROM agent_runtimes WHERE id=%s", (target,))
tr = cur.fetchone()
print("target row:", tr)
if not tr or (tr['credential_id'] or 0) == 0 or tr['status'] != 'online':
    print("!! target not healthy, abort")
    sys.exit(1)
cur.execute("SELECT id, status FROM runtime_credentials WHERE id=%s", (tr['credential_id'],))
rc = cur.fetchone()
print("target credential:", rc)
if not rc or rc['status'] != 'active':
    print("!! target credential not active, abort")
    sys.exit(1)

# 2. 找出所有坏配置行：runtime_id 指向 credential_id=0 / 不存在的 runtime
cur.execute("""SELECT c.id, c.user_id, c.runtime, c.runtime_id,
               r.credential_id, r.status AS r_status
               FROM chat_agent_session c
               LEFT JOIN agent_runtimes r ON r.id = c.runtime_id
               WHERE c.id LIKE 'sess-cfg-%%' AND c.repo_key=''""")
rows = cur.fetchall()
print(f"\nall sess-cfg rows: {len(rows)}")
bad = []
for r in rows:
    rid = r['runtime_id'] or 0
    cid = r['credential_id'] if r['credential_id'] is not None else 0
    st = r['r_status'] or 'missing'
    if rid <= 0 or cid == 0 or st != 'online':
        bad.append(r)
        print(f"  BAD  {r['id']} user={r['user_id']} runtime={r['runtime']} rid={rid} cred={cid} status={st}")
    else:
        print(f"  ok   {r['id']} user={r['user_id']} rid={rid} cred={cid} status={st}")

n = 0
for r in bad:
    cur.execute("UPDATE chat_agent_session SET runtime_id=%s WHERE id=%s", (target, r['id']))
    n += cur.rowcount
print(f"\nrebound {n} rows to runtime {target}")

# 3. 清 runtime=1 的死队列任务
cur.execute("SELECT COUNT(*) c FROM agent_tasks WHERE runtime_id=1 AND status='queued'")
qrow = cur.fetchone()
q = qrow['c'] if qrow else 0
if q:
    cur.execute("UPDATE agent_tasks SET status='failed', errors='runtime 1 本机直连行无人 claim，运维清理', completed_at=NOW() WHERE runtime_id=1 AND status='queued'")
    print(f"cleaned {q} queued blackhole tasks on runtime 1")
else:
    print("no queued blackhole tasks on runtime 1")

conn.close()
print("done")
