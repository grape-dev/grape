#!/bin/bash
cd /opt/grape
PREV_STATUS=""
while true; do
    OUTPUT=$(.venv/bin/python3 -c "
import pymysql, json
from datetime import datetime, timezone
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor()
now_utc = datetime.now(timezone.utc)
ts = now_utc.strftime('%H:%M:%S')

# Issue #5 SRE task (latest)
cur.execute(\"\"\"
    SELECT status, exit_code, runtime_id, started_at, attempts,
           LEFT(result_text,150) as result_preview, LEFT(errors,150) as errors_preview
    FROM agent_tasks 
    WHERE issue_id='#5' AND repo_key LIKE '%mm6667%' AND agent_type='sre'
    ORDER BY created_at DESC LIMIT 1
\"\"\")
row = cur.fetchone()
if row:
    status, exit_code, runtime_id, started_at, attempts, result, errors = row
    elapsed = '?'
    if started_at:
        try:
            st = datetime.fromisoformat(started_at)
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            elapsed = f'{round((now_utc - st).total_seconds(), 0):.0f}s'
        except:
            pass
    # Compact one-line status
    line = f'{ts} | SRE status={status} exit={exit_code} rt={runtime_id} attempts={attempts} elapsed={elapsed}'
    if result:
        line += f' result={result[:80]}'
    if errors:
        line += f' errors={errors[:80]}'
    print(line)

# Active task count
cur.execute(\"SELECT COUNT(*) FROM agent_tasks WHERE status IN ('running','dispatched','queued')\")
active = cur.fetchone()[0]
print(f'{ts} | 活跃任务: {active}')

# issue_state
cur.execute(\"SELECT stage FROM issue_state WHERE repo_key LIKE '%mm6667/HELLO-BLOG-SYS%' AND issue_id='#5'\")
st = cur.fetchone()
if st:
    print(f'{ts} | issue_state.stage = {st[0]}')

conn.close()
" 2>&1)
    echo "$OUTPUT"
    # Check if status changed to completed or failed
    if echo "$OUTPUT" | grep -qE "status=completed|status=failed"; then
        echo "=== SRE 终态! 停止监控 ==="
        break
    fi
    sleep 30
done
