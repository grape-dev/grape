import pymysql, json

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# #1738 的 prompt
c.execute("SELECT id, LEFT(prompt,8000) as prompt, status FROM agent_tasks WHERE event_seq=1738")
t = c.fetchone()
if t:
    prompt = t['prompt'] or ''
    print(f"=== #1738 task_id={t['id']} status={t['status']} ===")
    print(f"prompt length: {len(prompt)}")
    
    if '用户附件' in prompt:
        idx = prompt.find('用户附件')
        print(f"\n=== 附件注入 ===")
        print(prompt[max(0,idx-50):idx+400])
    elif 'user-attachments' in prompt:
        idx = prompt.find('user-attachments')
        print(f"\n=== user-attachments in prompt (body 里) ===")
        print(prompt[max(0,idx-100):idx+300])
    else:
        print(f"\n=== 无附件 ===")
    
    # 看 Body 部分
    if 'Body:' in prompt:
        idx = prompt.find('Body:')
        print(f"\n=== prompt Body ===")
        print(prompt[idx:idx+500])

# 看 #1739 是怎么触发的 - A2A payload
c.execute("SELECT payload FROM events WHERE seq=1739")
r = c.fetchone()
payload = json.loads(r['payload']) if r and r['payload'] else {}
print(f"\n=== #1739 A2A payload ===")
print(json.dumps(payload, ensure_ascii=False, indent=2))

db.close()
