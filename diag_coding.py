import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

coding_task = "t_cac951ca6ff3"

# 看 text 类型的消息（agent 的输出/结论）
c.execute("SELECT seq, LEFT(content,500) as content, created_at FROM task_messages WHERE task_id=%s AND type='text' ORDER BY seq ASC", (coding_task,))
rows = c.fetchall()
print(f"=== Coding text messages ({len(rows)}) ===")
for r in rows:
    print(f"  [{r['seq']}] {r['created_at']}: {r['content']}")
    print()

# 看 error 类型的消息
c.execute("SELECT seq, LEFT(content,500) as content, created_at FROM task_messages WHERE task_id=%s AND type='error' ORDER BY seq ASC", (coding_task,))
rows = c.fetchall()
print(f"=== Coding error messages ({len(rows)}) ===")
for r in rows:
    print(f"  [{r['seq']}] {r['created_at']}: {r['content']}")

# 看 tool_use bash 的命令（看 agent 在干什么）
c.execute("SELECT seq, LEFT(input_json,300) as input_json, created_at FROM task_messages WHERE task_id=%s AND type='tool_use' AND tool='bash' ORDER BY seq ASC", (coding_task,))
rows = c.fetchall()
print(f"\n=== Coding bash commands ({len(rows)}) ===")
for r in rows:
    print(f"  [{r['seq']}] {r['input_json']}")
    print()

db.close()
