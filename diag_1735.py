import pymysql, json

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# #1735 的 prompt 和 result
c.execute("SELECT id, LEFT(prompt,8000) as prompt, LEFT(result_text,500) as result_text, status FROM agent_tasks WHERE event_seq=1735")
r = c.fetchone()
if r:
    prompt = r['prompt'] or ''
    print(f"=== #1735 task_id: {r['id']} status: {r['status']} ===")
    
    # 看 body 部分
    if 'Body:' in prompt:
        idx = prompt.find('Body:')
        print(f"\n=== prompt Body section ===")
        print(prompt[idx:idx+500])
    
    # 看有没有附件注入
    if '用户附件' in prompt:
        idx = prompt.find('用户附件')
        print(f"\n=== 附件注入 ===")
        print(prompt[max(0,idx-50):idx+300])
    else:
        print(f"\n=== 无附件注入 ===")
    
    # 看有没有 user-attachments
    if 'user-attachments' in prompt:
        idx = prompt.find('user-attachments')
        print(f"\n=== user-attachments in prompt ===")
        print(prompt[max(0,idx-50):idx+200])
    
    print(f"\nresult_text: {r['result_text']}")

db.close()
