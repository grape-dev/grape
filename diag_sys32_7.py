import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 HELLO-BLOG-SYS-32 issue #7 的 events
c.execute("SELECT seq, repo_key, issue_id, title, agent_type, skill, status, exit_code, duration, LEFT(result,400) as result, created_at, completed_at FROM events WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#7' ORDER BY seq ASC")
rows = c.fetchall()
print(f"=== events for HELLO-BLOG-SYS-32 #7 ({len(rows)}) ===")
for r in rows:
    print()
    for k,v in r.items():
        print(f"  {k}: {v}")

# 查 agent_tasks
c.execute("SELECT id, event_seq, agent_type, skill, status, exit_code, LEFT(errors,300) as errors, LEFT(result_text,400) as result_text, started_at, completed_at FROM agent_tasks WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#7' ORDER BY created_at ASC")
tasks = c.fetchall()
print(f"\n=== agent_tasks ({len(tasks)}) ===")
for t in tasks:
    print()
    for k,v in t.items():
        print(f"  {k}: {v}")

db.close()
