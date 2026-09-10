# Issue-45 补充设计：grape-cli 打成 wheel 随 worker 包离线安装

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：Feature（worker 分发）| P1 | 状态：spec-ready（待评审）
> 分支：`weekly-02`
> 日期：2026-09-10
> 范围：把 `grape-cli` 打包成 wheel 并入 worker 安装包 `wheels/`，让弱网/离线 worker 也能获得 `grape` 命令，
> 替代当前「安装时 `pip install git+.../grape-cli.git`」的在线拉取。

## 一、背景与问题

`cli/` 拆独立项目 `gitcode.com/grape-dev/grape-cli` 后（2026-09-08），worker 安装改为安装期在线拉取：

```
install-worker.sh §4.5:  VENV/bin/pip install git+https://gitcode.com/grape-dev/grape-cli.git
```

问题（弱网/离线/私有仓库权限受限的 worker）：
- 需要**网络可达 gitcode** + **git 可用** + **读取 `grape-dev/grape-cli`（私有）的凭据**；
- 拉取失败仅告警 → worker 照常运行，但 **agent 内无 `grape`** → 依赖 CLI 的技能（VOD 试点等）不可用；
- 每次全新/重装都触发一次网络构建（build isolation 还会再下载 setuptools）。

## 二、目标

- **离线可获得**：worker 安装包自带 `grape`（wheel），安装不与网络/私有仓库绑定；
- **与依赖同一通道**：复用现有 `wheels/` 离线安装机制（`pip install --no-index --find-links wheels`）；
- **在线兜底保留**：wheel 缺失/失败时仍可回退在线 git 安装或告警（不阻塞 worker）。

## 三、方案

### 3.1 打包侧（服务端 build package 时）

在 `src/worker_package.py` 构建 worker 包时，**顺手把 grape-cli 打成 wheel 放进 `wheels/`**：

```
打包流程：
1. _ensure_wheels()：现有 pip download requirements-worker.txt → wheels/（含 click/httpx）
2. 新增 _ensure_grape_cli_wheel()：
   - 确保缓存里有一份 grape-cli 源码（懒拉缓存：git clone/浅拉
     https://gitcode.com/grape-dev/grape-cli.git 到 ${CACHE_DIR}/grape-cli-src，服务端已有 Jason7602 读凭据）
   - pip wheel ${CACHE_DIR}/grape-cli-src -w wheels/  → 产 grape-0.1.0-py3-none-any.whl（纯 Python，平台无关）
3. _wheels_fingerprint() 已遍历 wheels/ 全部 .whl → 新 wheel 自动进入包版本指纹，代码/CLI 变化即触发重建
```

- 产物随现有 `grape-worker.tar.gz` 的 `wheels/` 一起下发，`install-worker.sh` 解压后即可离线安装。
- 说明：CLI 是纯 Python（click/httpx），wheel 为 `py3-none-any`，与目标平台无关，可在服务端一次构建、多机复用。

### 3.2 安装侧（install-worker.sh）

离线分支追加安装 grape-cli（与依赖同一 `--no-index --find-links wheels`）：

```bash
# 现有离线依赖安装成功后再装 grape-cli
if "$VENV/bin/pip" install --quiet --no-index --find-links "$INSTALL_DIR/wheels" \
    grape-cli >/dev/null 2>&1; then
  chmod +x "$VENV/bin/grape" 2>/dev/null || true
  log "grape CLI 已装（离线 wheel）: $VENV/bin/grape"
else
  # 回退：在线 git 安装 → 仍失败才告警
  "$VENV/bin/pip" install -q --no-cache-dir \
      git+https://gitcode.com/grape-dev/grape-cli.git 2>/dev/null \
    && log "grape CLI 已装（在线）" \
    || log "警告: grape-cli 安装失败，agent 内暂无 grape；不影响 worker 运行"
fi
```

> 包名注意：`pyproject.toml [project] name="grape"`（wheel 名 `grape-*.whl`），
> `pip install grape-cli` 会从索引找 grape-cli 名——**应装 `grape`**：`pip install --no-index --find-links wheels grape`
> （与 `git+.../grape-cli.git` 的「项目名 grape、仓库名 grape-cli」一致）。

### 3.3 兼容与降级

| 场景 | 行为 |
|------|------|
| 包内含 grape wheel | 离线安装成功（首选）；无网络 worker 直接可用 |
| wheel 缺失/与 py 版本不匹配 | 回退在线 `pip install git+...`；再失败仅告警（现状保持） |
| 已装 worker 升级 | 无需额外动作；重跑 install-worker.sh 即按上流程更新 grape |
| 服务端构建机无法拉 grape-cli | wheel 缺失 → 走在线/告警兜底，不影响 worker 包其余部分 |

## 四、改动清单

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/worker_package.py` | `_ensure_grape_cli_wheel()`：懒拉 grape-cli 源码 + `pip wheel -w wheels`；纳入 wheels 指纹/包版本 | P0 |
| 2 | `scripts/install-worker.sh` | 离线分支 `pip install --no-index --find-links wheels grape`；失败回退在线 git；仍失败告警 | P0 |
| 3 | `tests/`（worker_package） | 断言 wheels/ 含 `grape-*.whl`；卸载/纯离线场景可安装 | P1 |

> wheel 需要随服务端构建机联网一次（拉 grape-cli 并 `pip wheel`）；产物离线复用。

## 五、验收标准

- [ ] 服务端 build package 后，`grape-worker.tar.gz` 内 `wheels/` 含 `grape-*.whl`
- [ ] 断网/离线环境执行 install-worker.sh → `.venv/bin/grape --version` 可用、`grape commands` 正常
- [ ] 包内无 grape wheel 时回退在线安装/告警，worker 不受影响
- [ ] `grape` CLI 更新后（build 新 wheel）包版本指纹变化 → 重装 worker 自动装新 CLI
- [ ] 现有依赖离线安装（click/httpx）行为不回归

## 六、风险与边界

| 风险 | 影响 | 缓解 |
|------|------|------|
| 服务端构建机无网络/拉不到 grape-cli | 无 wheel | 懒拉失败仅跳过（包照常构建）；安装侧在线/告警兜底 |
| wheel 名与包名混淆（grape-cli vs grape） | 装错/找不到 | 统一用项目名 `grape`；文档与脚本注释写明 |
| CLI 频繁更新 | 每次重建 wheel 增大包体（wheel ~几十 KB，可忽略） | 指纹随 wheels 变化自动重建；本地缓存复用源码减少网络 |
| 离线安装与 Python 版本 | 纯 py wheel 平台无关 | `py3-none-any`；同事 requirements 的分支校验 |

## 七、实施顺序

① worker_package 加 wheel 构建 → ② install-worker.sh 离线分支 → ③ 测试（含离线模拟）→ ④ 构建并部署主控 → ⑤ 重跑一台 worker 验证离线安装。