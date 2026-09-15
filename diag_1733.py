import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 event #1733
c.execute("SELECT seq, repo_key, issue_id, title, agent_type, skill, status, exit_code, duration, LEFT(result,500) as result, created_at, completed_at FROM events WHERE seq=1733")
e = c.fetchone()
if e:
    print("=== EVENT #1733 ===")
    for k,v in e.items():
        print(f"  {k}: {v}")

# 查 agent_task
c.execute("SELECT id, agent_type, skill, status, exit_code, LEFT(result_text,500) as result_text, LEFT(errors,300) as errors, started_at, completed_at FROM agent_tasks WHERE event_seq=1733")
t = c.fetchone()
if t:
    print(f"\n=== AGENT_TASK ===")
    for k,v in t.items():
        print(f"  {k}: {v}")

    # prompt 附件注入
    c.execute("SELECT LEFT(prompt,8000) as prompt FROM agent_tasks WHERE id=%s", (t['id'],))
    p = c.fetchone()
    prompt = p['prompt'] if p else ''
    if '用户附件' in prompt:
        idx = prompt.find('用户附件')
        print(f"\n=== prompt 附件注入 ===")
        print(prompt[max(0,idx-50):idx+400])
    else:
        print(f"\n=== prompt 无附件注入 ===")

    # 附件/视觉分析相关日志
    c.execute("SELECT seq, type, tool, LEFT(content,300) as content, LEFT(input_json,500) as input_json, created_at FROM task_messages WHERE task_id=%s AND (input_json LIKE %s OR input_json LIKE %s OR input_json LIKE %s OR content LIKE %s) ORDER BY seq ASC", (t['id'], "%analyze-image%", "%obs%", "%curl%", "%附件%"))
    rows = c.fetchall()
    print(f"\n=== 附件/视觉分析日志 ({len(rows)}) ===")
    for r in rows:
        print(f"\n  [{r['seq']}] {r['created_at']} {r['type']} {r['tool']}: {r['content'] or ''}")
        if r['input_json']:
            print(f"    input: {r['input_json'][:300]}")

    # text 消息（agent 结论）
    c.execute("SELECT seq, LEFT(content,400) as content, created_at FROM task_messages WHERE task_id=%s AND type='text' ORDER BY seq ASC", (t['id'],))
    rows = c.fetchall()
    print(f"\n=== text messages ({len(rows)}) ===")
    for r in rows:
        print(f"\n  [{r['seq']}] {r['created_at']}: {r['content']}")

db.close()
