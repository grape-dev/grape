# 部署规范：hermes 模型名与网关 key 可用列表对齐（Issue #2545）

> 适用范围：所有部署 hermes 引擎的机器（主控本机直连引擎、Worker 节点、AI DevSpace 节点）。
> 背景：chat 页切换 hermes / hermes-worker 引擎出现 `HTTP 404: Model not found` 的根因，
> 是 `~/.hermes/config.yaml` 配置的模型名不在该 API key 所属网关的可用模型列表中——
> 网关原样透传 404，运行时才暴露给用户。

## 1. 硬性约定（必须遵守）

**模型名必须与 API key 所属网关的可用列表对齐。**

- `model.default`、`model_aliases` 中每个别名的 `model`、`custom_providers` 中的每个
  `model`，都必须能在「该 key 所属网关」的 `GET /v1/models` 返回列表中找到。
- 模型可用性是 **key 级** 的：同一个网关、不同 key，可用模型列表可能不同。
  配置正确的判断依据永远是「你实际用的那个 key 查到的列表」，而非网关文档或历史经验。

## 2. 换 key / 换网关的同步动作

| 变更 | 必须同步执行 |
|------|--------------|
| 更换 API key | 用新 key 重跑 §3 自检；新 key 可用列表若不覆盖已配置模型名，同步改 `model.default` / `model_aliases` |
| 更换网关（base_url） | 同步更新 `model.base_url` 与所有 `model_aliases`/`custom_providers` 的 `base_url`，并重跑 §3 自检 |
| 网关侧下线/更名模型 | 用旧配置自检会命中 `model_not_found`——把配置改为可用列表中的模型名 |
| 重新安装 worker | `install-worker.sh` 会在安装期自动校验（见 §4），不对齐即中止安装；`install-worker.ps1` 为软门——只告警不中止（`-SkipEngineCheck` 可整体跳过校验） |

> 本规范不自动改写用户已有的 `~/.hermes/config.yaml`——安装期只**校验并提示**：
> `install-worker.sh` 不对齐即中止，`install-worker.ps1` 仅告警继续（软门，见 §4）；
> 探活期只标记不健康并给出原因。修复动作由节点运维者执行，避免静默覆盖。

## 3. 自检命令

### 3.1 一键自检（推荐，主控/worker 通用）

```bash
# 主控源码环境（仓库根目录）
python3 -m src.domains.runtime.engine.engine_model --check

# Worker 节点（安装目录，venv 内）
~/grape/.venv/bin/python -m src.domains.runtime.engine.engine_model --check

# 指定 hermes home（多套配置/隔离环境）
python3 -m src.domains.runtime.engine.engine_model --check --hermes-home /path/to/hermes-home
```

退出码语义：

- `exit 0` + `OK: gateway ... has all configured models`：全部对齐，无需处理。
- `exit 0` + `Warning: gateway ... unreachable ... SKIPPED`：网关不可达（离线安装/网络故障），
  校验降级跳过——联网后务必重跑一次自检。
- `exit 1` + `MISALIGNED`（含「配置模型名 vs 可用模型列表」对照）：模型名不对齐，
  按对照把 `config.yaml` 中的模型名改为可用列表中的任一模型后重跑自检。

### 3.2 手动查网关可用列表

```bash
curl -s -H "Authorization: Bearer <你的key>" https://<网关域名>/v1/models | python3 -m json.tool
```

> 注意 base_url 已含 `/v1` 时网关列表地址为 `<base_url>/models`（如 `.../v1/models`）；
> `engine_model --check` 已自动处理两种形态。

## 4. 安装期与探活期防线（自动行为，供排障参考）

- **安装期**：`scripts/install-worker.sh` / `scripts/install-worker.ps1` 在 venv 就绪后、
  停旧 worker 之前调用 `python -m src.domains.runtime.engine.engine_model --check`：
  - `install-worker.sh`（Linux/macOS）：不匹配 → 打印对照并**失败退出**；
  - `install-worker.ps1`（Windows，Issue #3874 起为软门）：校验非零退出只打印
    `Warning: hermes model config validation FAILED ... (non-blocking, install continues)`，
    安装继续并完成 opencode 注册；需要完全跳过校验时加 `-SkipEngineCheck`
    （跳过校验、仅注册 opencode）；
  - 网关不可达 → 降级告警不阻断离线安装；无 `config.yaml`（本机不用 hermes）→ 跳过；
  - 解析器不支持的配置结构（如 YAML 列表 `- ` 块 / `[a, b]` 流式列表）→ 非阻断
    warning 并跳过该块（「无法校验 ≠ 配置错误」），Windows 安装不再因此中止。
- **探活期**：主控本机直连引擎探针（`src/runtime/local_probe.py`）在 hermes ACP 握手
  通过后追加一次 1-token 最小模型调用：模型名不合法 → 引擎标记**不健康**，探活详情带
  `model_not_found: <配置模型名>` 原因（而非 healthy 但实际调用 404）。
- 模型名读取与校验的统一实现：`src/runtime/engine_model.py`（安装脚本、探针、自检命令共用，
  Linux/Windows 行为一致）。

## 5. 常见问题

- **自检报 `no resolvable gateway target`**：`config.yaml` 缺 `api_key`/`base_url` 或
  `${ENV}` 引用的环境变量未注入（worker 侧检查 `~/.grape/runtime.env` 是否保留对应
  `HERMES_CUSTOM_*` 变量）。
- **探活显示 `probe_detail.model_call: model_not_found: X`**：把 `model.default` 与别名中的
  `X` 改为自检输出的可用模型名，改完无需重启 worker。
- 引擎使用与配置的进一步说明见 `help/usage/runtimes.md`。
