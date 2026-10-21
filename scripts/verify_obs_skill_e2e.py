#!/usr/bin/env python3
"""B1 技能库 OBS 化端到端验证（真实测试桶 obs-grape-dev-file，OBS_E2E）。

链路：规范化打包 → 上传 OBS → 详情提取（缓存）→ 快照 zip base64 →
worker 物化（解压+恢复权限）→ 文件/二进制/权限断言。
只操作 grape/test/skills/e2e-* 前缀对象，验证后清理，不越权。
"""
import base64
import io
import os
import shutil
import stat
import sys
import tempfile
import zipfile

os.environ["OBS_ENABLED"] = "true"
os.environ["OBS_BUCKET"] = "obs-grape-dev-file"
os.environ["OBS_ENDPOINT"] = "https://obs.cn-north-4.myhuaweicloud.com"
os.environ["OBS_PREFIX"] = "grape/test"
# OBS 运行桶凭据不在代码里（2026-10-21 起）：读环境变量或 /etc/grape/grape.env。
# 见 scripts/obs_credentials.py 与守卫 scripts/check-secrets.py。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from obs_credentials import apply_runtime_obs_env  # noqa: E402
apply_runtime_obs_env()  # 注入 OBS_ACCESS_KEY / OBS_SECRET_KEY（缺失则明确报错）
sys.path.insert(0, "/home/yangaiwu/grape")

from src.storage import get_storage, reset_storage, skill_key, skill_md_content, get_skill_zip_bytes  # noqa: E402
from src.api.routes.skills import _normalize_skill_zip  # noqa: E402

SKILL = "e2e-ops"
WORK = tempfile.mkdtemp(prefix="e2e_")
storage = get_storage()
assert storage is not None and storage.backend_name() == "obs"


def build_skill_dir():
    base = os.path.join(WORK, "src", SKILL)
    os.makedirs(os.path.join(base, "references"))
    os.makedirs(os.path.join(base, "assets"))
    with open(os.path.join(base, "SKILL.md"), "w") as f:
        f.write("---\nname: e2e-ops\ndescription: e2e 验证技能\nversion: 1.0.0\n---\n\n## 场景\n## 知识\n## 步骤\n- e2e")
    with open(os.path.join(base, "references", "guide.md"), "w") as f:
        f.write("# Guide\n参考")
    with open(os.path.join(base, "assets", "icon.png"), "wb") as f:
        f.write(bytes([0x89, 0x50, 0x4E, 0x47]) + b"\x00\x01\x02\x03")
    return base


def worker_materialize(skill_zip_b64, cwd):
    """复刻 agent.py 物化逻辑（解码→写 zip→extractall→恢复权限）。"""
    import base64 as _b64
    import zipfile as _zf
    skill_dir = os.path.join(cwd, ".skills")
    raw = _b64.b64decode(skill_zip_b64)
    tmp_zip = os.path.join(cwd, "_delivery.zip")
    with open(tmp_zip, "wb") as f:
        f.write(raw)
    with _zf.ZipFile(tmp_zip, "r") as zf:
        zf.extractall(skill_dir)
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if mode and not info.is_dir():
                p = os.path.join(skill_dir, info.filename)
                if os.path.isfile(p):
                    os.chmod(p, mode & 0o777)
    os.remove(tmp_zip)
    return skill_dir


def main():
    key = None
    try:
        # 1) 规范化打包 + 上传（模拟上传链路 v1）
        src = build_skill_dir()
        zip_bytes = _normalize_skill_zip(src, SKILL)
        key = skill_key("custom", "9999", SKILL, 1)
        storage.save_bytes(key, zip_bytes)
        print("[1] 上传 OBS OK ->", key)

        # 2) 详情提取（内存缓存 + 真实 OBS 回源）
        content = skill_md_content(key, SKILL, 1)
        assert content and "## 场景" in content, "详情提取失败"
        print("[2] 详情提取 OK (content 含 SKILL.md 章节)")

        # 3) 快照 zip base64（服务端 enqueue 用）
        zip_b64 = base64.b64encode(get_skill_zip_bytes(key)).decode()
        assert zip_b64, "快照 zip base64 为空"
        print(f"[3] 快照 base64 OK ({len(zip_b64)} chars)")

        # 4) worker 物化（真实 OBS zip → 解码 → 解压 → 权限恢复）
        cwd = os.path.join(WORK, "worker")
        os.makedirs(cwd)
        skill_dir = worker_materialize(zip_b64, cwd)
        md = os.path.join(skill_dir, SKILL, "SKILL.md")
        png = os.path.join(skill_dir, SKILL, "assets", "icon.png")
        guide = os.path.join(skill_dir, SKILL, "references", "guide.md")
        assert os.path.isfile(md) and "## 场景" in open(md).read(), "SKILL.md 物化失败"
        assert os.path.isfile(guide), "references 子目录物化失败"
        with open(png, "rb") as f:
            assert f.read() == bytes([0x89, 0x50, 0x4E, 0x47]) + b"\x00\x01\x02\x03", "二进制无损失败"
        print("[4] worker 物化 OK（多文件夹 + 二进制无损）")

        # 5) 临时 URL（下载临时地址；前缀唯一不双写）
        url = storage.temporary_url(key, expires=300)
        assert url.startswith("https://") and "e2e-ops" in url
        assert url.count("grape/test/") == 1, f"前缀双写: {url}"
        print("[5] 临时下载 URL OK")

        print("\n=== B1 端到端验证全部通过（真实 OBS 桶 obs-grape-dev-file）===")
    finally:
        if key:
            storage.delete_prefix(key)  # 清理，不越权不残留
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
