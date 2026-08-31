import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
                       password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()

# 1. mm6667 issue #3 完整事件流 (含 result 全文)
print('=== mm6667 Issue #3 完整事件流 ===')
cur.execute("""
    SELECT seq, event_type, issue_id, agent_type, skill, status, exit_code, duration,
           created_at, started_at, completed_at, result, agent_logs, payload
    FROM events 
    WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' AND issue_id='#3'
    ORDER BY seq
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(f"\n--- seq={d['seq']} ---")
    print(f"  type={d['event_type']} agent={d['agent_type']} skill={d['skill']} status={d['status']} exit={d['exit_code']} dur={d['duration']}")
    print(f"  created={d['created_at']} started={d['started_at']} completed={d['completed_at']}")
    result = d.get('result','') or ''
    print(f"  result (len={len(result)}):")
    print(f"    {result[:600]}")
    payload = d.get('payload','') or ''
    if payload:
        print(f"  payload (len={len(payload)}): {payload[:400]}")

# 2. issue_state 变化历史 for mm6667 issue #3
print('\n\n=== mm6667 Issue #3 issue_state ===')
cur.execute("SELECT * FROM issue_state WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' AND issue_id='#3'")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 3. 查看 22:25 到 22:50 之间所有 mm6667 事件 (看 coding 是怎么被触发的)
print('\n\n=== mm6667 22:25-22:50 间所有事件 ===')
cur.execute("""
    SELECT seq, repo_key, event_type, issue_id, agent_type, skill, status, created_at,
           LEFT(result,300) as result_preview, LEFT(payload,300) as payload_preview
    FROM events 
    WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' 
    AND created_at >= '2026-08-24 22:25:00'
    AND created_at <= '2026-08-24 22:55:00'
    ORDER BY seq
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 4. agent_tasks for issue #3 - 看 coding 任务的完整信息
print('\n\n=== mm6667 Issue #3 coding 任务详情 ===')
cur.execute("""
    SELECT id, event_seq, agent_type, skill, status, exit_code, runtime_id,
           started_at, completed_at, created_at, attempts,
           LEFT(prompt,500) as prompt_preview,
           LEFT(result_text,500) as result_preview,
           LEFT(errors,300) as errors_preview,
           event_type
    FROM agent_tasks 
    WHERE issue_id='#3' AND repo_key LIKE '%mm6667%'
    ORDER BY created_at
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(f"\n--- task={d['id']} ---")
    for k in ['event_seq','agent_type','skill','status','exit_code','event_type','created_at','started_at','completed_at']:
        print(f"  {k}: {d.get(k)}")
    print(f"  prompt: {d.get('prompt_preview','')[:400]}")
    print(f"  result: {d.get('result_preview','')[:400]}")
    print(f"  errors: {d.get('errors_preview','')[:200]}")

# 5. event_thread for issue #3 - 看 A2A 交办链
print('\n\n=== mm6667 Issue #3 event_thread ===')
cur.execute("""
    SELECT * FROM event_thread 
    WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' AND issue_id='#3'
    ORDER BY created_at
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    content = d.get('content','') or ''
    d['content'] = content[:300] + '...' if len(content)>300 else content
    print(json.dumps(d, default=str, ensure_ascii=False))

conn.close()
