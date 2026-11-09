# Issue #470 — 任务执行记录

## 状态

进行中（TDD 红/绿切片逐步回填）

## 提交

| commit | 类型 | 说明 |
|--------|------|------|
| f3daaf74c | test | T1 红：运行配置统一 getter 失败测试——默认值/三源优先级/非法回退/派生阈值 |
| 9d5375b23 | feat | T2 绿：src/config.py getter（env>meta>文件>默认，60s TTL 缓存）+ example 配置 + gitignore |
| 56294f7a8 | test | T3 红：worker 超时链路失败测试——ACP env 优先/看门狗 env 优先/服务端注入 |
| d060f8ee0 | feat | T4 绿：worker 超时链路任务级 env 优先——看门狗/ACP 全局超时/服务端 enqueue 注入 |
| 5bf9cb837 | test | T5 红：派生阈值 getter 化失败测试——stuck/orphan 跟随统一超时 |
| 4966ad8a6 | feat | T6 绿：派生阈值 getter 化——stuck 22min/孤儿 6h 随统一超时派生，SWEEPER env 兜底保留 |
| b6df1723b | test | T7 红：DevStage 收敛+管理台运行参数失败测试 |
| 8a01d9b9 | feat | T8 绿：DevStage 模板/规格收敛单一来源 + 管理台运行参数读写（后端+前端+locales） |

## 备注

- 预存失败：tests/test_devstage_filter.py 6 用例基线即红（async→def 历史变更 vs asyncio.run 调用方式），与本改动无关，见 plan.md §4。
