import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

c.execute("""
    SELECT e.seq, e.issue_id, e.status, e.exit_code, e.created_at,
           t.repo_gitcode_token, LENGTH(t.repo_gitcode_token) as tok_len
    FROM events e
    JOIN agent_tasks t ON t.event_seq = e.seq
    WHERE e.repo_key LIKE '%HELLO-BLOG-SYS-5%'
      AND e.agent_type = 'coding'
    ORDER BY e.seq ASC
""")
rows = c.fetchall()
print("=== HELLO-BLOG-SYS-5 coding token history ===")
for r in rows:
    tok = r['repo_gitcode_token'] or ''
    print(f"  seq={r['seq']} issue={r['issue_id']} status={r['status']} exit={r['exit_code']} tok_len={r['tok_len']} tok={tok[:15]}... created={r['created_at']}")

db.close()
