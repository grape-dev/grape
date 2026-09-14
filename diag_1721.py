import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# 查 event #1721
c.execute("SELECT seq, repo_key, issue_id, title, agent_type, skill, status, exit_code, duration, LEFT(result,400) as result, created_at, completed_at FROM events WHERE seq=1721")
e = c.fetchone()
if e:
    print("=== EVENT #1721 ===")
    for k,v in e.items():
        print(f"  {k}: {v}")

# 查 agent_task
c.execute("SELECT id, agent_type, skill, status, exit_code, LEFT(result_text,400) as result_text, LEFT(errors,200) as errors, started_at, completed_at FROM agent_tasks WHERE event_seq=1721")
t = c.fetchone()
if t:
    print(f"\n=== AGENT_TASK ===")
    for k,v in t.items():
        print(f"  {k}: {v}")

    # 查 task_messages 里的 analyze-image / VLM 相关
    c.execute("SELECT seq, type, tool, LEFT(content,200) as content, LEFT(input_json,400) as input_json, created_at FROM task_messages WHERE task_id=%s AND (input_json LIKE %s OR input_json LIKE %s OR input_json LIKE %s OR content LIKE %s OR content LIKE %s) ORDER BY seq ASC", (t['id'], "%analyze-image%", "%vlm%", "%vision%", "%analyze-image%", "%视觉%"))
    rows = c.fetchall()
    print(f"\n=== 视觉分析相关日志 ({len(rows)}) ===")
    for r in rows:
        print(f"  [{r['seq']}] {r['type']} {r['tool']}: {r['content'] or ''}")
        if r['input_json']:
            print(f"    input: {r['input_json'][:200]}")
        print()

    # 也看看 prompt 里有没有附件注入（附件会触发 analyze-image 指引）
    c.execute("SELECT LEFT(prompt, 6000) as prompt FROM agent_tasks WHERE id=%s", (t['id'],))
    p = c.fetchone()
    if p and p['prompt']:
        prompt = p['prompt']
        if '用户附件' in prompt or 'analyze-image' in prompt:
            idx = prompt.find('用户附件')
            if idx == -1:
                idx = prompt.find('analyze-image')
            print(f"=== prompt 附件/视觉分析注入 ===")
            print(prompt[max(0,idx-50):idx+300])
        else:
            print("=== prompt 无附件/视觉分析注入 ===")

db.close()
