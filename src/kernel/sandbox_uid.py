"""沙箱链稳定标识工具（Provider uid，2026-11-04 用户裁定）。

语义对齐 #646 credential_uid：UUIDv4，创建时一次性生成、终身不变；
当前无消费者（引擎 uid 派生种子由 credential_uid 1:1 承担 Provider 身份），
先落列预留分布式/库恢复场景的身份锚：
- 自增 provider id 只在单库内唯一——多主控独立库聚合会撞号（credential_uid 用 UUIDv4 的先例）；
- 部分恢复/旧快照搬库后 AUTOINCREMENT 序列断裂，重建行拿新 id；
- 表重建（delete-重插迁移）id 全量换值。

本模块为纯函数层：不 import DB，供 db_setup 迁移、settings/runtime 写入链共用。
（i5 线无 #646 engine_uid.py——本文件是沙箱分支首个 uid 工具模块。）
"""
from __future__ import annotations

import uuid

_UID_RE_HEX = "0-9a-f"


def new_provider_uid() -> str:
    """Provider 稳定键：UUIDv4（随机），创建/回填时一次性生成，终身不变。"""
    return str(uuid.uuid4())


def is_uid_str(value) -> bool:
    """是否为 36 位小写连字符 UUID 规范形（宽松接受大写，判定即可）。"""
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    if len(v) != 36:
        return False
    try:
        return str(uuid.UUID(v)) == v
    except (ValueError, AttributeError, TypeError):
        return False
def new_row_uid() -> str:
    """技术主键 uid：13 张沙箱表（grape_t_*）的 TEXT 主键取值器，UUIDv4 小写连字符。

    依据（arch-guard A12/G-8 裁定，2026-11-17 用户裁定弃雪花改 UUIDv4 文本主键，
    对齐 D105 UuidPkMigration 三批 TEXT PK 先例与 credential_uid/provider_uid 形态）：
    自增 id 仅单库内唯一——多主控独立库聚合撞号、部分恢复/旧快照搬库后
    AUTOINCREMENT 序列断裂、表重建 delete-重插 id 全量换值；UUIDv4 一次性生成
    终身不变，三场景零漂移。跨域/跨库业务身份仍由 engine_uid/provider_uid 等
    uid 列承担，本 id 仅为库内行技术键。
    """
    return str(uuid.uuid4())
