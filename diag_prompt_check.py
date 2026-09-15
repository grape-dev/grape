import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 issue #9 VOD task 的 prompt
c.execute("SELECT id, LEFT(prompt, 8000) as prompt, created_at FROM agent_tasks WHERE repo_key LIKE '%HELLO-BLOG-SYS-32%' AND issue_id='#9' AND agent_type='vod'")
row = c.fetchone()
if not row:
    print("NOT FOUND")
else:
    prompt = row['prompt']
    print(f"task_id: {row['id']}")
    print(f"created_at: {row['created_at']}")
    print()
    # 找 analyze-image 相关
    if 'analyze-image' in prompt:
        idx = prompt.find('analyze-image')
        print("=== analyze-image in prompt ===")
        print(prompt[max(0,idx-100):idx+200])
    else:
        print("=== no analyze-image in prompt ===")
    
    print()
    if '--max-time 60' in prompt:
        print(">>> --max-time 60 FOUND in prompt <<<")
    else:
        print(">>> --max-time 60 NOT in prompt <<<")
    
    if '/api/chat/analyze-image' in prompt:
        print(">>> /api/chat/analyze-image (correct path) <<<")
    elif '/api/analyze-image' in prompt:
        print(">>> /api/analyze-image (OLD wrong path) <<<")

db.close()
