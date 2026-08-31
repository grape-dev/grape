import pymysql, json
from datetime import datetime, timezone

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
                       password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()
now_utc = datetime.now(timezone.utc)
print(f"查询时间: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC / CST+8")
print()

# 1. Issue #5 SRE task current status
print('=== mm6667 Issue #5 SRE 任务 ===')
cur.execute("""
    SELECT id, event_seq, agent_type, skill, status, exit_code, runtime_id,
           started_at, completed_at, created_at, attempts,
           LEFT(result_text,300) as result_preview,
           LEFT(errors,300) as errors_preview
    FROM agent_tasks 
    WHERE issue_id='#5' AND repo_key LIKE '%mm6667%' AND agent_type='sre'
    ORDER BY created_at
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    started = d.get('started_at','')
    if started:
        try:
            st = datetime.fromisoformat(started)
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            elapsed = (now_utc - st).total_seconds()
            d['elapsed_sec'] = round(elapsed, 0)
        except:
            d['elapsed_sec'] = '?'
    print(json.dumps(d, default=str, ensure_ascii=False))

# 2. Issue #5 event status
print('\n=== mm6667 Issue #5 最新事件 ===')
cur.execute("""
    SELECT seq, event_type, agent_type, skill, status, exit_code, duration,
           created_at, started_at, LEFT(result,300) as result_preview
    FROM events 
    WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' AND issue_id='#5'
    ORDER BY seq
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 3. issue_state for #5
print('\n=== mm6667 Issue #5 issue_state ===')
cur.execute("SELECT * FROM issue_state WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' AND issue_id='#5'")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 4. ALL running/dispatched tasks right now
print('\n=== 当前所有活跃任务 ===')
cur.execute("""
    SELECT t.id, t.event_seq, t.issue_id, t.repo_key, t.agent_type, t.skill, 
           t.status, t.runtime_id, t.started_at, t.created_at
    FROM agent_tasks t 
    WHERE t.status IN ('running','dispatched','queued') 
    ORDER BY t.created_at
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    started = d.get('started_at','')
    if started:
        try:
            st = datetime.fromisoformat(started)
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            d['elapsed_sec'] = round((now_utc - st).total_seconds(), 0)
        except:
            pass
    print(json.dumps(d, default=str, ensure_ascii=False))

# 5. Runtime capacity
print('\n=== Runtime 容量 ===')
cur.execute("""
    SELECT id, name, status, busy_slots, max_concurrent 
    FROM agent_runtimes 
    WHERE status='online' AND busy_slots > 0
    ORDER BY busy_slots DESC
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    pct = (d['busy_slots']/d['max_concurrent'])*100
    print(f"  runtime {d['id']:3} ({d['name']:10})  busy={d['busy_slots']}/{d['max_concurrent']} ({pct:.0f}%)")

# 6. SRE stats for HELLO-BLOG-SYS (all users)
print('\n=== SRE 任务统计 (HELLO-BLOG-SYS 全部) ===')
cur.execute("""
    SELECT status, COUNT(*) as cnt, AVG(duration) as avg_dur, MAX(duration) as max_dur
    FROM events 
    WHERE agent_type='sre' AND repo_key LIKE '%HELLO-BLOG-SYS%'
    GROUP BY status
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 7. Latest 5 events overall (any repo)
print('\n=== 全局最新 5 条事件 ===')
cur.execute("""
    SELECT seq, repo_key, event_type, issue_id, agent_type, status, created_at
    FROM events ORDER BY seq DESC LIMIT 5
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

conn.close()
