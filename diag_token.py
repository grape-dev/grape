import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 managed_repos 的 bot_gitcode_token
c.execute("SELECT repo_key, bot_gitcode_token, LENGTH(bot_gitcode_token) as token_len FROM managed_repos WHERE repo_key LIKE '%HELLO-BLOG-SYS-5%'")
rows = c.fetchall()
print("=== managed_repos ===")
for r in rows:
    tok = r['bot_gitcode_token'] or ''
    print(f"  repo: {r['repo_key']}")
    print(f"  token_len: {r['token_len']}")
    print(f"  token_preview: {tok[:10]}...{tok[-5:] if len(tok)>5 else tok}")

# 查 agent_tasks 里这个任务的 repo_gitcode_token
c.execute("SELECT id, event_seq, LEFT(repo_gitcode_token,10) as tok_preview, LENGTH(repo_gitcode_token) as tok_len FROM agent_tasks WHERE event_seq=1720")
rows = c.fetchall()
print("\n=== agent_tasks event_seq=1720 ===")
for r in rows:
    print(f"  id: {r['id']}")
    print(f"  tok_len: {r['tok_len']}")
    print(f"  tok_preview: {r['tok_preview']}...")

# 看 global bot token
c.execute("SELECT * FROM meta WHERE `key` LIKE '%token%' OR `key` LIKE '%bot%'")
rows = c.fetchall()
print("\n=== meta tokens ===")
for r in rows:
    val = r['value'] or ''
    print(f"  {r['key']}: len={len(val)}, preview={val[:10]}...")

db.close()
