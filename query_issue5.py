import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
                       password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()

# 1. issue_state
print('=== issue_state (HELLO-BLOG-SYS) ===')
cur.execute("SELECT * FROM issue_state WHERE repo_key LIKE '%HELLO-BLOG-SYS%'")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    for k,v in d.items():
        print(f'  {k}: {v}')
    print('---')

# 2. events
print('\n=== events (HELLO-BLOG-SYS) ===')
cur.execute("SELECT seq,event_type,source,issue_id,title,actor,agent_type,runtime,skill,status,exit_code,duration,created_at,started_at,completed_at FROM events WHERE repo_key LIKE '%HELLO-BLOG-SYS%' ORDER BY seq")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 3. agent_tasks
print('\n=== agent_tasks (HELLO-BLOG-SYS) ===')
cur.execute("SELECT id,event_seq,issue_id,agent_type,skill,status,exit_code,started_at,completed_at,created_at,attempts,runtime_id FROM agent_tasks WHERE repo_key LIKE '%HELLO-BLOG-SYS%' ORDER BY created_at")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 4. event_thread
print('\n=== event_thread (HELLO-BLOG-SYS) ===')
cur.execute("SELECT * FROM event_thread WHERE repo_key LIKE '%HELLO-BLOG-SYS%' ORDER BY created_at")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    if d.get('content'):
        d['content'] = d['content'][:200] + '...' if len(d['content'])>200 else d['content']
    print(json.dumps(d, default=str, ensure_ascii=False))

# 5. event_skipped
print('\n=== event_skipped (HELLO-BLOG-SYS) ===')
cur.execute("SELECT * FROM event_skipped WHERE repo_key LIKE '%HELLO-BLOG-SYS%' ORDER BY created_at")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 6. issue_agent_session
print('\n=== issue_agent_session (HELLO-BLOG-SYS) ===')
cur.execute("SELECT * FROM issue_agent_session WHERE repo_key LIKE '%HELLO-BLOG-SYS%' ORDER BY created_at")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

conn.close()
