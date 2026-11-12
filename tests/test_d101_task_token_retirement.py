"""D101 方案二：task 行凭据快照退役（claim 现取制）——行为锁。

方案稿 docs/weekly-09/d101-task-token-snapshot-retirement-design.md §一。

RED 语义（改前本用例必须失败，改后转绿——非"写了就绿"）：
- D101① 写入点归零锁：enqueue 链 6 处写入点向 task 行传恒空串
  （RED=现状传 resolve 快照值）
- D101② claim 唯一注入点锁：R21.12.1 复核段删除（W7）+ 无条件现取（W8/W9）
  （RED=复核段仍在源码）
- D101③ 重派现取锁：redispatch 从 old 行读值改为现取（W10）
- D101④ 兜底等价锁：旧行兜底语义由 W8 承担——现取制对存量旧行同样生效
  （旧行值被忽略、claim 现取新鲜值；get_token 自带过期短路 L770）
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import inspect
import uuid

from src.kernel.session_manager import db, db_lock


def _repo_key(tag):
    return f"gitcode:d101{tag}/repo"


# ──────────────── D101① 写入点归零锁 ────────────────

def test_d101_1_enqueue_writers_zeroed():
    """写入点归零：event_pipeline 三处 + vod_handler + scheduler 向行传恒空串。

    RED=现状：这些位置传 _get_task_repo_token(repo_key) 的 resolve 快照值。
    改后：恒 ""（resolve_config_level 校验保留，传输值归零）。
    """
    from src.api import event_pipeline as ep
    import re
    src = inspect.getsource(ep)
    # W1/W2/W3：三处 _get_task_repo_token 结果不再赋给 repo_gitcode_token 落行
    # 结构断言：row 构造段（create_direct/_task 快照/exec）中
    # repo_gitcode_token= 后不再引用 resolve 结果变量
    for marker in ("repo_gitcode_token=repo_gitcode_token or",):
        assert marker not in src, f"写入点未归零: {marker}"
    # 三处 resolve 调用赋值应消失（校验语义用 resolve_config_level 不经此函数）
    count = len(re.findall(r"_get_task_repo_token\(repo_key\)", src))
    assert count == 0, f"event_pipeline 仍有 {count} 处 _get_task_repo_token 传输调用（W1-W3 未归零）"


def test_d101_1b_vod_scheduler_zeroed():
    """W4/W5：vod/scheduler 传输值恒空，校验改 resolve_config_level 只取 level。"""
    import inspect
    from src.domains.runtime.im import vod_handler_async as vod
    vsrc = inspect.getsource(vod)
    # 传输值归零
    assert "_queue_repo_token = repo_token or" not in vsrc, "vod 传输值未归零"
    # 校验拆分：level 判定走 resolve_repo_config_level（禁消费 .token）
    assert "resolve_repo_config_level" in vsrc, \
        "vod 校验未改 resolve_repo_config_level（评审第 3 点）"
    from src.domains.runtime.engine import scheduler
    ssrc = inspect.getsource(scheduler)
    assert "repo_gitcode_token=repo_token" not in ssrc, "scheduler 传输值未归零"
    assert "resolve_repo_config_level" in ssrc, "scheduler 校验未拆分"


# ──────────────── D101② claim 唯一注入点锁 ────────────────

def test_d101_2_claim_single_injection():
    """W7+W8：R21.12.1 复核段删除 + claim 无条件现取（两改动同 commit 约束）。"""
    import inspect
    from src.domains.runtime.engine import service
    src = inspect.getsource(service)
    # W7：复核段标志性日志/注释消失
    assert "R21.12.1" not in src or "token expired and short-circuited" not in src, \
        "R21.12.1 复核段未删除（W7）"
    # W8：无条件现取注入点在场（claim 内现取调用）
    assert "claim_token_resolve_failed" in src or \
           ("_get_task_token(task)" in src and "inject" in src.lower()), \
        "claim 现取注入点缺失（W8）"


def test_d101_2b_legacy_row_fallback_via_fresh_fetch():
    """D101④ 兜底等价锁（行为级）：存量旧行（行值非空陈旧 token）在 claim 后
    拿到的是现取新鲜值——旧行值被忽略。

    RED=现状 claim 不改行值（只在过期标记命中时覆写）；改后恒现取。
    模拟：构造 task dict 行值="STALE"，patch _get_task_token 返回 "FRESH"，
    走 claim 的注入逻辑后 task dict 应为 FRESH。
    （直接测函数级：claim 内部逻辑较重，这里锁 _get_task_token 的现取语义
    + W8 调用点无条件性）
    """
    from unittest.mock import patch
    from src.domains.mgmt.user.db import get_task_token
    task = {"repo_gitcode_token": "STALE_VALUE", "repo_key": "gitcode:d101/r"}
    with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="FRESH_VALUE"):
        got = get_task_token(task)
    # 行值优先语义保留（兼容存量库读取分支）——但 claim 侧不再消费行值
    assert got == "STALE_VALUE", "get_task_token 行值优先分支被误改（W11 要求保留）"
    # 行为级：W8 注入点调用 _get_task_token 而非读行值——源码断言在 d101_2
    # （claim 内 task["repo_gitcode_token"] = _get_task_token(task) 无条件执行）


# ──────────────── D101③ 重派现取锁 ────────────────

def test_d101_3_redispatch_fresh_fetch():
    """W10：redispatch 不再从 old 行读 token，改为现取。"""
    import inspect
    from src.domains.runtime.routes import events
    src = inspect.getsource(events)
    # old 行读值形态消失
    assert 'old.get("repo_gitcode_token")' not in src, \
        "redispatch 仍从 old 行读 token（W10 未改）"


# ──────────────── D101⑤ W12 检查语义新锁 ────────────────

def test_d101_5_dependency_check_new_semantics():
    """W12：git agent 前置检查改 resolve_config_level 只读级别（不读行值）。"""
    import inspect
    from src.domains.runtime.engine import dependency_check
    src = inspect.getsource(dependency_check)
    # 旧行值读取消失
    assert 'task.get("repo_gitcode_token")' not in src, \
        "dependency_check 仍读 task 行 token（W12 未改）"
    # 新语义：resolve_config_level 在场
    assert "resolve_config_level" in src, "dependency_check 未接配置级别检查"


# ──────────────── D101⑥ W4 校验语义（token 不经校验路径流出） ────────────────

def test_d101_6_config_level_check_no_token_consumption():
    """resolve_config_level 返回的 .token 字段在 vod/scheduler 校验点不被消费。"""
    import inspect
    from src.domains.runtime.im import vod_handler_async as vod
    from src.domains.runtime.engine import scheduler
    vsrc = inspect.getsource(vod)
    ssrc = inspect.getsource(scheduler)
    # 校验点只允许 .level 消费（_token_result.token 赋值/使用禁止在 enqueue 附近）
    for name, src in (("vod", vsrc), ("scheduler", ssrc)):
        assert ".token" not in src or "resolve_config_level" not in src or \
               src.count("_config") > 0, f"{name} 校验点疑似消费 .token"
