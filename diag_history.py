import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 HELLO-BLOG-SYS-5 仓库历史上成功的 coding 任务
c.execute("""
    SELECT e.seq, e.issue_id, e.title, e.status, e.exit_code, e.duration, e.created_at, e.completed_at,
           LEFT(e.result, 200) as result
    FROM events e
    WHERE e.repo_key LIKE '%HELLO-BLOG-SYS-5%'
      AND e.agent_type = 'coding'
    ORDER BY e.seq ASC
""")
rows = c.fetchall()
print(f"=== HELLO-BLOG-SYS-5 coding events ({len(rows)}) ===")
for r in rows:
    print(f"  seq={r['seq']} issue={r['issue_id']} status={r['status']} exit_code={r['exit_code']} duration={r['duration']} created={r['created_at']}")
    print(f"    title: {r['title']}")
    print(f"    result: {r['result']}")
    print()

# 查其他仓库成功的 coding 任务，看 token 是否一样
c.execute("""
    SELECT e.seq, e.repo_key, e.issue_id, e.status, e.exit_code, e.created_at,
           t.repo_gitcode_token, LENGTH(t.repo_gitcode_token) as tok_len
    FROM events e
    JOIN agent_tasks t ON t.event_seq = e.seq
    WHERE e.agent_type = 'coding' AND e.status = 'completed' AND e.exit_code = 0
    ORDER BY e.seq DESC
    LIMIT 10
""")
rows = c.fetchall()
print(f"=== 最近成功的 coding 任务 ===")
for r in rows:
    tok = r['repo_gitcode_token'] or ''
    print(f"  seq={r['seq']} repo={r['repo_key']} tok_len={r['tok_len']} tok_preview={tok[:10]}... created={r['created_at']}")

db.close()
