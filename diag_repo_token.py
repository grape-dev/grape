import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查所有 HELLO-BLOG-SYS-5 仓库的 managed_repos 记录
c.execute("SELECT id, repo_key, bot_gitcode_token, LENGTH(bot_gitcode_token) as tok_len, created_at FROM managed_repos WHERE repo_key LIKE '%HELLO-BLOG-SYS%'")
rows = c.fetchall()
print(f"=== managed_repos HELLO-BLOG-SYS* ({len(rows)}) ===")
for r in rows:
    tok = r['bot_gitcode_token'] or ''
    print(f"  id={r['id']} repo={r['repo_key']} tok_len={r['tok_len']} tok={tok[:15]}... created={r['created_at']}")

# 看看之前成功的 token Y3Y74xy 在哪些仓库配过
c.execute("SELECT id, repo_key, bot_gitcode_token, created_at FROM managed_repos WHERE bot_gitcode_token LIKE 'Y3Y74xy%'")
rows = c.fetchall()
print(f"\n=== repos with Y3Y74xy token ===")
for r in rows:
    print(f"  id={r['id']} repo={r['repo_key']} tok={r['bot_gitcode_token'][:15]}... created={r['created_at']}")

# 看 HELLO-BLOG-SYS-5 有没有历史更新记录
# 检查 devops_agent_session 里有没有 token 痕迹
c.execute("SELECT DISTINCT repo_gitcode_token FROM devops_agent_session WHERE repo_key LIKE '%HELLO-BLOG-SYS-5%' AND repo_gitcode_token != '' LIMIT 10")
rows = c.fetchall()
print(f"\n=== devops_agent_session tokens for HELLO-BLOG-SYS-5 ===")
for r in rows:
    tok = r['repo_gitcode_token'] or ''
    print(f"  tok={tok[:15]}... len={len(tok)}")

db.close()
