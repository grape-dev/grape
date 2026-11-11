"""微信卡片降级测试（Issue #1728 M3）——「标题 + 正文 + 编号」。"""
from src.im.channel.card import (
    Action,
    Card,
    Tone,
    divider_block,
    fields_block,
    row_block,
    select_block,
    simple_card,
    text_block,
)
from src.im.channel.renderers.wechat import WechatCardRenderer

RENDERER = WechatCardRenderer()


def test_repo_card_renders_title_body_and_number_hint():
    items = ["owner/a（owner）", "owner/b（member）"]
    body = "**📁 已托管仓库列表**\n1. owner/a（owner）\n2. owner/b（member）\n回复数字选择 · `0` 取消"
    blocks = [text_block(body)]
    blocks.append(row_block(
        Action(id="bind", label="1", params={"num": 1}),
        Action(id="bind", label="2", params={"num": 2}),
    ))
    blocks.append(row_block(Action(id="help", label="❓ 帮助")))
    card = Card(title="📁 选择仓库", tone=Tone.DEFAULT, blocks=tuple(blocks))

    payload = RENDERER.render(card).payload
    text = payload["text"]
    assert text.startswith("【📁 选择仓库】")
    assert "1. owner/a（owner）" in text
    assert "2. owner/b（member）" in text
    assert text.count("回复数字选择") >= 1
    assert "`/help`" in text


def test_select_options_numbered():
    card = Card(
        title="任务产出",
        blocks=(text_block("待派发问题"), divider_block(),
                select_block("选择要派发的问题",
                             [Action(id="finding-select", label="finding one"),
                              Action(id="finding-select", label="finding two")])),
    )
    text = RENDERER.render(card).payload["text"]
    assert "1. finding one" in text
    assert "2. finding two" in text


def test_url_action_rendered_as_link():
    card = Card(title="打开", blocks=(text_block("详情"),),
                actions=(Action(id="open", label="PR",
                                url="https://gitcode.com/x/y/pull/1"),))
    text = RENDERER.render(card).payload["text"]
    assert "PR：https://gitcode.com/x/y/pull/1" in text


def test_fields_and_divider():
    card = Card(title="信息", blocks=(
        fields_block(("数量", "2"), ("范围", "user")), divider_block()))
    text = RENDERER.render(card).payload["text"]
    assert "数量：2" in text
    assert "范围：user" in text
    assert "──────" in text


def test_page_and_hint_footer():
    card = Card(title="列表", page=(1, 2), hint="回复数字选择 · 0 取消")
    text = RENDERER.render(card).payload["text"]
    assert "第 1/2 页" in text
    assert "回复数字选择 · 0 取消" in text


def test_empty_card_falls_back_and_flags_degraded():
    card = Card()
    rendered = RENDERER.render(card)
    assert rendered.payload["text"] == ""
    assert "card_text_fallback" in rendered.degraded


def test_numeric_selection_not_duplicated():
    card = Card(title="选择", blocks=(
        text_block("1. a\n2. b"),
        row_block(Action(id="bind", label="1", params={"num": 1}),
                  Action(id="bind", label="2", params={"num": 2})),
    ))
    text = RENDERER.render(card).payload["text"]
    assert text.count("1.") == 1
    assert text.count("2.") == 1
    assert text.count("回复数字选择") == 1