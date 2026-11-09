# Plan: Issue #565 技术方案

## 方案概述
自循环检测两处路径对 bot actor 的 issue_opened 增加放行分支（打 `evt["_bot_opened"]`）；
放行后复用既有推荐链（`_is_scene_recommend=True` → 已有 scene_id 则重试 3 次 0.5s 跳过 →
派发 vod 推荐 → set_issue_scene）；前置仓级滑动窗口限流；执行棒在
`_redispatch_vod_after_recommend` 派发点用事件标记统一短路。

## 实现点
| 位置 | 改动 |
|---|---|
| `event_pipeline.py` 模块级 | `_BOT_OPEN_RECOMMEND_WINDOWS`（repo_key → 时间戳列表）+ `_bot_open_recommend_rate_limited()`（60s/5，超限不计数） |
| `event_pipeline.py` 2.0 主路径 | `_allow_through`：bot actor + issue_opened → 一律放行 + `evt["_bot_opened"]=True`（原「## 父任务」豁免保留为 elif 分支，叠加生效） |
| `event_pipeline.py` 2.0 fallback | 同构放行（actor in bot_usernames + issue_opened → 放行 + 标记） |
| `event_pipeline.py` 2.0c | 限流插在 issue_opened 台账检查后、路由判定前；子任务（「## 父任务」）不占配额；超限 `append_skipped_event(bot_open_recommend_rate_limited)` 并 return |
| `event_pipeline.py` `_redispatch_vod_after_recommend` | 函数入口 `evt["_bot_opened"]` 短路：不创建 a2a_event、不重派 VOD 执行棒 |
| `runtime/service.py` `_handle_vod_recommend_complete` | worker 侧 evt 为重建对象：按 issue 首事件 actor 是否 bot（`get_all_bot_usernames`）还原 `_bot_opened` 后调重派（查询失败降级不标记，人建单行为不变） |

## 关键设计决策
- 限流存储：进程内 dict（webhook 去重层挡重投，重启丢失可接受，不进 DB）。
- 放行范围：仅 issue_opened；评论族拦截是防死循环最后闸门，不动。
- 执行棒短路：事件标记（`evt["_bot_opened"]`）最小侵入，人建单链路零改动。
- 核实结论（Spec 开放问题）：推荐完成回调**确实派发执行棒**——direct 路径
  （event_pipeline `_execute_agent` done 处理）与 worker 路径
  （service `_handle_vod_recommend_complete`）均调 `_redispatch_vod_after_recommend`
  重派 VOD 执行流；已按标记在派发点短路，bot 建单只做场景判定。

## 测试
`tests/test_bot_opened_recommend.py`：T1 放行+推荐+scene_id 写入+执行棒短路（双路径）；
T2 已有场景跳过推荐；T3 第 6 条限流；T4 bot 评论仍拦截；T5 人建单行为不变。
