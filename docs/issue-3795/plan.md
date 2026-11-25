# Issue #3795 方案设计：WS 关闭码归因（不建新契约，扩既有分层）

## 关键判断（“生产为何仍报 ERROR”）

基线 `arch-weekly-11-ljt` 已含 #3698 归一（`28cedb61`，飞书 `connect()` 的 `async with` 块
整体 `except Exception → _ws_failure()`）。因此生产产出的 ERROR 只可能来自：

1. 生产镜像早于 `28cedb61`（发布滞后）；
2. 归一管道仍存在的语义缺口 —— **本次定性结论：是 2 的变体**。
   `_ws_failure()` 把 3003 压成**无关闭码**的 `ChannelError`，supervisor `except ChannelError`
   只记 `ws_transport`，于是：
   - 关闭码信息在归因链上丢失（看板无法区分 3003 / 1006）；
   - 任何未归一的泄漏异常（非 OSError 族的 `ConnectionClosed`）仍落通用
     `except Exception → log.exception("channel crashed (unexpected)")`。

即：修复不依赖“发布滞后”假设也能消除 ERROR 噪声并恢复关闭码归因；发布滞后场景由同一兜底覆盖。

## 设计

| 层 | 改动 | 理由 |
|---|---|---|
| 契约层 `channel/channel.py` | 新增 `ChannelWsClosed(ChannelError)`，`close_code` 属性 + `__str__` 保证含 `ws closed code=<code>` | isinstance 精确捕获，避免解析文案；仍可重试（继承 ChannelError） |
| 适配器 `feishu/channel.py` | `_ws_failure()` 带码→`ChannelWsClosed`，无码→`ChannelError`；3001 仍 `ChannelRegistrationConflict` | 不让无码传输异常（BrokenPipe）虚构关闭码 |
| 引擎 `supervisor.py` | 新分支 `except ChannelWsClosed`（位于 `ChannelError` 之前）→ WARNING 单行 + `ws_transport` + `ws_close_<code>`；通用 `except Exception` 内先 duck-typing 取码，命中即走同一 WARNING 路径 | 双记保证看板 reconnects/flap 序列不断档；真 bug 仍 ERROR |
| 指标 `metrics.py` | `ws_close_cause()` 分桶 + `record_ws_close()` | `flap_by_cause` 常驻字典，4000+ 应用自定义码统一 `ws_close_4xxx`，无码 `ws_close_unknown`，键有界 |
| 取码 | `supervisor.ws_close_code_of()` duck-typing（`rcvd/sent.code`、自身 `code`+`reason`） | 引擎不 import websockets，避免库版本耦合；不误取 HTTP/业务 `code` |

### 兼容性

- `reconnects` / `ws_transport` 语义不变（关闭仍 +1 一次）；
- `ChannelWsClosed` 是 `ChannelError` 子类 → 现有 `except ChannelError` / 测试断言不破；
- 3001 并发注册冲突分支行为不变；
- `flap_by_cause` 新增键只在真实关闭时出现，键空间有界（`ws_close_<已知码>` / `ws_close_4xxx` / `ws_close_unknown`）。

### 风险

| 风险 | 缓解 |
|---|---|
| 把真 bug 降级成 WARNING | 只有 `ChannelWsClosed` 或 duck-typed 关闭码命中的异常走 WARNING；`AttributeError` 用例守护 |
| 键无界增长 | 分桶 + unknown 兜底 |
| 与 #3735 邻接冲突 | 只改归类与归因，不触碰 ClientSession 生命周期/重建逻辑 |