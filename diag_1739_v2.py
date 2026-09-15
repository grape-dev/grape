import pymysql, json

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 issue #12 所有 events
c.execute("SELECT seq, event_type, status, created_at FROM events WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#12' ORDER BY seq ASC")
rows = c.fetchall()
print(f"=== events for issue #12 ({len(rows)}) ===")
for r in rows:
    print(f"  seq={r['seq']} event_type={r['event_type']} status={r['status']} created={r['created_at']}")

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

# #1739 的 agent_task
c.execute("SELECT id, event_seq, agent_type FROM agent_tasks WHERE event_seq=1739")
t = c.fetchone()
if t:
    print(f"=== #1739 agent_task ===")
    print(f"  id={t['id']} event_seq={t['event_seq']} agent={t['agent_type']}")
    
    # 看 prompt
    c.execute("SELECT LEFT(prompt,8000) as prompt FROM agent_tasks WHERE id=%s", (t['id'],))
    p = c.fetchone()
    prompt = p['prompt'] or ''
    if 'Body:' in prompt:
        idx = prompt.find('Body:')
        print(f"\n=== prompt Body ===")
        print(prompt[idx:idx+500])
    if 'user-attachments' in prompt:
        idx = prompt.find('user-attachments')
        print(f"\n=== user-attachments in prompt ===")
        print(prompt[max(0,idx-100):idx+300])
    if '用户附件' in prompt:
        idx = prompt.find('用户附件')
        print(f"\n=== 附件注入 ===")
        print(prompt[max(0,idx-50):idx+400])
    else:
        print(f"\n=== 无附件注入 ===")

db.close()
