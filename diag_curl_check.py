import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

task_id = "t_f4baefcac1a2"

# 看 analyze-image 相关的完整 curl 命令
c.execute("SELECT seq, type, tool, LEFT(input_json,600) as input_json, created_at FROM task_messages WHERE task_id=%s AND (input_json LIKE %s OR input_json LIKE %s) ORDER BY seq ASC", (task_id, "%analyze-image%", "%obs%"))
rows = c.fetchall()
print(f"=== analyze-image / obs 日志 ({len(rows)}) ===")
for r in rows:
    print(f"\n[{r['seq']}] {r['created_at']} {r['type']} {r['tool']}")
    if r['input_json']:
        print(f"  input: {r['input_json']}")

db.close()
