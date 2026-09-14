import pymysql, json

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 event #1739
c.execute("SELECT seq, repo_key, issue_id, title, agent_type, status, payload FROM events WHERE seq=1739")
e = c.fetchone()
if e:
    print("=== EVENT #1739 ===")
    print(f"  seq: {e['seq']}")
    print(f"  repo_key: {e['repo_key']}")
    print(f"  issue_id: {e['issue_id']}")
    print(f"  title: {e['title']}")
    print(f"  status: {e['status']}")
    
    payload = json.loads(e['payload']) if e['payload'] else {}
    print(f"  payload keys: {list(payload.keys())}")
    
    obj = payload.get('object_attributes', {})
    body = obj.get('body', '') or obj.get('description', '') or ''
    print(f"  body length: {len(body)}")
    print(f"  body: {body[:1000]}")

# 查 agent_task prompt
c.execute("SELECT id, LEFT(prompt, 8000) as prompt FROM agent_tasks WHERE event_seq=1739")
t = c.fetchone()
if t:
    prompt = t['prompt'] or ''
    print(f"\n=== AGENT_TASK prompt ===")
    print(f"  task_id: {t['id']}")
    print(f"  prompt length: {len(prompt)}")
    
    # 检查附件注入
    if '用户附件' in prompt:
        idx = prompt.find('用户附件')
        print(f"\n=== 附件注入 ===")
        print(prompt[max(0,idx-50):idx+400])
    else:
        print(f"\n=== 无附件注入 ===")
    
    # 检查 user-attachments
    if 'user-attachments' in prompt:
        idx = prompt.find('user-attachments')
        print(f"\n=== user-attachments in prompt ===")
        print(prompt[max(0,idx-100):idx+300])
    
    # 检查 Body 部分
    if 'Body:' in prompt:
        idx = prompt.find('Body:')
        print(f"\n=== prompt Body ===")
        print(prompt[idx:idx+500])

db.close()
