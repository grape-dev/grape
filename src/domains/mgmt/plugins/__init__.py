"""插件能力挂点（Issue #1089 [插件市场-C1] 五类挂点接入）。

模块划分：
- resolver：插件配置继承链 seam（A4 #1082 契约：个人→仓库→Agent→节点）。
  A4 合入前按其口径实现纯函数；A4 落地后替换内部实现、签名不变。
- registry：plugin.json 清单解析与插件注册/卸载（bundled_skills 联动，Slice F）。

各挂点（①scene.json node.plugins ②SKILL.md plugins: 软引用
③agent-description.json agent.plugins ④Runtime installed_plugins
⑤plugin.json bundled_skills）统一经 resolver 合并生效。
"""
