import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

coding_task = "t_cac951ca6ff3"

# push 相关的日志（seq 139-177）
c.execute("SELECT seq, type, tool, content, LEFT(input_json,800) as input_json, created_at FROM task_messages WHERE task_id=%s AND seq >= 139 ORDER BY seq ASC", (coding_task,))
rows = c.fetchall()
print(f"=== Coding push 相关日志 ({len(rows)}) ===")
for r in rows:
    print(f"\n[{r['seq']}] {r['created_at']} {r['type']} {r['tool']}")
    if r['content']:
        print(f"  content: {r['content']}")
    if r['input_json']:
        print(f"  input: {r['input_json']}")

db.close()
