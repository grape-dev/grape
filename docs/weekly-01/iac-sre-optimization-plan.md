# Grape IaC-SRE 优化方案

## 一、问题全景

基于 issue #3/#4/#5 的完整追踪,共发现 14 个根因,归类为 4 大类:

| 类别 | 根因数 | 影响 |
|------|--------|------|
| 超时 & 进程管理 | 4 | SRE 100% 超时失败,僵尸进程污染流水线 |
| SKILL & 执行效率 | 6 | 单任务 30-40 分钟,远超 1200s 上限 |
| 流水线 & 路由 | 2 | quick-deploy 被错误推进到 coding/qa |
| 资源 & 并发 | 2 | 6 用户争抢,资源重复创建 |

---

## 二、优化方案 (按优先级 P0→P3)

### P0-1: iac-sre 独立超时配置

**问题**: 全局 1200s,iac-sre 实际需要 30-40 分钟

**方案**: 按 agent_type + skill 设定超时

```python
# src/runtime/agent.py — _wrap 函数内
_TIMEOUT_OVERRIDES = {
    ("sre", "iac-sre"): 2400,      # IaC 部署: 40 分钟
    ("sre", "cloud-deploy"): 1800, # 应用部署: 30 分钟
    ("coding", "implementation"): 1800,
    ("vod", "iac-vod"): 300,       # VOD 分析应该很快
}
task_timeout = _env_float("RUNTIME_TASK_TIMEOUT", 1200)
_override = _TIMEOUT_OVERRIDES.get((agent_type, skill_name))
if _override:
    task_timeout = float(os.environ.get(f"RUNTIME_TIMEOUT_{agent_type}_{skill}", _override))
```

**改动量**: 1 个文件, ~15 行
**风险**: 低

---

### P0-2: 超时 kill 改用 pgid 追踪 (弃用 cwd 匹配)

**问题**: `_kill_stale_engine` 按 cwd 匹配进程,hermes isolation / terraform chdir 后匹配失败

**方案**: Popen 时记录 pgid,超时直接按 pgid kill

```python
# src/agent/executor.py — _run_once 函数内
proc = subprocess.Popen(..., start_new_session=True)  # 已有
_track_proc(proc)

# 新增: 记录 pgid 到 task
_pgid = os.getpgid(proc.pid)
# 通过环境变量或共享字典传递 pgid
_ACTIVE_PGIDS[task_id] = _pgid

# src/runtime/agent.py — _kill_stale_engine 改写
def _kill_stale_engine(task_id: str, backend: str = "opencode"):
    pgid = _ACTIVE_PGIDS.get(task_id)
    if pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
            # 3s 后 SIGKILL
            threading.Thread(target=_force_kill, args=(pgid, 3), daemon=True).start()
            return True
        except ProcessLookupError:
            return True  # already dead
    # fallback: 旧的 cwd 匹配逻辑
    ...
```

**改动量**: 2 个文件, ~40 行
**风险**: 中 (需要确保 pgid 在 worker 重启后清理)

---

### P0-3: Worker 暴露远程 kill 接口

**问题**: Server 无法杀掉远程 worker 上的僵尸进程

**方案**: Worker 注册 `POST /tasks/{tid}/kill` 接口

```python
# worker 侧 (src/runtime/agent.py 的 HTTP server 部分)
async def handle_kill(task_id: str):
    pgid = _ACTIVE_PGIDS.get(task_id)
    if pgid:
        os.killpg(pgid, signal.SIGTERM)
        await asyncio.sleep(3)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except:
            pass
    # 同时 kill 所有 cwd 匹配的子进程 (兜底)
    _kill_stale_engine(task, backend)
    return {"ok": True, "killed": True}

# server 侧 (src/api/routes/events.py — terminate_event)
# 超时后: 先调 worker kill API, 确认后再标记 failed
```

**改动量**: 3 个文件, ~60 行
**风险**: 中

---

### P0-4: A2A 端点加路径校验

**问题**: 僵尸进程能调 A2A 推进流水线,quick-deploy 不该出现 coding/qa

**方案**: A2A 端点校验 issue 标签和 task 状态

```python
# src/api/routes/webhook.py — A2A handler 内
# 收到 A2A 时校验:
# 1. issue 是否有 quick-deploy 标签
# 2. 如果有,拒绝 agent_name in ("coding", "qa")
# 3. 检查来源 task 是否已 failed (failed 的 task 不能调 A2A)

if issue_number:
    # 查 issue labels
    labels = get_issue_labels(repo_key, issue_number)
    if "quick-deploy" in labels and agent_name in ("coding", "qa"):
        log.warning(f"A2A rejected: quick-deploy issue #{issue_number} cannot route to {agent_name}")
        return JSONResponse(status_code=403, content={
            "status": "rejected",
            "reason": "quick-deploy path does not allow coding/qa"
        })

    # 检查来源 task 是否已 failed
    # (需要 A2A 调用时带上 source_task_id)
```

**改动量**: 1 个文件, ~30 行
**风险**: 低

---

### P1-1: 预置 TF 模板 + Provider Schema

**问题**: agent 从零写 TF 代码,6-7 轮 plan→fix 循环,耗时 5+ 分钟

**方案**: iac-sre SKILL 目录预置模板和 schema 文档

```
skills/iac/iac-sre/
  SKILL.md
  references/
    huaweicloud-deploy-runbook.md
    gitcode-api-reference.md
    build-debug-checklist.md
  templates/                    ← 新增
    main.tf.template            # 6 资源完整模板 (VPC/Subnet/SG/ECS/RDS/EIP)
    variables.tf.template       # 变量定义
    outputs.tf.template         # 输出定义
    backend.tf.template         # OBS backend 配置
  references/
    provider-schema-v1.94.0.md  ← 新增: v1.94.0 的完整 schema 文档
    credential-guide.md         ← 新增: AK/SK 使用指南
```

SKILL.md 中增加指令:
```
## 快速部署执行步骤

1. 复制 templates/ 下的 .tf 文件到临时目录
2. 替换变量: prefix, region, az, passwords
3. terraform init → plan → apply
4. 不要自行编写 TF 代码, 除非模板不满足需求
```

**改动量**: 新增 6 个文件, 修改 SKILL.md
**预期收益**: 消除 schema 探索循环 (省 5 分钟), 消除 TF 编写错误 (省 3 分钟)

---

### P1-2: Worker 镜像预装 provider + 统一配置

**问题**: arm64 provider 需要从 GitHub 下载,8 分钟排错

**方案**: Worker 镜像构建脚本预装

```bash
#!/bin/bash
# worker-image-setup.sh — 在 worker ECS 镜像构建时执行

# 1. 预装 terraform
TERRAFORM_VERSION=1.9.8
wget -q https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_arm64.zip
unzip terraform_${TERRAFORM_VERSION}_linux_arm64.zip && mv terraform /usr/local/bin/

# 2. 预装 huaweicloud provider (arm64, 从 GitHub releases)
PROVIDER_VERSION=1.94.0
mkdir -p ~/.terraform.d/plugin-cache/registry.terraform.io/huaweicloud/huaweicloud/${PROVIDER_VERSION}/linux_arm64/
curl -sL -o /tmp/hw_provider.zip \
  "https://github.com/huaweicloud/terraform-provider-huaweicloud/releases/download/v${PROVIDER_VERSION}/terraform-provider-huaweicloud_${PROVIDER_VERSION}_linux_arm64.zip"
unzip -o /tmp/hw_provider.zip -d ~/.terraform.d/plugin-cache/registry.terraform.io/huaweicloud/huaweicloud/${PROVIDER_VERSION}/linux_arm64/
chmod +x ~/.terraform.d/plugin-cache/registry.terraform.io/huaweicloud/huaweicloud/${PROVIDER_VERSION}/linux_arm64/terraform-provider-huaweicloud_v${PROVIDER_VERSION}

# 3. 预装 random provider
RANDOM_VERSION=3.6.3
mkdir -p ~/.terraform.d/plugin-cache/registry.terraform.io/hashicorp/random/${RANDOM_VERSION}/linux_arm64/
# ... 同理下载

# 4. 配置 terraform CLI 使用本地 mirror
cat > ~/.terraformrc << 'EOF'
plugin_cache_dir = "$HOME/.terraform.d/plugin-cache"
EOF

# 5. 预装 obsutil, hcloud CLI
# ...
```

**改动量**: 1 个脚本, worker 重建 1 次
**预期收益**: 消除 provider 下载排错 (省 8 分钟)

---

### P1-3: 统一凭证注入

**问题**: HUAWEICLOUD_SDK_AK (20字符 IAM) 有 huaweicloud provider 权限但无 OBS 权限, agent 花 20 步排错

**方案**: 注入两套凭证, SKILL 文档明确说明

```python
# src/agent/executor.py — 凭证注入部分
# 注入用户凭据时, 同时注入 OBS 专用凭证
if user_id > 0:
    from src.user.db import get_credentials
    for c in get_credentials(user_id):
        for k, v in c.get("config", {}).items():
            env[k.upper()] = str(v)
    
    # 确保 OBS 凭证可用 (如果 IAM AK 无 OBS 权限, 注入账号级 AK)
    if not env.get("OBS_ACCESS_KEY") and env.get("HUAWEICLOUD_SDK_AK"):
        # 尝试从 user_accounts 获取 OBS 专用 AK/SK
        obs_ak, obs_sk = get_user_obs_credentials(user_id)
        if obs_ak:
            env["OBS_ACCESS_KEY"] = obs_ak
            env["OBS_SECRET_KEY"] = obs_sk
            env["AWS_ACCESS_KEY_ID"] = obs_ak  # terraform S3 backend
            env["AWS_SECRET_ACCESS_KEY"] = obs_sk
```

credential-guide.md:
```
## 凭证使用指南

环境中有两套凭证:
- HUAWEICLOUD_SDK_AK/SK: IAM 用户级, 用于 huaweicloud provider (VPC/ECS/RDS)
- OBS_ACCESS_KEY/SECRET_KEY: 账号级, 用于 OBS (terraform backend + 文件上传)

如果只有 HUAWEICLOUD_SDK_AK/SK, 创建新 OBS bucket (自己 own 即有权限),
不要尝试访问他人创建的 bucket。
```

**改动量**: 1 个代码文件 + 1 个文档文件
**预期收益**: 消除 OBS 凭证排错 (省 3 分钟)

---

### P1-4: 资源隔离 — 每个 issue 独立前缀 + tfstate

**问题**: issue #3 和 #5 用相同前缀 blog-mlc, 资源冲突

**方案**: SKILL 强制要求加 issue number

```markdown
# iac-sre SKILL.md — 快速部署步骤

### 资源命名规则
资源前缀必须包含 issue number, 格式: `{prefix}-issue{number}`
示例: issue #5, 前缀 blog-mlc → 资源名为 `blog-mlc-5-vpc`, `blog-mlc-5-ecs`

### tfstate 隔离
OBS tfstate key 必须包含 issue number:
key = "issue-{number}/terraform.tfstate"
```

**改动量**: 修改 SKILL.md
**预期收益**: 消除资源冲突排错 (省 3 分钟), 避免重复创建

---

### P1-5: 拆分巨型 Step — SKILL 强制分步

**问题**: Step 1 内 30+ tool call, 897 秒, 上下文膨胀

**方案**: SKILL 用 todo 强制分步

```markdown
# iac-sre SKILL.md

## 执行步骤 (严格按顺序, 每步一个 todo)

### Step 1: 环境检查 (≤30s)
- read SKILL.md
- bash: `terraform version; obsutil version; env | grep HUAWEICLOUD`
- **不要读项目源码** (VOD 已分析过)

### Step 2: 创建 OBS bucket (≤30s)
- bash: obsutil mb
- 配置 terraform backend

### Step 3: 复制 TF 模板 (≤10s)
- cp templates/*.tf → /tmp/tf-deploy-{issue}/
- 替换变量 (prefix, region, az, passwords)

### Step 4: terraform init + plan (≤60s)
- bash: terraform init && terraform plan

### Step 5: terraform apply (≤600s)
- bash: terraform apply -auto-approve
- 等待 RDS 创建完成

### Step 6: 生成清单 + 上传 OBS (≤30s)
- terraform output -json > inventory.json
- obsutil cp inventory.json obs://...

### Step 7: 回复 Issue 评论 (≤30s)
- curl GitCode API

每步完成后更新 todo, 不要跳步。
```

**改动量**: 修改 SKILL.md
**预期收益**: 消除巨型 step (省 5 分钟), 上下文不膨胀, LLM 调用更快

---

### P2-1: SRE SKILL 硬规则强化 — 禁止 A2A

**问题**: SRE 完成后自行调用 A2A 触发 coding (issue #3)

**方案**: SKILL prompt 级别强制 + 代码拦截

```markdown
# iac-sre SKILL.md — 硬规则

### 完成后的动作 (严格按此顺序)

1. terraform output 生成 inventory.json
2. obsutil 上传到 OBS
3. curl GitCode API 回复 Issue 评论 (包含资源清单 + OBS 路径)
4. **停止。不要调用 A2A。不要触发 coding/qa。**

### ❌ 绝对禁止
- 部署成功后调用 A2A → coding agent
- 部署成功后调用 A2A → qa agent
- 自行决定"下一步应该部署应用" (这不是 SRE 的职责)
- 读取 blog_api.py 源码 (SRE 只管基础设施, 不碰应用)
```

代码拦截 (P0-4 已覆盖): A2A 端点校验 quick-deploy 标签。

**改动量**: 修改 SKILL.md (P0-4 已覆盖代码侧)
**预期收益**: 杜绝僵尸进程违规推进流水线

---

### P2-2: Sweeper 增加 failed-task 进程清理

**问题**: 任务 failed 后僵尸进程仍在跑

**方案**: sweeper 周期扫描 failed 任务的残留进程

```python
# src/runtime/cleanup.py — sweep_once 内新增

def _cleanup_zombie_processes():
    """扫描 recently-failed 任务, 确保对应子进程已终止"""
    with db_lock:
        c = db.cursor()
        # 最近 30 分钟内 failed 的任务
        c.execute("""
            SELECT id, runtime_id, cwd, agent_type, completed_at
            FROM agent_tasks 
            WHERE status='failed' 
            AND completed_at > datetime('now', '-30 minutes')
        """)
        for task_id, runtime_id, cwd, agent_type, completed_at in c.fetchall():
            # 检查该 task 是否还有活跃子进程
            if _has_live_process(cwd, agent_type):
                log.warning(f"zombie process found for failed task {task_id}, killing")
                _kill_stale_engine({"id": task_id, "cwd": cwd, "agent_type": agent_type}, 
                                   _backend_for_agent(agent_type))
```

**改动量**: 1 个文件, ~40 行
**预期收益**: 僵尸进程在 30 分钟内被清理, 不会违规发评论/调 A2A

---

### P3-1: 共享账号 VPC 配额管理

**问题**: 6 用户共用华为云账号, VPC 配额 5 个上限

**方案**: 
- 短期: SKILL 中要求 agent 先检查配额, 配额不足时报错而非等待
- 长期: 每个用户独立子账号 / 使用企业项目隔离

```markdown
# iac-sre SKILL.md — 前置检查

### 配额检查 (terraform plan 前必须执行)
- bash: `hcloud VPC ListVpcs --region=cn-north-4 | wc -l`
- 如果 VPC 数量 >= 4 (配额 5, 留 1 个 buffer), 停止并报错:
  "VPC 配额不足, 请清理无用 VPC 后重试"
```

---

### P3-2: Runtime 扩容 + 调度优化

**问题**: 5 个 runtime × 4 并发 = 20 槽位, 6 用户并发时不够

**方案**:
- 扩容到 8 个 worker (32 槽位)
- 调度时按 repo_key 分组, 同一 repo 的任务串行 (避免 issue #3/#5 互相干扰)
- hermes 引擎取消并发 1 的限制 (目前 hermes 限制只能跑 1 个任务, 是因为模型 API 限流)

---

## 三、优化效果预估

| 优化项 | 当前耗时 | 优化后 | 节省 |
|--------|----------|--------|------|
| 预置 TF 模板 | 8 min (写+修) | 1 min (复制+填变量) | 7 min |
| Worker 预装 provider | 8 min (下载排错) | 0 min | 8 min |
| 统一凭证 | 3 min (排错) | 0 min | 3 min |
| 资源隔离 | 3 min (冲突处理) | 0 min | 3 min |
| 拆分 Step | 5 min (巨型 step) | 2 min (分步) | 3 min |
| Provider schema 文档 | 5 min (反查) | 0 min | 5 min |
| **合计** | **~32 min** | **~13 min** | **~19 min** |

理想执行时间 ~13 分钟, 在 2400s (40min) 超时内绰绰有余。

## 四、实施路线图

```
Week 1 (P0 — 止血):
  ├── iac-sre 超时改为 2400s
  ├── A2A 端点加 quick-deploy 校验
  ├── _kill_stale_engine 改用 pgid
  └── Worker 远程 kill 接口

Week 2 (P1 — 提效):
  ├── 预置 TF 模板 + schema 文档
  ├── Worker 镜像预装 provider
  ├── 统一凭证注入
  ├── 资源前缀加 issue number
  └── SKILL 拆分执行步骤

Week 3 (P2 — 加固):
  ├── SRE SKILL 硬规则强化
  ├── Sweeper 僵尸进程清理
  └── 端到端测试

Week 4 (P3 — 扩容):
  ├── Runtime 扩容
  ├── VPC 配额管理
  └── 调度优化
```
