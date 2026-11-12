"""S110 用例（G1 搁置期边界守卫）：um 通道列读写边界锁定。

背景：G1（um 存储切换）经评审拍板搁置——外部正在优化 IM 逻辑，此时改造
um 必然冲突（channel_directory/session_manager 是 IM 链路中枢）；且迁移
清单以"6 处调用方收敛"为快照，IM 重构期间该快照不稳定。

守卫意图：G1 搁置期间，um 通道列（feishu_open_id/feishu_name/app_id/
gitcode_nickname）只允许被 channel_directory 与 session_manager（表定义/
启动回填桥）触碰。任何绕过 directory 的直读写（新调用点/复辟裸 SQL）
即 CI 红——保护 S99 的收敛边界在 IM 重构期间不被侵蚀，同时为未来按
um-storage-switch-plan v2 执行切换保留完整前提。

白名单（只减不增）：
- src/domains/runtime/im/channel_directory.py（唯一权威访问层，S99）
- src/session_manager.py（表 DDL + 启动回填桥，S92 守卫）
- src/domains/runtime/routes/chat.py 的 purge 仅经 directory（白名单按文件），
  prompt_builder 的 agent 引导模板字符串为文案非代码路径
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"

_CHANNEL_COLS = ("feishu_open_id", "feishu_name")

# 允许出现 um 通道列字面量的文件（只减不增）
_WHITELIST = {
    "domains/runtime/im/channel_directory.py",
    "kernel/session_manager.py",
    "kernel/prompt_builder.py",   # agent 引导模板字符串（非运行时代码路径，S99 注记）
}


def test_s110_um_channel_columns_boundary():
    """结构守卫（G1 搁置期）：um 表的 SQL 触点只允许存在于白名单文件。

    口径：文件出现 um 表的真实 SQL 动词（FROM/UPDATE/INSERT/DELETE/JOIN +
    unified_user_mapping）才算触点——字面量提及（注释/docstring/字段名）不算。
    新增 um SQL 触点必须改走 channel_directory。
    """
    import re as _re
    _SQL_UM = _re.compile(
        r"(FROM|UPDATE|INSERT\s+INTO|DELETE\s+FROM|JOIN)\s+unified_user_mapping", re.I)
    SNAPSHOT = {
        "domains/mgmt/admin/cooldown.py", "api/issues_center.py",
        "domains/mgmt/routes/agents.py", "domains/mgmt/routes/grape_auth.py",
        "domains/runtime/routes/pipeline.py", "domains/mgmt/routes/settings.py",
        "api/session_store.py", "api/db_setup.py",
        "domains/insight/observability/alerts.py", "domains/runtime/engine/engine_select.py",
        "domains/runtime/im/engine_wiring.py", "domains/runtime/routes/chat.py",
        "kernel/session_manager.py", "kernel/prompt_builder.py",
    }
    new_sites = []
    for f in (_SRC).rglob("*.py"):
        rel = str(f.relative_to(_SRC))
        if rel in _WHITELIST:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if _SQL_UM.search(text) and rel not in SNAPSHOT:
            new_sites.append(rel)
    assert not new_sites, (
        "um 表出现快照外的新 SQL 触点（G1 搁置期：新触点必须走 channel_directory，"
        f"或经重估后登记进快照）：{new_sites}"
    )


def test_s110_channel_directory_is_single_writer():
    """单写者守卫：channel_directory 为 um 通道数据的权威访问层（登记版）。

    已知遗留直连（G1 搁置期登记，IM 重构合入后随重估收口，不阻断）：
    - domains/runtime/im/handler_async.py: bind_feishu_identity（p2p 绑定路径，session_manager 入口）
    - session_manager.update_unified_user_info（OAuth 登录整行，G1 v2 步骤 3 同批改造项）
    快照=当前全部已知引用（实测 2026-11-05）；本用例锁定"不新增"——新增即失败。
    """
    cd = (_SRC / "domains" / "runtime" / "im" / "channel_directory.py").read_text(encoding="utf-8")
    assert "unified_user_mapping" in cd, "channel_directory 必须是 um 通道数据的权威访问层"
    known = {
        "domains/runtime/routes/chat.py": {"bind_feishu_identity"},
        "domains/mgmt/routes/feishu_onboard.py": {"bind_feishu_identity"},
        "domains/runtime/im/feishu_async.py": {"bind_feishu_identity", "update_unified_user_info"},
        "domains/runtime/im/handler_async.py": {"bind_feishu_identity"},
        "kernel/session_manager.py": {"bind_feishu_identity", "update_unified_user_info"},
    }
    legacy_writers = ("bind_feishu_identity", "update_unified_user_info")
    import re as _re2
    found = {}
    for f in (_SRC).rglob("*.py"):
        rel = str(f.relative_to(_SRC))
        if rel in _WHITELIST or rel == "domains/runtime/im/channel_directory.py":
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for w in legacy_writers:
            if (f"import {w}" in text or f", {w}" in text or f"{w}," in text
                    or f"{w}(" in text):
                found.setdefault(rel, set()).add(w)
    for rel, ws in found.items():
        for w in ws:
            assert rel in known and w in known.get(rel, set()), (
                f"新的遗留 um 通道写入引用（边界侵蚀，需登记进快照或改走 "
                f"channel_directory）：{rel} -> {w}")
