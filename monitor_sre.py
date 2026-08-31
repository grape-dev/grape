import pymysql, json
from datetime import datetime, timezone

def check():
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
                           password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
    cur = conn.cursor()
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime('%H:%M:%S')

    # Issue #5 SRE task
    cur.execute("""
        SELECT id, status, exit_code, runtime_id, started_at, completed_at, attempts,
               LEFT(result_text,200) as result_preview, LEFT(errors,200) as errors_preview
        FROM agent_tasks 
        WHERE issue_id='#5' AND repo_key LIKE '%mm6667%' AND agent_type='sre'
        ORDER BY created_at DESC LIMIT 3
    """)
    cols = [d[0] for d in cur.description]
    tasks = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Issue #5 events
    cur.execute("""
        SELECT seq, agent_type, skill, status, exit_code, duration, created_at, started_at,
               LEFT(result,200) as result_preview
        FROM events 
        WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' AND issue_id='#5'
        ORDER BY seq
    """)
    cols = [d[0] for d in cur.description]
    events = [dict(zip(cols, row)) for row in cur.fetchall()]

    # All active tasks count
    cur.execute("SELECT COUNT(*) FROM agent_tasks WHERE status IN ('running','dispatched','queued')")
    active_count = cur.fetchone()[0]

    # Runtime capacity
    cur.execute("""
        SELECT id, name, busy_slots, max_concurrent 
        FROM agent_runtimes 
        WHERE status='online' AND busy_slots > 0
        ORDER BY busy_slots DESC
    """)
    cols = [d[0] for d in cur.description]
    runtimes = [dict(zip(cols, row)) for row in cur.fetchall()]

    # issue_state
    cur.execute("SELECT stage, owner_agent, updated_at FROM issue_state WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' AND issue_id='#5'")
    cols = [d[0] for d in cur.description]
    state = [dict(zip(cols, row)) for row in cur.fetchall()]

    conn.close()

    print(f"\n{'='*60}")
    print(f"查询时间: {ts} CST")
    print(f"{'='*60}")

    print(f"\n活跃任务总数: {active_count}")
    print(f"\nIssue #5 issue_state: {json.dumps(state[0], default=str, ensure_ascii=False) if state else 'N/A'}")

    print(f"\nIssue #5 事件流:")
    for e in events:
        print(f"  seq={e['seq']} {e['agent_type']:8} {e['skill']:12} {e['status']:10} exit={e['exit_code']} dur={e['duration']} created={e['created_at']}")
        if e.get('result_preview'):
            print(f"    result: {e['result_preview'][:120]}")

    print(f"\nIssue #5 SRE 任务 (最新3条):")
    for t in tasks:
        started = t.get('started_at','')
        elapsed = '?'
        if started:
            try:
                st = datetime.fromisoformat(started)
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                elapsed = f"{round((now_utc - st).total_seconds(), 0):.0f}s"
            except:
                pass
        print(f"  task={t['id']} status={t['status']} exit={t['exit_code']} runtime={t['runtime_id']} attempts={t['attempts']} elapsed={elapsed}")
        if t.get('result_preview'):
            print(f"    result: {t['result_preview'][:120]}")
        if t.get('errors_preview'):
            print(f"    errors: {t['errors_preview'][:120]}")

    print(f"\nRuntime 容量:")
    for r in runtimes:
        pct = (r['busy_slots']/r['max_concurrent'])*100
        marker = ' ⚠️' if pct > 100 else ''
        print(f"  runtime {r['id']:3} ({r['name']:10})  busy={r['busy_slots']}/{r['max_concurrent']} ({pct:.0f}%){marker}")

    return tasks, events

if __name__ == '__main__':
    check()
