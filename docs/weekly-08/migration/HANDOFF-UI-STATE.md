# 视觉债/色板阶段 · 未决问题交接（HANDOFF-UI-STATE）

> 2026-10-30 会话末落盘。新会话第一件事：读本文件。
> 姊妹文档：SESSION-STATE.md（迁移期）、HANDOFF-STATE.md（迁移收官）。
> 本文件管**视觉债/色板阶段的当前状态与未决问题**。

## 1. 分支与提交状态（2026-10-30）

- 分支：`weekly-clean-mys`，工作树干净（仅无关的 .hermes-handoff 未跟踪文件）
- 本阶段 commit 链（新→旧）：
  - `9466d8bd1` fix: 修复暗色 token 泄漏进浅色（OS暗色+应用浅色组合）——bridge 三处暗色媒体块从裸 `:root` 改为 `[data-theme=apple][data-theme-mode=auto]` 限定；6/6 组合矩阵验证通过
  - `b8f0a695e` fix: 场景市场页暗色豁免（主人裁决回退，见 §3）
  - `fed75ea1d1` feat: 色板全量清扫（apple-theme 38 处硬编码→var；六视图 136 处 token 化）
  - `24ff14eed` feat: 主人苹果暗色色板落地（暗色块强调色/border/玻璃 token）
  - `4f607b2dc` fix: 债#1#5（暗色表面抬升+卡片边界 token）
  - `fed63aa4f` fix: 债#2#6#6a（--t1 定义/dashboard 玻璃卡/im-channel 平台卡）
- 版本：apple-theme v23 / bridge v7 / scene-market chunk v11 / dash+issues+runtimes+autopilot+feedback+event chunk v10 / app.js v190
- API :3000 运行中（python3 -m src.api.run，磁盘直读静态文件）

## 2. 视觉债状态（ui-debt.md 8 条）

- ✅ #1 issues 太黑、#2 scene+im 太白、#3 霓虹配色、#5 卡片边界、#6 字色撞背景、#6a dashboard 白底
- ⬜ #4 skills/knowledge/chat 配色（主人未给具体不满点）
- ⬜ #7 admin 独立站无暗色（决策项：是否补 --c-* 暗色变量集）

## 3. ⚠️ 未决问题：主人报告的场景页异常（最高优先级）

主人 2026-10-30 反馈（原话归纳）：
1. "项目场景页面的动画效果也没了"
2. "场景的详情页里面执行流程的展示现在全是灰色不展示了，只有编辑模式才正常显示"
3. 截图在 `/home/mys/problem/screenshot-20261030-110700.png`（2310×547 浅色横向长条，PIL 只能确认浅色+左上蓝色 logo+文字带，**本会话模型无视觉能力读不出内容**；OCR 工具缺失：无 tesseract、无 sudo、easyocr 装 torch 超时被中断）

### 已排查（排除项）：
- **逻辑代码零改动**：SceneMarket.vue / SceneCard.vue 与批5（fc5a0edd7）逐字节相同，git diff 为空；工作树干净；最近 commit 都只动 CSS
- 无头浏览器 mock 复现（暗色 + 强制暗色两种路径）：执行流程卡 3 张正常渲染、色彩条莫兰迪色（rgb(176,122,139) 等）、`.sm__fcard-top`/`.sm__fnum` 计算色正常、服务端 CSS 里动画规则（scn-spin/scn-card:hover/渐变条）都在
- 疑点：**主人浏览器看的真实数据/真实环境 vs 我的 mock 环境**——mock 数据可能掩盖真实问题（如真实场景 skills 字段结构不同导致 agentMeta(sk) 返回空 → color undefined → 灰色）

### 最可疑的技术线索（新会话优先查）：
1. `agentMeta(sk).color` 依赖 `sk.tag || sk.agent_type` 命中 AGENT_META 的 vod/issue/coding/qa/sre/chat/auto 键——**真实后端返回的 skills 若 tag 是别的值，color 为 undefined**，内联 `background: undefined` → 卡顶条/数字圆点全部无色=灰。编辑模式（DashboardView 的 ap-nd）用的是 `agentColor(node.id)`（node.id 是 vod/issue 等硬键名）**不依赖数据字段** → 这完美解释"只有编辑模式正常"！！
2. 动画消失：scn-card hover/spinner 都在 app.css（v85 没变过）——查主人浏览器实际加载的 CSS 版本组合是否新旧混搭（缓存）
3. 亮色泄漏已修（9466d8bd1），主人需强刷验证是否已解决；截图是浅色模式（mean 250）说明主人看的是**亮色下的异常**——上面线索1在亮色同样成立（灰卡无关主题）

### 新会话第一步建议：
```
读本文件 → 无头浏览器用【真实后端数据】（真账号或主人账号的 mock 复刻）打开场景详情页
→ 取 .sm__fcard-top 的 background 计算值 → 若是 undefined/空 → 定位 agentMeta 匹配失败
→ 对照后端 /scenes/market/{id} 实际返回的 skills[].tag 值域 → 修 AGENT_META 映射或 tag 解析
```

## 4. 本阶段沉淀的规则（主人裁决，必须遵守）

1. **浅色模式绝对不动**——主人原话"我一点也不想影响浅色模式的代码"。所有暗色改动必须限定在暗色选择器内（`[data-theme=apple][data-theme-mode=auto/dark]`），禁止裸 `:root`（曾泄漏，见 9466d8bd1）
2. **场景市场页已豁免色板**（b8f0a695e）：`.sm` 根作用域内暗色回原值（--a 回 #0071e3 等），主人裁决"这次改动还不如原来的"——其他页面保持色板
3. 暗色块修改必须做 6/6 组合矩阵验证（OS偏好×应用模式）
4. 本会话模型无视觉能力：截图无法读取，需要图片判断时让主人文字描述或换有视觉的会话

## 5. 工具链备忘

- 无头 Chrome：9222（playwright connect_over_cdp；崩溃后重启命令：`/home/mys/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome --headless --disable-gpu --remote-debugging-port=9222 --no-sandbox about:blank` ——注意是 chrome-linux64 目录）
- 登录基线：vue-mig-test / VueMig-Test-2026
- 构建：`cd ~/grape/src/api/frontend/app && npx vite build`；改 bridge/entries 后必须 bump entries 里的 `?v=`（当前 v7）+ index.html 对应 chunk 版本
- 色彩盘点脚本：docs/weekly-08/migration/color_audit.py
- 截图核对脚本模式：palette-*3 系列（每主题新开页面防 emulate 泄漏）
