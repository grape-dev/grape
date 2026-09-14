import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# VOD task: 附件下载是否成功
vod_task = "t_6bf2a0950998"
c.execute("SELECT seq, type, tool, LEFT(content,200) as content, LEFT(input_json,400) as input_json, created_at FROM task_messages WHERE task_id=%s AND (input_json LIKE %s OR input_json LIKE %s OR input_json LIKE %s OR content LIKE %s) ORDER BY seq ASC", (vod_task, "%obs%", "%attach%", "%curl%", "%附件%"))
rows = c.fetchall()
print(f"=== VOD 附件相关日志 ({len(rows)}) ===")
for r in rows:
    print(f"  [{r['seq']}] {r['type']} {r['tool']}: {r['content'] or ''}")
    if r['input_json']:
        print(f"    input: {r['input_json'][:200]}")
    print()

# VOD prompt 里附件注入
c.execute("SELECT LEFT(prompt,5000) as prompt FROM agent_tasks WHERE id=%s", (vod_task,))
row = c.fetchone()
prompt = row['prompt'] if row else ''
if '用户附件' in prompt:
    idx = prompt.find('用户附件')
    print("=== VOD prompt 附件注入 ===")
    print(prompt[max(0,idx-50):idx+400])
else:
    print("=== VOD prompt 无附件注入 ===")

# Coding task 日志
coding_task = "t_cac951ca6ff3"
c.execute("SELECT seq, type, tool, LEFT(content,300) as content, LEFT(input_json,400) as input_json, created_at FROM task_messages WHERE task_id=%s ORDER BY seq ASC LIMIT 10", (coding_task,))
rows = c.fetchall()
print(f"\n=== Coding 前10条日志 ({len(rows)}) ===")
for r in rows:
    print(f"  [{r['seq']}] {r['created_at']} {r['type']} {r['tool']}: {r['content'] or ''}")
    if r['input_json']:
        print(f"    input: {r['input_json'][:200]}")
    print()

# Coding 最后10条日志
c.execute("SELECT seq, type, tool, LEFT(content,300) as content, LEFT(input_json,400) as input_json, created_at FROM task_messages WHERE task_id=%s ORDER BY seq DESC LIMIT 10", (coding_task,))
rows = c.fetchall()
print(f"=== Coding 最后10条日志 ===")
for r in rows:
    print(f"  [{r['seq']}] {r['created_at']} {r['type']} {r['tool']}: {r['content'] or ''}")
    if r['input_json']:
        print(f"    input: {r['input_json'][:200]}")
    print()

# Coding 总日志数
c.execute("SELECT COUNT(*) as cnt FROM task_messages WHERE task_id=%s", (coding_task,))
cnt = c.fetchone()
print(f"Coding 总日志数: {cnt['cnt']}")

# Coding type 分布
c.execute("SELECT type, COUNT(*) as cnt FROM task_messages WHERE task_id=%s GROUP BY type", (coding_task,))
rows = c.fetchall()
print("\n=== Coding type 分布 ===")
for r in rows:
    print(f"  {r['type']}: {r['cnt']}")

db.close()
