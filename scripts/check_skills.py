#!/usr/bin/env python3
"""Check current skills in DB."""
import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

# Describe skill table
cur.execute("DESCRIBE skill")
print("=== skill table columns ===")
print([r['Field'] for r in cur.fetchall()])

# Check skills
cur.execute("SELECT * FROM skill ORDER BY id LIMIT 20")
rows = cur.fetchall()
print(f"\nTotal skills: {len(rows)}")
for r in rows:
    print(json.dumps(r, default=str, ensure_ascii=False))

conn.close()
