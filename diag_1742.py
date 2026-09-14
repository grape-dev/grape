import pymysql, json

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 #1742
c.execute("SELECT seq, repo_key, issue_id, title, agent_type, event_type, status, payload FROM events WHERE seq=1742")
e = c.fetchone()
if e:
    print(f"=== EVENT #1742 ===")
    print(f"  seq: {e['seq']}")
    print(f"  issue_id: {e['issue_id']}")
    print(f"  agent_type: {e['agent_type']}")
    print(f"  event_type: {e['event_type']}")
    print(f"  status: {e['status']}")
    
    payload = json.loads(e['payload']) if e['payload'] else {}
    print(f"  payload keys: {list(payload.keys())}")
    obj = payload.get('object_attributes', {})
    body = obj.get('body', '') or obj.get('description', '') or ''
    print(f"  body in payload: {len(body)} chars")

# 查 agent_task prompt
c.execute("SELECT id, LEFT(prompt, 8000) as prompt FROM agent_tasks WHERE event_seq=1742")
t = c.fetchone()
if t:
    prompt = t['prompt'] or ''
    print(f"\n=== AGENT_TASK ===")
    print(f"  task_id: {t['id']}")
    print(f"  prompt length: {len(prompt)}")
    
    if '用户附件' in prompt:
        idx = prompt.find('用户附件')
        print(f"\n=== 附件注入成功 ===")
        print(prompt[max(0,idx-50):idx+500])
    elif 'user-attachments' in prompt:
        idx = prompt.find('user-attachments')
        print(f"\n=== user-attachments in body (未注入但 body 里有) ===")
        print(prompt[max(0,idx-100):idx+300])
    else:
        print(f"\n=== 无附件 ===")

# 查原始 webhook 事件
c.execute("SELECT seq, event_type, payload FROM events WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#13' AND event_type='issue_opened' ORDER BY seq DESC LIMIT 1")
r = c.fetchone()
if r:
    p = json.loads(r['payload']) if r['payload'] else {}
    obj = p.get('object_attributes', {})
    body = obj.get('body', '') or obj.get('description', '') or ''
    print(f"\n=== 原始 webhook event seq={r['seq']} ===")
    print(f"  body_len: {len(body)}")
    if body:
        print(f"  body: {body[:500]}")

db.close()
