import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

task_id = "t_7d0d831bb2f8"

# 看 analyze-image 相关的完整日志
c.execute("SELECT seq, type, tool, content, LEFT(input_json,600) as input_json, created_at FROM task_messages WHERE task_id=%s AND (input_json LIKE %s OR input_json LIKE %s OR content LIKE %s) ORDER BY seq ASC", (task_id, "%analyze-image%", "%obs%", "%403%"))
rows = c.fetchall()
print(f"=== analyze-image / 403 相关日志 ({len(rows)}) ===")
for r in rows:
    print(f"\n[{r['seq']}] {r['created_at']} {r['type']} {r['tool']}")
    if r['content']:
        print(f"  content: {r['content']}")
    if r['input_json']:
        print(f"  input: {r['input_json']}")

# 看 text 消息（agent 结论）
c.execute("SELECT seq, LEFT(content,600) as content, created_at FROM task_messages WHERE task_id=%s AND type='text' ORDER BY seq ASC", (task_id,))
rows = c.fetchall()
print(f"\n=== text messages ({len(rows)}) ===")
for r in rows:
    print(f"\n[{r['seq']}] {r['created_at']}: {r['content']}")

db.close()
