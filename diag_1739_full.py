import pymysql, json

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 HELLO-BLOG-SYS-32 issue #12 的所有 events
c.execute("SELECT seq, event_type, status, LEFT(payload,300) as payload_preview, created_at FROM events WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#12' ORDER BY seq ASC")
rows = c.fetchall()
print(f"=== events for issue #12 ({len(rows)}) ===")
for r in rows:
    p = json.loads(r['payload_preview']) if r['payload_preview'] else {}
    print(f"  seq={r['seq']} event_type={r['event_type']} status={r['status']} payload_keys={list(p.keys())[:5]} created={r['created_at']}")

# 找原始 webhook 事件
c.execute("SELECT seq, event_type, payload FROM events WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#12' AND event_type='issue_opened' ORDER BY seq ASC")
rows = c.fetchall()
print(f"\n=== issue_opened events ({len(rows)}) ===")
for r in rows:
    p = json.loads(r['payload']) if r['payload'] else {}
    obj = p.get('object_attributes', {})
    body = obj.get('body', '') or obj.get('description', '') or ''
    print(f"  seq={r['seq']} body_len={len(body)}")
    if body:
        print(f"  body: {body[:1000]}")
    print()

# 看 #1739 的 agent_task 是哪个 event_seq
c.execute("SELECT id, event_seq, agent_type, skill FROM agent_tasks WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#12' ORDER BY created_at ASC")
tasks = c.fetchall()
print(f"=== agent_tasks for issue #12 ({len(tasks)}) ===")
for t in tasks:
    print(f"  id={t['id']} event_seq={t['event_seq']} agent={t['agent_type']} skill={t['skill']}")

db.close()
