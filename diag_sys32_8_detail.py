import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

task_id = "t_13735eab572d"

# prompt 附件注入
c.execute("SELECT LEFT(prompt,6000) as prompt FROM agent_tasks WHERE id=%s", (task_id,))
row = c.fetchone()
prompt = row['prompt'] if row else ''
if '用户附件' in prompt:
    idx = prompt.find('用户附件')
    print("=== prompt 附件注入 ===")
    print(prompt[max(0,idx-80):idx+400])
else:
    print("=== prompt 无附件注入 ===")

# 视觉分析相关日志
c.execute("SELECT seq, type, tool, LEFT(content,200) as content, LEFT(input_json,500) as input_json, created_at FROM task_messages WHERE task_id=%s AND (input_json LIKE %s OR input_json LIKE %s OR content LIKE %s OR input_json LIKE %s) ORDER BY seq ASC", (task_id, "%analyze-image%", "%obs%", "%附件%", "%curl%"))
rows = c.fetchall()
print(f"\n=== 附件/视觉分析日志 ({len(rows)}) ===")
for r in rows:
    print(f"  [{r['seq']}] {r['created_at']} {r['type']} {r['tool']}: {r['content'] or ''}")
    if r['input_json']:
        print(f"    input: {r['input_json'][:300]}")
    print()

# 最新5条
c.execute("SELECT seq, type, tool, LEFT(content,150) as content, created_at FROM task_messages WHERE task_id=%s ORDER BY seq DESC LIMIT 5", (task_id,))
rows = c.fetchall()
print(f"=== 最新5条日志 ===")
for r in rows:
    print(f"  [{r['seq']}] {r['created_at']} {r['type']} {r['tool']}: {r['content'] or ''}")

c.execute("SELECT COUNT(*) as cnt FROM task_messages WHERE task_id=%s", (task_id,))
cnt = c.fetchone()
print(f"\n总日志数: {cnt['cnt']}")

db.close()
