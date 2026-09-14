import pymysql, json

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 HELLO-BLOG-SYS-32 issue #11 的所有 events
c.execute("SELECT seq, issue_id, title, agent_type, event_type, status, LEFT(payload,200) as payload_preview, created_at FROM events WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#11' ORDER BY seq ASC")
rows = c.fetchall()
print(f"=== events for issue #11 ({len(rows)}) ===")
for r in rows:
    print(f"  seq={r['seq']} agent={r['agent_type']} event_type={r['event_type']} status={r['status']} created={r['created_at']}")
    print(f"    payload: {r['payload_preview'][:150]}")
    print()

# 看 #1736 的完整 payload
c.execute("SELECT payload FROM events WHERE seq=1736")
r = c.fetchone()
payload = json.loads(r['payload']) if r and r['payload'] else {}
print("=== #1736 full payload ===")
print(json.dumps(payload, ensure_ascii=False, indent=2)[:1000])

# 看看有没有原始 webhook 事件
c.execute("SELECT seq, event_type, payload FROM events WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#11' ORDER BY seq ASC")
rows = c.fetchall()
print(f"\n=== all events with payload detail ({len(rows)}) ===")
for r in rows:
    p = json.loads(r['payload']) if r['payload'] else {}
    obj = p.get('object_attributes', {})
    body = obj.get('body', '') or obj.get('description', '') or ''
    print(f"  seq={r['seq']} event_type={r['event_type']} body_len={len(body)} payload_keys={list(p.keys())[:5]}")
    if body:
        print(f"  body: {body[:500]}")
    print()

db.close()
