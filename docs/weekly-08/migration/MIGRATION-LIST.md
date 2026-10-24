# Vue 迁移清单（阶段2，每批更新）

| 批次 | 模块/页面 | commit | 验收 | 截图 |
|---|---|---|---|---|
| 1a | beta-apply.html → /static-vue/beta-apply | bea729687 | 新旧文本逐行比对一致；student分支交互/sessionStorage持久化/URL参数回填验证通过 | screenshots/beta-light.png, beta-dark.png |
| 1b | help.html（生成产物，改生成器 help_page_gen.py 的 CSS 为 Liquid Glass 明暗双主题） | (待填) | 玻璃卡片 blur(16px)/18px圆角/药丸按钮实测生效；暗色切换+localStorage 正常；测试与迁移前一致（6 个预存失败不变）。**偏差说明：未做整页 Vue 化——该页是启动时从 help/ markdown 自动生成的产物，整页 Vue 化需分叉生成器，超出"只改渲染层"单批范围，留待阶段3主人裁决** | screenshots/help-light.png, help-dark.png |
