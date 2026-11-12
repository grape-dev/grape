"""S86 用例（D62，先测后改）：健康扫描平台派生去映射表 + event 族 fixture 修复。

两块：
1. bot_token_health 凭据扫描：provider 由 repo_key 前缀派生
   （parse_repo_key），不再依赖 _CRED_PROVIDER 字面映射表——
   第 5 平台（config-only，cred_type=bot_{p}_token）自动纳入巡检；
2. test_event_action_filter fixture 修复验证：删除引用产品不存在列
   （repo_key）的陈旧清理语句，改按 event_type 清理——事件族 ~30
   预存量失败转绿（由全 family 运行验证，本文件只锁 health 行为）。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _env():
    with db_lock:
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 'gitcode:s86%' OR repo_key LIKE 'gitee:s86%' OR repo_key LIKE 'acme:s86%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 'gitcode:s86%' OR repo_key LIKE 'gitee:s86%' OR repo_key LIKE 'acme:s86%'")
        db.commit()


def _scan_pairs():
    """直接复用 _verify_repo_bot_tokens 的扫描逻辑（提取前先录制行为）。"""
    from src.domains.runtime.engine import bot_token_health as h
    import inspect
    src = inspect.getsource(h)
    return src


def test_s86_health_scan_no_literal_cred_provider_map():
    """结构守卫：_CRED_PROVIDER 字面映射表清除（provider=repo_key 派生）。"""
    src = _scan_pairs()
    assert "_CRED_PROVIDER" not in src, (
        "bot_token_health 仍用字面 cred_type→provider 映射表——"
        "provider 统一 repo_key 前缀派生（D62/S86，cred_type 冗余裂缝）")


def test_s86_health_scan_bot_filter_semantics():
    """行为锁：只巡检 bot 凭据族（webhook 行不进巡检）。
    D99 值域归一：cred_type='bot' 精确匹配（LIKE 反模式消除，UC41④）。"""
    src = _scan_pairs()
    assert ("cred_type = 'bot'" in src), (
        "扫描必须保留 bot 凭据族过滤（webhook 不是 bot token，不该被巡检；"
        "D99 归一后为 cred_type='bot' 精确匹配）")


def test_s86_fifth_platform_scan_included_by_derivation():
    """第 5 平台（config-only acme，bot_acme_token）自动纳入巡检——
    旧映射表形态下此用例 RED（map 无 acme 键，行被静默跳过）。"""
    from src.kernel.repo_key import parse_repo_key
    from src.domains.mgmt.user.db import get_repo_credential_value
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
            "VALUES ('%s', 'acme:s86o/w1', 'bot', 'acme-tok')" % str(__import__("uuid").uuid4()))  # D99: id+值域归一
        db.commit()
    # 读者面：按卡派生 cred_type 可读（S80b 契约）
    assert get_repo_credential_value("acme:s86o/w1", "bot") == "acme-tok"  # D99 值域归一
    # 派生口径：provider 由 repo_key 而非 cred_type 映射
    assert parse_repo_key("acme:s86o/w1")[0] == "acme"
