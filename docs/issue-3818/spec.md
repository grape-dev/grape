# Spec（引用 Issue 已发布版本）

本单 Spec 由 Issue Agent 于 2026-11-23 发布在 Issue #3818 评论（`[issue agent] 📋 Spec Document`），
代码实现以该文档为准，此处不复制全文、只固化落地口径：

- **In Scope**：入库 + 派发双点载荷审查；`prompt built` 打点元信息化 + 脱敏兜底；同 payload hash
  重复投递熔断 + dead-letter。
- **Out of Scope**：任务 id=79 删除 / AK 吊销 / 存量预签名 URL 失效 / 已落 Loki 日志清理（运维处置）；
  #3737、#3741/#3800 各自独立。
- **验收**：恶意载荷入库或派发被拒且可追溯；同载荷达阈值停派并告警；日志不再出现预签名串/口令；
  fake completion 降级 failed 不放大重试；现有测试全绿。
