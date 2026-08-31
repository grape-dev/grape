import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
                       password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()

# Get ALL task_messages for mm6667 SRE tasks, ordered by time
# This is the step-by-step execution trace from the worker
cur.execute("""
    SELECT tm.task_id, tm.seq, tm.type, tm.tool, 
           LEFT(tm.content,300) as content,
           LEFT(tm.input_json,200) as input_json,
           LEFT(tm.output,200) as output,
           tm.created_at
    FROM task_messages tm
    JOIN agent_tasks at ON tm.task_id = at.id
    WHERE at.repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' 
    AND at.agent_type='sre'
    AND at.issue_id IN ('#3','#5')
    ORDER BY at.issue_id, tm.created_at
""")
cols = [d[0] for d in cur.description]
rows = cur.fetchall()
print(f"=== SRE task_messages 总计 {len(rows)} 条 ===\n")

current_issue = ""
current_task = ""
for row in rows:
    d = dict(zip(cols, row))
    if d['task_id'] != current_task:
        current_task = d['task_id']
        print(f"\n{'='*60}")
        print(f"Task: {current_task}")
        print(f"{'='*60}")
    
    ts = d.get('created_at','')
    typ = d.get('type','')
    tool = d.get('tool','')
    content = d.get('content','') or ''
    inp = d.get('input_json','') or ''
    out = d.get('output','') or ''
    
    if tool:
        print(f"\n[{ts}] 🔧 {tool}")
        if inp and len(inp) > 5:
            print(f"  input: {inp[:150]}")
        if out and len(out) > 5:
            print(f"  output: {out[:150]}")
    elif typ == 'text':
        print(f"\n[{ts}] 💬 {content[:200]}")
    elif typ == 'step_start':
        print(f"\n[{ts}] ▶ {content[:100]}")
    elif typ == 'step_finish':
        print(f"\n[{ts}] ✅ {content[:100]}")
    elif typ == 'error':
        print(f"\n[{ts}] ❌ {content[:200]}")
    elif typ == 'reasoning':
        print(f"\n[{ts}] 🤔 {content[:150]}")
    else:
        print(f"\n[{ts}] [{typ}] {content[:150]}")

conn.close()
