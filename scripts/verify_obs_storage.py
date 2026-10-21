#!/usr/bin/env python3
"""ObsStorage 端到端连通性验证（华为云 OBS 测试桶 obs-grape-dev-file）。

只操作配置桶内 __probe__ 前缀的对象，验证后清理，不越权。
"""
import os
import sys

# OBS 运行桶凭据不在代码里（2026-10-21 起）：读环境变量或 /etc/grape/grape.env。
# 见 scripts/obs_credentials.py 与守卫 scripts/check-secrets.py。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from obs_credentials import apply_runtime_obs_env  # noqa: E402

os.environ["OBS_ENABLED"] = "true"
os.environ["OBS_BUCKET"] = "obs-grape-dev-file"
os.environ["OBS_ENDPOINT"] = "https://obs.cn-north-4.myhuaweicloud.com"
os.environ["OBS_PREFIX"] = "grape/test"
apply_runtime_obs_env()  # 注入 OBS_ACCESS_KEY / OBS_SECRET_KEY（缺失则明确报错）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import get_storage, reset_storage, skill_key, parse_skill_key  # noqa: E402

reset_storage()
storage = get_storage()
assert storage is not None and storage.backend_name() == "obs", "ObsStorage 未启用"

PROBE_KEY = "__probe__/connectivity.txt"
try:
    # 1) save_bytes
    storage.save_bytes(PROBE_KEY, b"grape-obs-probe-123")
    print("[1] save_bytes OK")

    # 2) read_bytes
    data = storage.read_bytes(PROBE_KEY)
    assert data == b"grape-obs-probe-123", f"read mismatch: {data!r}"
    print("[2] read_bytes OK")

    # 3) exists
    assert storage.exists(PROBE_KEY) is True
    print("[3] exists OK")

    # 4) temporary_url（签名 URL，仅 GET）
    url = storage.temporary_url(PROBE_KEY, expires=300)
    assert url.startswith("https://") and "grape/test/__probe__" in url
    print("[4] temporary_url OK ->", url[:110])

    # 5) save_stream（文件上传）
    tmp = "/tmp/obs_probe_stream.txt"
    with open(tmp, "wb") as f:
        f.write(b"stream-probe")
    storage.save_stream("__probe__/stream.txt", tmp)
    assert storage.read_bytes("__probe__/stream.txt") == b"stream-probe"
    print("[5] save_stream OK")

    # 6) list_keys（前缀枚举）
    keys = storage.list_keys("__probe__")
    assert len(keys) == 2 and all(k.startswith("__probe__/") for k in keys), keys
    print("[6] list_keys OK ->", keys)

    # 7) skill_key 生成 + parse
    k = skill_key("custom", "123", "hello-skill", 2)
    info = parse_skill_key(k)
    assert info["skill_name"] == "hello-skill" and info["version"] == 2, info
    print("[7] skill_key OK ->", k)

    # 8) delete_prefix（清理，不越权）
    n = storage.delete_prefix("__probe__")
    assert n == 2 and storage.list_keys("__probe__") == [], f"cleanup failed: {n}"
    print("[8] delete_prefix OK（清理完成，桶内无残留）")

    print("\n=== ObsStorage 连通性验证全部通过（测试桶 obs-grape-dev-file）===")
except Exception as e:
    # 清理残留
    try:
        storage.delete_prefix("__probe__")
    except Exception:
        pass
    print(f"FAILED: {e}")
    sys.exit(1)
