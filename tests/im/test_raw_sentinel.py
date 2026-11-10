"""依赖铁律哨兵测试（Issue #742 §2.1 / §7.7）——静态扫描守卫。

铁律：
  1. im_channel 不 import 平台 SDK / im_engine / 适配器；
  2. im_engine 不 import 适配器；
  3. 适配器不 import im_engine 内部；
  4. 核心不读写 InboundMessage.raw（引擎内部状态走引擎侧 context，无白名单豁免）。
"""
import os
import re

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))


def _py_files(rel: str):
    base = os.path.join(ROOT, rel)
    for dirpath, _, files in os.walk(base):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


FORBIDDEN_IN_CHANNEL = re.compile(
    r"^\s*(import|from)\s+(lark|dingtalk|slack_sdk|telegram|requests|aiohttp"
    r"|src\.im\.engine|src\.im\.adapters|\.\.engine|\.\.adapters|src\.im\.feishu_async)\b",
    re.M,
)
FORBIDDEN_IN_ENGINE = re.compile(
    r"^\s*(import|from)\s+(src\.im\.adapters|\.\.adapters|src\.im\.feishu_async"
    r"|src\.im\.handler_async|src\.im\.vod_handler_async|lark|dingtalk|slack_sdk)\b",
    re.M,
)
FORBIDDEN_IN_ADAPTERS = re.compile(
    r"^\s*(import|from)\s+(src\.im\.engine|\.\.engine)\b", re.M,
)
RAW_READ = re.compile(r"\b(?:msg|message|inbound|envelope)\.raw\b")


def test_im_channel_no_platform_or_engine_imports():
    for path in _py_files("im/channel"):
        src = open(path).read()
        m = FORBIDDEN_IN_CHANNEL.search(src)
        assert m is None, f"{path} violates channel dependency rule: {m and m.group(0)}"


def test_im_engine_no_adapter_imports():
    for path in _py_files("im/engine"):
        src = open(path).read()
        m = FORBIDDEN_IN_ENGINE.search(src)
        assert m is None, f"{path} violates engine dependency rule: {m and m.group(0)}"


def test_adapters_never_import_engine_internals():
    for path in _py_files("im/adapters"):
        src = open(path).read()
        m = FORBIDDEN_IN_ADAPTERS.search(src)
        assert m is None, f"{path} violates adapter dependency rule: {m and m.group(0)}"


def test_engine_never_reads_platform_raw_of_inbound():
    """核心禁读 InboundMessage.raw：engine 层（含 router）不得访问 <var>.raw。

    引擎内部路由状态一律走引擎侧 context（Router.handle 的 state dict），
    不再写入/读回 raw 字典——无白名单豁免。
    """
    for path in _py_files("im/engine"):
        src = open(path).read()
        m = RAW_READ.search(src)
        assert m is None, f"{path} reads InboundMessage.raw: {m and m.group(0)}"
