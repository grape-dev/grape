import pymysql, json
from datetime import datetime, timezone

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
                       password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()
now_utc = datetime.now(timezone.utc)
print(f"查询时间(UTC): {now_utc.isoformat()}")
print(f"查询时间(CST): {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC+8\n")

# 1. ALL running/dispatched/queued tasks
print('=== 当前所有运行中/排队中任务 ===')
cur.execute("""
    SELECT t.id, t.event_seq, t.issue_id, t.repo_key, t.agent_type, t.skill, 
           t.status, t.runtime_id, t.started_at, t.created_at, t.attempts,
           e.event_type, e.title
    FROM agent_tasks t 
    LEFT JOIN events e ON t.event_seq = e.seq
    WHERE t.status IN ('running','dispatched','queued') 
    ORDER BY t.created_at
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    # 计算已运行时间
    started = d.get('started_at','')
    if started:
        try:
            st = datetime.fromisoformat(started)
            if st.tzinfo is None:
                from datetime import timezone as tz
                st = st.replace(tzinfo=tz.utc)
            elapsed = (now_utc - st).total_seconds()
            d['elapsed_sec'] = round(elapsed, 0)
        except:
            d['elapsed_sec'] = '?'
    print(json.dumps(d, default=str, ensure_ascii=False))

# 2. issue #5 mm6667 全部任务
print('\n=== mm6667 Issue #5 全部任务 ===')
cur.execute("""
    SELECT id, event_seq, agent_type, skill, status, exit_code, runtime_id,
           started_at, completed_at, created_at, attempts,
           LEFT(result_text,200) as result_preview,
           LEFT(errors,200) as errors_preview
    FROM agent_tasks 
    WHERE issue_id='#5' AND repo_key LIKE '%mm6667%'
    ORDER BY created_at
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 3. issue #3 mm6667 全部任务 (看看 QA 从哪来的)
print('\n=== mm6667 Issue #3 全部任务 ===')
cur.execute("""
    SELECT id, event_seq, agent_type, skill, status, exit_code, runtime_id,
           started_at, completed_at, created_at, attempts,
           LEFT(result_text,300) as result_preview,
           LEFT(errors,200) as errors_preview
    FROM agent_tasks 
    WHERE issue_id='#3' AND repo_key LIKE '%mm6667%'
    ORDER BY created_at
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 4. 最新事件 (last 15) - 看看有没有新的 QA 事件
print('\n=== 最新 15 条事件 ===')
cur.execute("""
    SELECT seq, repo_key, event_type, issue_id, agent_type, skill, status, 
           exit_code, duration, created_at, started_at, completed_at,
           LEFT(result,200) as result_preview
    FROM events 
    WHERE repo_key LIKE '%HELLO-BLOG-SYS%'
    ORDER BY seq DESC 
    LIMIT 15
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 5. issue_state for mm6667
print('\n=== mm6667 Issue 状态 (issue_state) ===')
cur.execute("SELECT * FROM issue_state WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' ORDER BY created_at")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(json.dumps(d, default=str, ensure_ascii=False))

# 6. Runtime busy slots NOW
print('\n=== Runtime 当前容量 ===')
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

conn.close()
