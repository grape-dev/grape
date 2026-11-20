"""plugin-tester（QA 节点）— 插件自带评测基准与回归出分。

Issue #1093：将 QA/plugin-tester 节点验证过的免凭据自检用例
（L0 校验/方法名探测/401 分层/双引擎路由）沉淀为随插件包管理的评测基准，
芯更新与新版本发布前自动回归出分，分数纳入 QA 门禁并上统一插件卡片。

Issue #1796：L0 校验统一经 ``src.api.plugin_l0.validate_plugin_l0`` 单入口
（与市场上传 POST /api/plugins/upload 同源），报告输出 validator/degraded，
杜绝「QA 全绿、上架被拒」。

子模块：
- benchmark：基准清单解析/校验（随插件包分发）
- runner：基准执行与量化出分（0-100；L0 走统一入口）
- gate：QA 门禁阈值判定（不达标阻断发布/升级）
- card：统一插件卡片分数字段数据源
"""
