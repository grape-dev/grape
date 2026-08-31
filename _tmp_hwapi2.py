import sys, json, requests, hashlib, hmac, time, base64
from datetime import datetime, timezone
sys.path.insert(0, "src")
from src.session_manager import db

# 获取华为云凭据
rows = db.execute("SELECT huawei_cloud_ak, huawei_cloud_sk FROM user_accounts WHERE id=1").fetchall()
AK = None
SK = None
for r in rows:
    d = r if isinstance(r, dict) else dict(zip(["huawei_cloud_ak","huawei_cloud_sk"], r))
    AK = d["huawei_cloud_ak"]
    SK = d["huawei_cloud_sk"]

print(f"AK={AK[:8]}... SK={SK[:8]}...")

# SDK 方式获取 token — 使用 sigv4 签名
# 简化: 直接用 AK/SK 请求 ECS API (Ku peng SDK 签名)
# 华为云 AK/SK 认证需要使用 WSSDK 签名算法 (HWS签名)
# 尝试使用 simpler 方式: application/json + 签名头

def hmac_sha256(key, msg):
    return hmac.new(key.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()

def sha256_hex(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

# 尝试使用华为云 SDK
try:
    from huaweicloudsdkcore.auth.credentials import BasicCredentials
    from huaweicloudsdkcore.region.region import Region
    from huaweicloudsdkecs.v2 import EcsClient, ListServersDetailsRequest
    cred = BasicCredentials(ak=AK, sk=SK, project_id="3c9ee3aff637402b8c3942b57eb40f35")
    region = Region("cn-north-4", "https://ecs.cn-north-4.myhuaweicloud.com")
    client = EcsClient.new_builder().with_credentials(cred).with_region(region).build()
    req = ListServersDetailsRequest(limit=50)
    resp = client.list_servers_details(req)
    servers = resp.servers or []
    print(f"\nECS 总数: {len(servers)}")
    for s in servers:
        name = s.name or ""
        sid = s.id or ""
        status = s.status or ""
        addrs = s.addresses or {}
        ips = []
        for net, addr_list in (addrs.items() if isinstance(addrs, dict) else []):
            for a in (addr_list if isinstance(addr_list, list) else []):
                if isinstance(a, dict):
                    ips.append(a.get('addr', ''))
        print(f"  {name:<40} id={sid[:12]}... status={status} ips={ips}")
except ImportError as e:
    print(f"华为云 SDK 未安装: {e}")
    print("尝试直接用 REST API + 签名...")
    
    # 手动实现华为云签名 (HWS V4)
    # 太复杂，先看看有没有简单的 token 方式
    # 尝试用 AK/SK 的 secret 方式获取 token
    import urllib.request
    
    # 方法2: 直接用已有 IAM token (从 grape.env 或其他地方)
    print("没有华为云 SDK，尝试从 grape 的配置中获取 token...")
