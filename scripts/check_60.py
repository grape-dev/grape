#!/usr/bin/env python3
"""Check issue #60 pipeline status."""
import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

cur.execute("SELECT seq, event_type, agent_type, status, LEFT(result,200) as result, created_at FROM events WHERE issue_id='#60' ORDER BY seq")
print("=== #60 EVENTS ===")
for r in cur.fetchall():
    print(f"  seq={r['seq']} {r['event_type']} {r['agent_type']} {r['status']} {r['created_at']}")

cur.execute("SELECT id, issue_id, agent_type, status, exit_code, runtime_id, started_at, completed_at, LEFT(errors,200) as errors, LEFT(result_text,300) as result_text, event_seq FROM agent_tasks WHERE issue_id='#60' ORDER BY created_at")
print("\n=== #60 TASKS ===")
for r in cur.fetchall():
    print(json.dumps(r, default=str, ensure_ascii=False))

cur.execute("SELECT stage, owner_agent, a2a_seq, updated_at FROM issue_state WHERE issue_id='#60'")
print("\n=== #60 STATE ===")
for r in cur.fetchall():
    print(json.dumps(r, default=str, ensure_ascii=False))

cur.execute("""SELECT tm.seq, tm.type, tm.tool, LEFT(tm.content,200) as content, tm.created_at
FROM task_messages tm JOIN agent_tasks at ON tm.task_id = at.id
WHERE at.issue_id='#60' ORDER BY tm.created_at DESC LIMIT 15""")
print("\n=== #60 RECENT MESSAGES ===")
for r in cur.fetchall():
    print(f"  seq={r['seq']} type={r['type']} tool={r['tool']} {str(r['content'])[:150]}")

conn.close()
