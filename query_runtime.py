import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
                       password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()

# 1. Check current running/dispatched tasks across ALL repos to see runtime contention
print('=== ALL RUNNING/DISPATCHED TASKS ===')
cur.execute("SELECT id,event_seq,issue_id,repo_key,agent_type,skill,status,runtime_id,started_at,created_at FROM agent_tasks WHERE status IN ('running','dispatched','queued') ORDER BY created_at")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 2. Check the latest events to see if anything new happened after 2453
print('\n=== LATEST EVENTS (last 10) ===')
cur.execute("SELECT seq,repo_key,event_type,issue_id,agent_type,status,created_at FROM events ORDER BY seq DESC LIMIT 10")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 3. Check SRE task timeout config
print('\n=== SRE TIMEOUT CONFIG ===')
# Look for timeout settings in the skill or agent_type config
cur.execute("SELECT * FROM agent_type WHERE agent_type='sre'")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 4. Runtime busy slots summary
print('\n=== RUNTIME CAPACITY SUMMARY ===')
cur.execute("SELECT id,name,display_name,status,busy_slots,max_concurrent,scope FROM agent_runtimes WHERE status='online' ORDER BY busy_slots DESC")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    pct = (d['busy_slots']/d['max_concurrent'])*100 if d['max_concurrent'] else 0
    print(f"  runtime {d['id']:3} ({d['name']:10}) busy={d['busy_slots']}/{d['max_concurrent']} ({pct:.0f}%)  scope={d['scope']}")

# 5. Count SRE failures vs successes for HELLO-BLOG-SYS
print('\n=== SRE SUCCESS/FAIL STATS (HELLO-BLOG-SYS) ===')
cur.execute("""
    SELECT status, COUNT(*) as cnt, 
           AVG(duration) as avg_dur,
           MIN(duration) as min_dur,
           MAX(duration) as max_dur
    FROM events 
    WHERE agent_type='sre' AND repo_key LIKE '%HELLO-BLOG-SYS%'
    GROUP BY status
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 6. Check the devops-bot log for very latest entries
print('\n=== LAST 20 LINES OF devops-bot.log ===')

conn.close()
