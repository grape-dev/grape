"""三库四进程边界守卫：D109 平台对象模型（四表归 mgmt 域）。

背景（2026-11-16 架构复核，docs/lib-3db-3host-design.md §1.1）：
- 每个进程只直连本域库，跨域读走 `*_lib` / public 具名查询，跨域写走目标域 public 原语；
- 历史事故：未知表被四个进程各建一份同名基表（设计 §2.1 的「多份基表」）。

本锁四条：
  1. `configs/table_ownership.json` 必须把四张平台对象表登记为 mgmt 域，并给 runtime 留视图；
  2. D109 建表迁移必须过 `_owned_skip`（含 grape_t_repo_credentials 的补列只在 owner 进程做）；
  3. runtime / insight 域源码不得 import 平台对象 ORM 层（否则拆库后读自己库的空副本）；
  4. 跨域访问必须落在 mgmt public 原语上（`repo_platform_cards` / `mark_repo_platform_bot_status`，
     具名查询须在 `_ANY_QUERIES` 登记）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
PLATFORM_TABLES = (
    "grape_t_oauth_channels",
    "grape_t_repo_platforms",
    "grape_t_channel_repo_link",
    "grape_t_user_repo_platform_link",
)


def test_platform_tables_have_single_owner_in_manifest():
    manifest = json.loads((ROOT / "configs" / "table_ownership.json").read_text(encoding="utf-8"))
    tables = manifest.get("tables") or {}
    views = manifest.get("views") or {}
    missing = [t for t in PLATFORM_TABLES if t not in tables]
    assert not missing, f"平台对象表未登记域归属（拆库后会变成四进程各建一份）: {missing}"
    wrong = {t: tables[t] for t in PLATFORM_TABLES if tables[t] != "mgmt"}
    assert not wrong, f"平台对象表归属应为 mgmt: {wrong}"
    assert "runtime" in (views.get("grape_t_repo_platforms") or []), (
        "grape_t_repo_platforms 缺 runtime 视图映射（同实例+视图回滚形态下 runtime 读不到平台卡）")


def test_d109_ddl_goes_through_owner_guard():
    src = (SRC / "api" / "db_setup.py").read_text(encoding="utf-8")
    i = src.index("def _fn_create_platform_object_tables")
    seg = src[i:i + 3000]
    assert "_owned_skip(_tbl" in seg, "D109 建表未过表归属守卫（非 owner 进程会各建一份）"
    assert '_owned_skip("grape_t_repo_credentials"' in seg, (
        "grape_t_repo_credentials 归 insight 域，补列不得由非 owner 进程执行")


def test_runtime_and_insight_do_not_import_platform_orm():
    allowed = {SRC / "kernel", SRC / "api", SRC / "domains" / "platform", SRC / "domains" / "mgmt"}
    hits = []
    for dom in ("runtime", "insight"):
        for p in (SRC / "domains" / dom).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            for pat in ("repositories_platform_object", "models_platform_object"):
                if pat in text:
                    hits.append(f"{p.relative_to(ROOT)}: {pat}")
    assert not hits, (
        "runtime/insight 域直接引用平台对象 ORM（拆库后读写的是本域副本，应走 mgmt public）:\n"
        + "\n".join(hits))


def test_runtime_platform_access_goes_through_mgmt_public():
    card = (SRC / "domains" / "runtime" / "vcs" / "platform_card.py").read_text(encoding="utf-8")
    assert "mgmt" in card and "repo_platform_cards" in card, (
        "runtime 平台卡读取未走 mgmt public 原语")
    health = (SRC / "domains" / "runtime" / "engine" / "bot_token_health.py").read_text(encoding="utf-8")
    assert "mark_repo_platform_bot_status" in health, (
        "runtime bot 巡检状态未走 mgmt public 写原语")

    pub = (SRC / "domains" / "mgmt" / "public" / "__init__.py").read_text(encoding="utf-8")
    for name in ("def repo_platform_cards", "def mark_repo_platform_bot_status"):
        assert name in pub, f"mgmt public 缺原语: {name}"
    for q in ("repo_platform_cards", "repo_platform_cards_all", "repo_platform_bot_status_set"):
        assert f'"{q}"' in pub, f"具名查询未登记: {q}"
    # 凭据列不得跨域出口
    m = re.search(r"_PLATFORM_CARD_COLUMNS = \((.*?)\)", pub, re.S)
    assert m, "mgmt public 缺 _PLATFORM_CARD_COLUMNS 白名单"
    card_cols = m.group(1)
    assert "bot_token_enc" not in card_cols and "client_secret_enc" not in card_cols, (
        "平台卡跨域读白名单里带了凭据列")