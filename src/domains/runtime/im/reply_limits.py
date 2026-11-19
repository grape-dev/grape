"""IM 回复正文长度口径（单一来源）。

历史：chat / VOD 两条回复链路各自写死 `[:2000]`，用户侧表现为「IM 回复到 2000 字就断」。
2026-11-19：放宽到 10000 字符（本文件是唯一口径，改这里即全链路生效）。

各渠道硬上限不在这里管——适配器自己分块：
  - 飞书卡片：请求体 30KB（超了报 230025）→ feishu_async 按 6000 字符切多张卡片
  - 企微 markdown 20480B / 钉钉 markdown 15000B / 微信 TEXT_LIMIT /
    Slack 3000 per section / Telegram 4096 → 各 sender 已有 split_* 分块
"""

MAX_REPLY_CHARS = 10000

__all__ = ["MAX_REPLY_CHARS"]