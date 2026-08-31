import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
                       password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()

# 1. Full event details for issue #5 events (seq 2448, 2449, 2451, 2452)
print('=== FULL EVENT DETAILS (issue #5) ===')
for seq in [2448, 2449, 2451, 2452]:
    cur.execute("SELECT * FROM events WHERE seq=%s", (seq,))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    if row:
        d = dict(zip(cols, row))
        print(f"\n--- seq={seq} ---")
        for k in ['seq','event_type','issue_id','agent_type','runtime','skill','status','exit_code','duration','created_at','started_at','completed_at']:
            print(f"  {k}: {d.get(k)}")
        # Print result and agent_logs (truncated)
        result = d.get('result','') or ''
        logs = d.get('agent_logs','') or ''
        print(f"  result (len={len(result)}): {result[:800]}")
        print(f"  agent_logs (len={len(logs)}): {logs[:800]}")

# 2. Full agent_tasks details for issue #5
print('\n\n=== FULL AGENT_TASKS DETAILS (issue #5) ===')
cur.execute("SELECT * FROM agent_tasks WHERE issue_id='#5' AND repo_key LIKE '%mm6667%' ORDER BY created_at")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(f"\n--- task={d.get('id')} ---")
    for k in ['id','event_seq','agent_type','skill','status','exit_code','runtime_id','started_at','completed_at','created_at','attempts']:
        print(f"  {k}: {d.get(k)}")
    result = d.get('result_text','') or ''
    errors = d.get('errors','') or ''
    stderr = d.get('stderr','') or ''
    print(f"  result_text (len={len(result)}): {result[:600]}")
    print(f"  errors (len={len(errors)}): {errors[:400]}")
    print(f"  stderr (len={len(stderr)}): {stderr[:400]}")

# 3. Compare: SRE failures for issue #3 and #4 - what are the errors?
print('\n\n=== SRE FAILURE ANALYSIS (issue #3 & #4) ===')
cur.execute("SELECT seq,issue_id,skill,status,exit_code,duration,result,agent_logs FROM events WHERE issue_id IN ('#3','#4') AND agent_type='sre' AND repo_key LIKE '%mm6667%' ORDER BY seq")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(f"\n--- seq={d['seq']} issue={d['issue_id']} skill={d['skill']} status={d['status']} exit={d['exit_code']} dur={d['duration']} ---")
    result = d.get('result','') or ''
    logs = d.get('agent_logs','') or ''
    print(f"  result (len={len(result)}): {result[:500]}")
    print(f"  agent_logs (len={len(logs)}): {logs[:500]}")

# 4. Check runtime/agent_runtimes for capacity info
print('\n\n=== AGENT RUNTIMES ===')
cur.execute("SELECT * FROM agent_runtimes")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

conn.close()
