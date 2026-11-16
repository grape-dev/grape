# Grafana 看板（可恢复）

LGTM 容器**没有挂卷**，容器一重建，Grafana 里的数据源和看板就全没了。
所以看板 JSON 必须留在仓库里 —— 这里就是它的家。

## 恢复（容器重建后一条命令）

```bash
# 默认 127.0.0.1:3000, admin:admin
bash deploy/grafana/import-dashboards.sh

# 指向兄弟机
GRAFANA_URL=http://192.168.0.116:3000 bash deploy/grafana/import-dashboards.sh

# 只查现状，不写
GRAFANA_URL=http://192.168.0.116:3000 bash deploy/grafana/import-dashboards.sh --check
```

脚本做三件事，都是幂等的：

1. 探 `GRAFANA_URL/api/health`（连不上直接 fail，不假装成功）
2. 补数据源：`prometheus` / `loki` / `tempo`（按固定 uid，已存在的跳过 —— 不覆盖你手改的连接参数）
3. 用 `overwrite=true` 导入 `dashboards/*.json`（以仓库为准）

脚本只自动补**无认证**的数据源（prometheus / loki / tempo）。
`grape-mysql` 带库口令，故意不写进脚本 —— 重建后手动建一次（或从 `/etc/grape/` 下取）。
看板里如果用了它，重建时会提示数据源不存在，那时再补。

## 现有看板

| 文件 | uid | 说明 |
|---|---|---|
| `dashboards/grape-gsb-bus.json` | `grape-gsb-bus` | GSB 服务总线调用总览：走总线的调用速率/占比、流向、延迟、状态、实例分布，以及 HTTP 接口侧对照 |

## 新增看板的做法

```bash
# 1) 在 Grafana UI 里调好，导出 JSON（Share → Export）
# 2) 确认 uid 是稳定字符串（不要留空，否则重建后 uid 变化，脚本会重复建）
# 3) 放进 dashboards/，跑一次 import 验证幂等
bash deploy/grafana/import-dashboards.sh --check
```

约定：

- **uid 必须显式写死**（如 `grape-gsb-bus`），不能留空——空 uid 每次导入都会新建一份。
- 看板只用固定 uid 的数据源（`prometheus` / `loki` / `tempo` / `grape-mysql`），不要用变量或"默认数据源"，否则换环境后面板全空。
- 中文标题 + 判读说明（最后一个 markdown 面板写清"这块看板回答什么问题"），方便别人接手时不用猜。

## GSB 看板的现状（重要）

`grape-gsb-bus` 现在大部分面板是 "No data"，这是**真实情况**而不是面板坏了：

```
grape_gsb_client_* 只在 contracts/remote/*_remote.py 的 RemoteProvider(HTTP) 模式被调用。
当前跨域走的是：
  读  → owner 域 *_any 原语（DSN 直连）
  写  → outbox 异步写回
RemoteProvider 自身有回环风险（provider.py 注释：同域调用会 HTTP 打回自己），所以未启用。
```

所以"通过 GSB 的调用占比"现在是 0。这块看板的作用是**基线**：将来如果有人把跨域调用从 DSN 直连改成 RemoteProvider，占比会从 0 跳起来，那时一眼能看出边界被重划了。