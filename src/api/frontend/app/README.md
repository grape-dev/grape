# frontend/app — Grape Vue (phase 1 scaffold)

新前端唯一根目录（Vue 3 + Vite + vue-router，组合式 API）。老代码在 `../shell/`（迁移期由 shell 托管，迁一批删一批）。

## 命令

- `npm run dev` — 本地开发（vite）
- `npm run build` — 构建输出到 `../static-dist/`（FastAPI 挂载在 `/static-vue/`）
- `npm run lint` / `lint:fix` — ESLint（收编一页删一页豁免）
- commitlint：提交信息走 Conventional Commits，页面迁移批次用 `refactor(ui): migrate <page> to Vue`

## 结构

- `src/components/ui/` — @apple-ui 毛玻璃组件库（见其 README）
- `src/views/` — 迁移完成的新页面（每批一页）
- `src/styles/tokens.css` — 明/暗双主题变量，唯一视觉规范
- `src/router/` — vue-router，迁移页逐批挂路由

## 规矩

- 只用 tokens 变量，禁手写魔法值
- 新页面一律进 `src/views/`，禁止往 `../shell/` 加代码
- shell 侧改 CSS 仍要 bump `?v=`（直到清零）
