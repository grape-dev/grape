# Vue 迁移清单（阶段2，每批更新）

| 批次 | 模块/页面 | commit | 验收 | 截图 |
|---|---|---|---|---|
| 1a | beta-apply.html → /static-vue/beta-apply | bea729687 | 新旧文本逐行比对一致；student分支交互/sessionStorage持久化/URL参数回填验证通过 | screenshots/beta-light.png, beta-dark.png |
| 1b | help.html（生成产物，改生成器 help_page_gen.py 的 CSS 为 Liquid Glass 明暗双主题） | e84389ce9 | 玻璃卡片 blur(16px)/18px圆角/药丸按钮实测生效；暗色切换+localStorage 正常；测试与迁移前一致（6 个预存失败不变）。**偏差说明：未做整页 Vue 化——该页是启动时从 help/ markdown 自动生成的产物，整页 Vue 化需分叉生成器，超出"只改渲染层"单批范围，留待阶段3主人裁决** | screenshots/help-light.png, help-dark.png |
| 2a | my_feedback.js + my_feedback_detail.js → Vue视图（壳内chunk方案：/static/js/vue/*.js + 全局renderMyFeedback契约不变） | a885d0937 | 列表/空态/分页/删除确认/详情404/附件预览逻辑等价；登录态壳内实测渲染OK、console零错误；暗色(auto+prefers-dark)生效。**架构说明：这批发现模块页面是老壳SPA内部视图而非独立HTML，改为Vite多入口产出壳内chunk，index.html换module script加载；为桥接ES模块作用域，legacy api.js/app.js 各加一行 window.API=API / window.app=app（渲染层集成，无行为变化）** | screenshots/my-feedback-light.png, my-feedback-dark.png |
| 2b | im_channel.js → Vue视图（renderImChannel契约，桥接tokens） | 6e8223521 | 三平台卡/选中态/已连接徽标/表单/扫码/保存/解绑(Issue #294语义)等价；mock全局的独立harness实测渲染+computed style验证（玻璃cfg卡blur16px/输入框圆角）；真实壳内因该视图是repo级、测试账号无repo被正确引导到仓库大厅（老行为一致）。截图为harness态。 | screenshots/im-channel-light.png |
| 2c | organizations.js → Vue视图（renderOrganizations契约；AGModal/AGInput/AGSelect首次入壳，ag-ui.css改为全组件聚合） | 6510d7035 | 列表/个人空间徽标/角色色/详情/成员列表/移除成员/添加成员(用户搜索防抖)/编辑/删除/切换组织全部等价；mock harness实测（该视图与im-channel同为repo语境视图，测试账号无repo被壳引导到仓库大厅=老行为一致）；玻璃卡blur16px/18px实测。**settings.js重新分类：全局辅助函数被app.js内联onclick引用，属壳耦合，推迟到外壳批** | screenshots/organizations-detail.png |
