import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 event #1736
c.execute("SELECT seq, repo_key, issue_id, title, agent_type, skill, status, exit_code, duration, LEFT(result,500) as result, created_at, completed_at FROM events WHERE seq=1736")
e = c.fetchone()
if e:
    print("=== EVENT #1736 ===")
    for k,v in e.items():
        print(f"  {k}: {v}")

# 查 agent_task
c.execute("SELECT id, agent_type, skill, status, exit_code, LEFT(result_text,500) as result_text, LEFT(errors,500) as errors, started_at, completed_at FROM agent_tasks WHERE event_seq=1736")
t = c.fetchone()
if t:
    print(f"\n=== AGENT_TASK ===")
    for k,v in t.items():
        print(f"  {k}: {v}")

    # prompt 检查附件注入
    c.execute("SELECT LEFT(prompt,8000) as prompt FROM agent_tasks WHERE id=%s", (t['id'],))
    p = c.fetchone()
    prompt = p['prompt'] if p else ''
    if '用户附件' in prompt:
        idx = prompt.find('用户附件')
        print(f"\n=== prompt 有附件注入 ===")
        print(prompt[max(0,idx-50):idx+400])
    else:
        print(f"\n=== prompt 无附件注入 ===")
        # 看 issue body
        if 'Body:' in prompt:
            idx = prompt.find('Body:')
            print(f"\n=== issue body ===")
            print(prompt[idx:idx+500])

    # 所有日志
    c.execute("SELECT seq, type, tool, LEFT(content,200) as content, LEFT(input_json,300) as input_json, created_at FROM task_messages WHERE task_id=%s ORDER BY seq ASC", (t['id'],))
    rows = c.fetchall()
    print(f"\n=== 全部日志 ({len(rows)}) ===")
    for r in rows:
        print(f"  [{r['seq']}] {r['created_at']} {r['type']} {r['tool']}: {r['content'] or ''}")
        if r['input_json']:
            print(f"    input: {r['input_json'][:200]}")

db.close()
