# hcloud CLI Installation Guide

> KooCLI / hcloud 是华为云的官方 CLI。Phase 2（tech-research）和 Phase 4（test-execution）
> 需要它来执行 `executor=cli` 的命令。SDK 模式（`executor=sdk`）也需要先装 Python SDK。

## Install hcloud (KooCLI)

```bash
# curl install (Linux/Mac)
curl -sSL https://apiexplorer.developer.huaweicloud.com/install/hcloud/install.sh | bash

# pip install (cross-platform)
pip install huaweicloudcli

# Windows (PowerShell)
Invoke-WebRequest -UseBasicParsing -OutFile install.ps1 https://apiexplorer.developer.huaweicloud.com/install/hcloud/install.ps1
.\install.ps1

# verify
hcloud version
```

## Configure Credentials

### Option A: hcloud CLI config（推荐，长期使用）

```bash
hcloud configure set \
  --cli-profile=default \
  --cli-access-key=<YOUR_ACCESS_KEY> \
  --cli-secret-key=<YOUR_SECRET_KEY>
```

### Option B: Environment variables（推荐，CI / 临时测试）

```bash
# Linux / Mac
export HUAWEI_ACCESS_KEY=<your_access_key>
export HUAWEI_SECRET_KEY=<your_secret_key>

# Windows PowerShell
$env:HUAWEI_ACCESS_KEY = "<your_access_key>"
$env:HUAWEI_SECRET_KEY = "<your_secret_key>"
```

> 测试框架的 `lib/utils.sh: ensure_ak_sk()` 会按以下优先级解析凭证：
> 1. 任意 `HUAWEI*` / `HW*` / `HWC*` 开头的环境变量（匹配 `*_AK` / `*_SK` / `*_ACCESS_KEY` / `*_SECRET_KEY`）
> 2. hcloud CLI config
> 3. TTY prompt（直接终端运行时）
>
> 非交互（agent / CI）下如果都没有，会 emit `__HUAWEI_SKILL_TESTER_CRED_REQUEST_v1__` sentinel 并 exit 77。

## Verify Configuration

```bash
hcloud ECS ListServers --cli-region=cn-north-4 --limit=1
```

期望：返回 ECS 实例列表（即使为空 `{"servers": [], "total_count": 0}` 也算成功）。

## Verify Python SDK (可选，Phase 2/4 SDK mode)

```bash
pip install huaweicloudsdkcore huaweicloudsdkrds   # 替换成对应服务

python3 -c "from huaweicloudsdkrds.v2 import *; print('SDK OK')"
```

## Common Issues

| 现象 | 原因 | 解决 |
|------|------|------|
| `hcloud: command not found` | PATH 没设 | `export PATH="$HOME/.hcloud/bin:$PATH"`（按 install 输出） |
| `AK/SK 凭证缺失（退出码 77）` | 环境无凭证 + 非 TTY | 显式 set `HUAWEI_ACCESS_KEY` + `HUAWEI_SECRET_KEY` |
| `[USE_ERROR] cli-region的值不支持` | region 不在白名单 | 用 `HUAWEI_REGION=cn-north-4`（默认）或换被测 skill 的真实 region |
| Python3 找不到（Windows） | WindowsApps alias 冲突 | 用 shim 或 `C:\Program Files\Python311\python.exe` 显式路径 |

完整的环境变量 / 参数说明见 `SKILL.md` § Parameters / § Environment Variables。
