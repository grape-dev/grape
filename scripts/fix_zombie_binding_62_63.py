#!/usr/bin/env python
"""Fix zombie runtime bindings 62/63 -> 64/65 (token-swap leftovers, 2026-08-17).

- 62 (opencode, credential 92 dev-worker-0002, offline) -> 64 (opencode, credential 93 vod-worker, online)
- 63 (hermes,   credential 92 dev-worker-0002, offline) -> 65 (hermes,   credential 93 vod-worker, online)
Updates devops_agent_runtime_binding + devops_agent_session rows, then live-tests pick_engine.
"""
import sys, json
import pymysql

cfg = json.load(open('/opt/grape/configs/db-backend.json'))['mysql']
conn = pymysql.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'],
                       password=cfg['password'], database=cfg['database'],
                       charset='utf8mb4', autocommit=True, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

# ---- 1. find all binding/session rows pointing at dead rows 62/63 ----
cur.execute("SELECT repo_key, agent_type, runtime_id, enabled FROM devops_agent_runtime_binding WHERE runtime_id IN (62,63) ORDER BY repo_key, agent_type")
bindings = cur.fetchall()
cur.execute("SELECT id, repo_key, agent_type, runtime_id FROM devops_agent_session WHERE runtime_id IN (62,63) ORDER BY repo_key, agent_type")
sessions = cur.fetchall()

print(f"=== bindings on 62/63: {len(bindings)} rows ===")
for r in bindings: print(" ", r)
print(f"=== sessions on 62/63: {len(sessions)} rows ===")
for r in sessions: print(" ", r)

# ---- 2. apply fix ----
n_b = n_s = 0
for r in bindings:
    new = 64 if r['runtime_id'] == 62 else 65
    cur.execute("UPDATE devops_agent_runtime_binding SET runtime_id=%s WHERE repo_key=%s AND agent_type=%s AND runtime_id=%s",
                (new, r['repo_key'], r['agent_type'], r['runtime_id']))
    n_b += cur.rowcount
for r in sessions:
    new = 64 if r['runtime_id'] == 62 else 65
    cur.execute("UPDATE devops_agent_session SET runtime_id=%s WHERE id=%s", (new, r['id']))
    n_s += cur.rowcount
print(f"\nupdated bindings={n_b} sessions={n_s}")
conn.close()
print("DB fix applied.")
