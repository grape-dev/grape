#!/bin/bash
cd /opt/grape
while true; do
    .venv/bin/python3 -c "
import pymysql, json
from datetime import datetime, timezone
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()
now_utc = datetime.now(timezone.utc)
ts = now_utc.strftime('%H:%M:%S')

# Latest SRE task
cur.execute(\"\"\"
    SELECT status, exit_code, runtime_id, started_at, attempts,
           LEFT(result_text,150) as result, LEFT(errors,150) as errors
    FROM agent_tasks 
    WHERE issue_id='#5' AND repo_key LIKE '%mm6667%' AND agent_type='sre'
    AND created_at > '2026-08-24T15:25:00'
    ORDER BY created_at LIMIT 1
\"\"\")
row = cur.fetchone()
if row:
    status, exit_code, rt, started, attempts, result, errors = row
    elapsed = '?'
    if started:
        try:
            st = datetime.fromisoformat(started)
            if st.tzinfo is None: st = st.replace(tzinfo=timezone.utc)
            elapsed = f'{round((now_utc-st).total_seconds(),0):.0f}s'
        except: pass
    line = f'{ts} | SRE status={status} exit={exit_code} rt={rt} elapsed={elapsed} attempts={attempts}'
    if result: line += f' result={result[:80]}'
    if errors: line += f' errors={errors[:80]}'
    print(line)

# Latest task_messages (last 3 steps)
cur.execute(\"\"\"
    SELECT tm.seq, tm.type, tm.tool, LEFT(tm.content,80), tm.created_at
    FROM task_messages tm
    JOIN agent_tasks at ON tm.task_id = at.id
    WHERE at.issue_id='#5' AND at.repo_key LIKE '%mm6667%' AND at.agent_type='sre'
    AND at.created_at > '2026-08-24T15:25:00'
    ORDER BY tm.created_at DESC LIMIT 5
\"\"\")
for r in cur.fetchall():
    desc = r[3] if r[3] else (r[2] or r[1])
    print(f'  [{r[4][-8:]}] seq={r[0]} {r[1]} {r[2] or \"\"} {desc[:60]}')

conn.close()
" 2>&1
    echo "---"
    # Check if SRE reached terminal state
    STATUS=$(.venv/bin/python3 -c "
import pymysql
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()
cur.execute(\"SELECT status FROM agent_tasks WHERE issue_id='#5' AND repo_key LIKE '%mm6667%' AND agent_type='sre' AND created_at > '2026-08-24T15:25:00' ORDER BY created_at LIMIT 1\")
print(cur.fetchone()[0])
conn.close()
" 2>/dev/null)
    if echo "$STATUS" | grep -qE "completed|failed"; then
        echo "=== SRE 终态: $STATUS ==="
        break
    fi
    sleep 30
done
