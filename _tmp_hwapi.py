import sys, json, requests, hashlib, time
sys.path.insert(0, "src")
from src.session_manager import db

# 获取华为云凭据
rows = db.execute("SELECT huawei_cloud_ak, huawei_cloud_sk FROM user_accounts WHERE id=1").fetchall()
for r in rows:
    d = r if isinstance(r, dict) else dict(zip(["huawei_cloud_ak","huawei_cloud_sk"], r))
    AK = d["huawei_cloud_ak"]
    SK = d["huawei_cloud_sk"]

print(f"AK={AK[:8]}... SK={SK[:8]}...")

# 获取华为云 IAM token
iam_url = "https://iam.cn-north-4.myhuaweicloud.com/v3/auth/tokens"
payload = {
    "auth": {
        "identity": {
            "methods": ["hw_ak_sk"],
            "hw_ak_sk": {
                "access": {"key": AK},
                "secret": {"key": SK}
            }
        },
        "scope": {
            "project": {
                "id": "3c9ee3aff637402b8c3942b57eb40f35"
            }
        }
    }
}
resp = requests.post(iam_url, json=payload, timeout=10)
token = resp.headers.get("X-Subject-Token", "")
print(f"IAM token 获取: {'OK' if token else 'FAIL'} (status={resp.status_code})")

if token:
    # 查询 ECS 列表
    ecs_url = "https://ecs.cn-north-4.myhuaweicloud.com/v1/3c9ee3aff637402b8c3942b57eb40f35/cloudservers/detail"
    headers = {"X-Auth-Token": token}
    resp2 = requests.get(ecs_url, headers=headers, timeout=10)
    data = resp2.json()
    servers = data.get("servers", [])
    print(f"\nECS 总数: {len(servers)}")
    for s in servers:
        name = s.get("name", "")
        sid = s.get("id", "")
        status = s.get("status", "")
        addrs = s.get("addresses", {})
        ips = []
        for net, addr_list in addrs.items():
            for a in addr_list:
                ips.append(f"{a.get('version','v4')}:{a.get('addr','')}")
        print(f"  {name:<40} id={sid[:12]}... status={status} ips={ips}")
