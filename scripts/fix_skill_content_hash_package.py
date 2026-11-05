#!/usr/bin/env python3
"""Issue #1070：skill 表 content_hash_package 污染数据一次性修复 + OBS zip 缓存索引重建。

背景：skill 表 582 行 content_hash_package 被 DDL 文本（ALTER TABLE ... 片段）
污染、content_hash 为空，导致 grape skills detail / skills API 兜底取回 404。
本脚本幂等：仅处理「content_hash_package 匹配 DDL 文本特征（且 content_hash
为空）」的行，不触碰正常数据。

用法：
  python scripts/fix_skill_content_hash_package.py [--dry-run] [--db-backend /path/db-backend.json]

步骤：
  1. 扫描 skill 表污染行（content_hash_package 含 ALTER TABLE/DDL 特征，
     或 content_hash_package 非哈希格式且 content_hash 为空）；
  2. 清洗 content_hash_package：能经 OBS 对象键取回 zip 则重算 sha256 回写，
     否则置空串（修复 404 的关键是去掉污染文本，索引重建后哈希自然补齐）；
  3. 重建 OBS 磁盘 zip 缓存 index.json：hash 为空的条目按本地缓存 zip 字节
     重算补齐（保留旧 index 备份 index.json.bak-1070）。

校验口径与 src/storage/index.json 条目一致：{"hash": <sha256hex>, "sid": ...}。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DDL_MARKERS = re.compile(r"\b(ALTER\s+TABLE|ADD\s+COLUMN|MODIFY\s+COLUMN|CREATE\s+(TABLE|INDEX)|DROP\s+(TABLE|COLUMN))\b", re.I)
HEX64 = re.compile(r"^[0-9a-f]{64}$")

BACKUP_SUFFIX = ".bak-1070"


def _is_polluted(value: str, content_hash: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    if DDL_MARKERS.search(v):
        return True
    # 哈希位被污染成任意非哈希文本（content_hash 同时空，构成 #1070 现场）
    if not HEX64.match(v) and not (content_hash or "").strip():
        return True
    return False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Issue #1070 skill 表污染修复")
    parser.add_argument("--dry-run", action="store_true", help="只扫描报告，不写库")
    args = parser.parse_args()

    from src.session_manager import db, db_lock

    # ── 1. 扫描污染行 ──────────────────────────────────────
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, skill_name, content_hash_package, content_hash FROM skill")
        rows = c.fetchall()
    polluted = [(r[0], r[1], r[2] or "", r[3] or "")
                for r in rows if _is_polluted(r[2], r[3])]
    print(f"skill 表总行数: {len(rows)}，污染行: {len(polluted)}")
    for sid, name, v, _h in polluted[:20]:
        print(f"  polluted id={sid} skill={name} content_hash_package={v[:60]!r}")
    if len(polluted) > 20:
        print(f"  ... 共 {len(polluted)} 行")

    # ── 2. 清洗 + 重算哈希（能取到 OBS zip 则回写正确 sha256）──
    storage = None
    try:
        from src.storage import get_storage
        storage = get_storage()
    except Exception as e:
        print(f"storage 不可用（跳过哈希重算，仅清洗）: {e}")

    if polluted and not args.dry_run:
        with db_lock:
            c = db.cursor()
            for sid, name, v, _h in polluted:
                fixed_hash = ""
                if storage:
                    try:
                        from src.user.db import get_skill_object_key
                        key = get_skill_object_key(sid)
                        if key:
                            data = storage.read_bytes(key)
                            if data:
                                fixed_hash = _sha256_bytes(data)
                    except Exception as e:
                        print(f"  hash recompute failed id={sid}: {e}")
                if fixed_hash:
                    c.execute("UPDATE skill SET content_hash_package=?, content_hash=? WHERE id=?",
                              (fixed_hash, fixed_hash, sid))
                else:
                    c.execute("UPDATE skill SET content_hash_package='' WHERE id=?", (sid,))
            db.commit()
        print(f"已清洗 {len(polluted)} 行（含哈希重算回写）")

    # ── 3. 重建 OBS zip 缓存 index.json ───────────────────
    from src.storage import ZIP_CACHE_DIR, ZIP_CACHE_INDEX
    if not os.path.isfile(ZIP_CACHE_INDEX):
        print("zip 缓存 index.json 不存在，跳过索引重建")
        return
    with open(ZIP_CACHE_INDEX, "r", encoding="utf-8") as f:
        index = json.load(f)
    empty = {k: v for k, v in index.items()
             if isinstance(v, dict) and not (v.get("hash") or "").strip()}
    print(f"index.json 条目: {len(index)}，hash 为空: {len(empty)}")
    if not empty or args.dry_run:
        return
    backup = ZIP_CACHE_INDEX + BACKUP_SUFFIX
    if not os.path.isfile(backup):
        shutil.copy2(ZIP_CACHE_INDEX, backup)
        print(f"旧 index 已备份: {backup}")
    fixed = 0
    for key, entry in empty.items():
        local = os.path.join(ZIP_CACHE_DIR, f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}.zip")
        data = None
        if os.path.isfile(local):
            with open(local, "rb") as f:
                data = f.read()
        elif storage:
            data = storage.read_bytes(key)
        if data:
            entry["hash"] = _sha256_bytes(data)
            fixed += 1
    with open(ZIP_CACHE_INDEX, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    print(f"index.json 已重建：补齐 {fixed}/{len(empty)} 条 hash")

    # ── 4. 终检：污染行应为 0 ─────────────────────────────
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, content_hash_package, content_hash FROM skill")
        rest = [r for r in c.fetchall() if _is_polluted(r[1], r[2])]
    print(f"终检：剩余污染行 = {len(rest)}")
    sys.exit(0 if not rest else 2)


if __name__ == "__main__":
    main()
