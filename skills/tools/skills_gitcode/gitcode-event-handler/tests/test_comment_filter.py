"""无关评论过滤大小写修复测试(2026-08-11)。

背景: issue #101 评论 "[@skills_bot] 帮我处理一下这些BUG" 被误判无关过滤——
功能词 "bug" 小写匹配大写 "BUG" 失败。修复: ASCII 功能词大小写不敏感 + 词边界。
"""
import importlib.util

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== 大小写: BUG 大写应命中功能词 bug =====
def test_uppercase_bug_not_filtered():
    mod = _load_handler()
    note = "[@skills_bot](https://gitcode.com/skills_bot) 帮我处理一下这些BUG"
    title = "[huawei-cloud-computing-query]基本功评测发现3条问题"
    assert mod.is_irrelevant_comment(note, title) is False, \
        "大写 BUG 应命中功能词 bug, 不判无关"


# ===== 各种大小写形态都放行 =====
def test_case_variants():
    mod = _load_handler()
    for note in ["BUG", "Bug", "bug", "有BUG要修", "这个BUG很严重"]:
        assert mod.is_irrelevant_comment(note, "测试标题") is False, \
            f"大小写变体应放行: {note}"


# ===== 纯灌水仍过滤(防回归) =====
def test_garbage_still_filtered():
    mod = _load_handler()
    for note in ["好的", "收到", "👍", "+1", "谢谢", "666", "顶"]:
        assert mod.is_irrelevant_comment(note, "标题") is True, \
            f"灌水应仍过滤: {note}"


# ===== 词边界: 前缀包含不误伤(#175 教训) =====
def test_word_boundary():
    mod = _load_handler()
    # "bug" 在 "debugger" 里不应命中(词边界); 但中文"修bug"应命中
    assert mod.is_irrelevant_comment("help debugger please", "title") is True
    assert mod.is_irrelevant_comment("帮我修bug", "title") is False
