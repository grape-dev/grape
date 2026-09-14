import pymysql

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)

# server_base_url from meta
c.execute("SELECT `key`, `value` FROM meta WHERE `key`=%s", ("server_base_url",))
row = c.fetchone()
if row:
    print(f"server_base_url = {row['value']}")
else:
    print("server_base_url NOT FOUND in meta")

# runtime 2135 credential
print()
print("=== runtime 2135 ===")
print("  credential_id: 84")
print("  last_ip: 1.92.80.69 (not 127.x, so _resolve_server_url returns public_url)")

# 看看 agent 尝试访问的地址
print()
print("=== 分析 ===")
print("server_url 字段(agent_tasks): http://123.249.19.227")
print("prompt 里 A2A 地址: http://123.249.19.227/api/a2a")
print("prompt 里 analyze-image 地址: http://123.249.19.227/api/analyze-image")
print("附件 URL: https://obs-grape-dev-file.obs.cn-north-4.myhuaweicloud.com/...")
print()
print("worker 节点: 1.92.80.69")
print("服务端公网: 123.249.19.227")
print()

# 看 get_server_base_url 的实现
db.close()
