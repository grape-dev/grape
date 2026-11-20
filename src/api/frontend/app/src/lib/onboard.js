// 飞书扫码接入（/feishu/onboard/{start,status,cancel}）的共用参数。
//
// 2026-11-18 架构审查 B2 / A8：两个页面（ChatView、ImChannel）各写了一份扫码流程，
// 轮询间隔 2000ms 散落在两处裸数字里。这里统一成一个具名常量，两处共用：
//   - 契约侧：status 是对 grape_t_feishu_onboard_task 的单行读，无外部调用，成本极低；
//     2s ⇒ 单个进行中任务 ≤30 次/分钟，服务端压力可忽略；
//   - 体验侧：扫码成功到前端切「已连接」的额外延迟上限 = 一个轮询周期（2s），
//     与既有 ChatView 实现完全一致（本次不改行为，只消除重复的裸数字）。
// 若后续要按环境调节，改这一个常量即可，两个入口同步生效。
export const ONBOARD_POLL_MS = 2000
