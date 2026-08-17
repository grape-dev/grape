#!/usr/bin/env python
"""Re-run failed engine_unavailable events by replaying stored payload through run_pipeline.

Same path as the webhook handler (run_pipeline), bypassing HTTP auth.
Only re-runs events whose issue is still pending (no later success for that issue).
"""
import sys, json
sys.path.insert(0, '/opt/grape')
import pymysql
from src.api.event_pipeline import run_pipeline

cfg = json.load(open('/opt/grape/configs/db-backend.json'))['mysql']
conn = pymysql.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'],
                       password=cfg['password'], database=cfg['database'],
                       charset='utf8mb4', autocommit=True, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

# (seq, repo_key, issue_id) — engine_unavailable + still pending
targets = [
    (562, 'gitcode:songspace/shelldev', '#8'),
    (574, 'gitcode:songspace/shelldev', '#9'),
    (608, 'gitcode:songspace/shelldev', '#10'),
    (479, 'gitcode:XOcto/redbook-operate', '#3'),
]

for seq, repo_key, issue_id in targets:
    cur.execute("SELECT payload FROM events WHERE seq=%s", (seq,))
    row = cur.fetchone()
    if not row or not row['payload']:
        print(f"seq {seq}: NO PAYLOAD, skip")
        continue
    payload = json.loads(row['payload'])
    owner_repo = repo_key.replace('gitcode:', '')
    headers = {"X-GitCode-Event": "Issue Hook", "X-GitCode-Token": "replay"}
    try:
        result = run_pipeline(json.dumps(payload).encode('utf-8'), headers,
                              account=owner_repo, repo_key_override=repo_key)
        print(f"seq {seq} ({issue_id}): replayed -> new seq={result.get('seq')} ok={result.get('ok')} agent={result.get('skill')}")
    except Exception as e:
        import traceback
        print(f"seq {seq} ({issue_id}): FAILED to replay: {e}")
        traceback.print_exc()
conn.close()
