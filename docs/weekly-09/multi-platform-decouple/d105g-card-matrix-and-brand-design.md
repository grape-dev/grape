# 台阶1+2 实施设计：平台卡验收矩阵 CI 常驻化 + 品牌资产进卡（2026-11-09）

> 前置：d105e（配卡即接入七断言）。本片把七断言中可静态验证的部分升级为 **CI 常驻矩阵**，并把品牌资产（图标兜底色/平台名）纳入卡 schema——新平台配卡后前端品牌显示自动生效，零 JS 改动。

## 一、台阶1：验收矩阵 CI 常驻化（tests/test_platform_card_matrix.py）

参数化遍历 `configs/platforms/` 全部卡（经 `list_vcs_platforms()`，新增卡自动进矩阵），每卡跑适用断言：

| 断言 | 说明 |
|---|---|
| ①卡构建 | `build_platform_card` 成功、`list_vcs_platforms` 含该平台 |
| ②webhook | `_generic_backend_for` 非 None；`list_webhook_providers` 含 (provider, webhook_path) |
| ④工厂 | `create_provider(p)` 实例且 `provider_name==p`，**不落 GitCodeProvider 兜底** |
| ⑥凭据 | credential_registry 可解析 `<p>_pat` |
| ③资源API | 有模块平台正向断言；无模块平台断言 ValueError（门槛显性化，台阶3 后配 resources 段即翻正） |
| brand_color | 目录接口响应透出（台阶2 联动） |

任何一张卡任何格红 → 该卡用例红（CI 常驻保真：卡是活配置，不是接入时验一次的静态文件）。

## 二、台阶2：品牌资产进卡

- `PlatformCard.brand_color`（可选，缺省 `''`；Builder 校验 `#RRGGBB`，非法抛 PlatformCardError）
- 目录接口 `/settings/vcs/platforms` 透出 `brand_color`
- 四卡数据：gitcode=#DA203E、gitee=#C71D23、github=#0969DA、gitlab=#FC6D26
- 前端：`_provBrandColor()` 目录主路径；`PLATFORM_BRAND_COLOR`/`_brandOverride` 静态表降为竞态兜底（同值对齐，消三处不一致）；`platformIcon` 目录优先、SVG 带 `data-p`、异步就位原地刷 fill（projectNewBtn 原地替换模式）
- 已知视觉变更：github 登录按钮 #f0f6fc（近白）→ #0969DA（官方蓝），为收敛的预期行为

## 三、守卫口径

矩阵对"配卡即接入"的边界是显性的：无模块平台的资源 API 断言**预期 ValueError**（台阶3 后卡带 resources 段翻正）——门槛不消失，只被钉在测试里看得见。

## 四、场景用例

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| UC68 | 全卡验收矩阵 | 每卡六断言全绿；新卡自动进矩阵 | test_platform_card_matrix |
| UC69 | 品牌资产目录化 | 目录接口透出 brand_color；前端兜底表与卡同值 | test_platform_card_matrix + 目录接口断言 |
