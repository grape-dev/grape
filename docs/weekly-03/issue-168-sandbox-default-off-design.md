# Issue #168 修复方案：GRAPE_AGENT_SANDBOX 默认关闭

> 分支：`fix/issue-168-sandbox-default-off`（基于 `grape-dev/weekly-03-hc` 最新）
>
> 关联：#168（P0 Bug）、#156（P1 系统性修复）、#64（沙箱隔离父 Issue）、#139（已 revert 网络隔离）

---

## 一、问题是什么

DevStage 创建的新 Worker 上三个引擎（hermes/opencode/codearts）全部 exit=1 失败。老 Worker 正常。

## 二、根因

commit `2c3f6a74`（XOcto, 9月3日）一次性引入了 bwrap 文件系统沙箱，包含三个组件：

| 组件 | 位置 | 内容 |
|------|------|------|
| 开关默认 on | config.py:454 | `GRAPE_AGENT_SANDBOX = os.environ.get("GRAPE_AGENT_SANDBOX", "on")` |
| 自动安装 bwrap | install-worker.sh:252 | section 5.5，root 模式自动 `apt-get install bubblewrap` + setuid |
| 沙箱实现 | src/agent/sandbox.py | `build_bwrap_cmd()` 构造 bwrap 白名单命令 |

三个条件同时满足时沙箱生效：`sandbox_enabled() == True` + `bwrap 二进制存在` + `cwd 有效`。

老 Worker 在 2c3f6a74 之前安装，没有 bwrap 二进制 → 条件2不满足 → `build_bwrap_cmd` 返回 None → 不套沙箱 → 正常。

DevStage 新 Worker 跑了 `install-worker.sh --root` → 自动装 bwrap → 三个条件全满足 → 沙箱生效 → 白名单不完整 → 引擎失败。

## 三、白名单不完整的具体表现

### hermes

```
executor.py:446  _iso_home = tempfile.mkdtemp(prefix="hermes-iso-")
                 → 产生 /tmp/hermes-iso-XXXXXX（宿主机 /tmp 上）

sandbox.py:76    opts += ["--tmpfs", "/tmp"]
                 → /tmp 被替换为空 tmpfs

sandbox.py:78-81 只 bind /tmp/hermes-acp-homes/{md5} 回宿主机
                 → /tmp/hermes-iso-XXXXXX 没被 bind → 沙箱内不存在

结果：hermes 进程 HERMES_HOME=/tmp/hermes-iso-XXXXXX → 找不到 config.yaml/.env
      → initialize timeout after 100s
```

### opencode

```
config.py:469  BWRAP_RO_ENGINE["opencode"] = ("~/.nvm", "~/.config/opencode", "~/.cache", "~/.opencode")
               → ~/.cache 被挂为只读（--ro-bind）

config.py:483  BWRAP_RW_ENGINE["opencode"] = ("/var/cache/bun-compile",)
               → 只有这一个路径可写

结果：opencode 运行时需要写 ~/.cache（Node 缓存）
      → Read-only file system → 0.8s 崩溃
```

### codearts

```
config.py:470  BWRAP_RO_ENGINE["codearts"] = ("~/.codeartsdoer", "~/.nvm", "~/.local", "~/.cache")
               → ~/.cache 同样只读

结果：同 opencode → Read-only file system → 0.8s 崩溃
```

### 探活通过但任务失败的原因

探活用 `subprocess.run` 直接跑 CLI，不经过 executor.py 的 bwrap 包装 → exit=0。
实际任务走 `executor.py → build_bwrap_cmd → bwrap 沙箱` → 引擎配置被隔离 → exit=1。

## 四、修复方案

### 方案选择

| 方案 | 内容 | 优点 | 缺点 |
|------|------|------|------|
| A 改默认 off | config.py 一行改动 | 最快止血，风险最低 | 文件系统隔离关闭，安全降级 |
| B 修白名单 | hermes bind /tmp/hermes-iso-* + opencode/codearts ~/.cache 移到 RW | 保留隔离 | #156 的范围，工作量大，需逐引擎验证 |
| C 不装 bwrap | 移除 install-worker.sh section 5.5 | 自然降级 | 不彻底，手动装了 bwrap 仍会触发 |

**选择方案 A**。理由：

1. #168 是 P0 急性故障，DevStage Worker 全部不可用，需立即恢复
2. 当前沙箱只有文件系统隔离这一层（网络隔离/cgroup/审计已在 PR #179 revert），白名单又不完整，"有 bug 的隔离"比"没隔离"更危险
3. #156 是系统性解决方案（引擎级白名单配置 + 执行路径统一），做完后白名单跟着引擎走不会再漏路径
4. 沙箱方案设计文档的依赖链：#156 前置 → #139 恢复 → P1。#156 没做之前 bwrap 不应该开

### 改动内容

文件：`src/config.py`
改动：1 行

```diff
-# 默认开启（关 = 回滚，设 GRAPE_AGENT_SANDBOX=off）；bwrap 沙箱隔离 agent 子进程。
-# 启用后 agent 子进程套 bwrap：只挂「基础系统 + 引擎运行时 + 工位 cwd」，
-# 敏感路径（configs/、data/sessions.db、.env、/root、~/.grape、其他仓库/issue）天然不可见。
-GRAPE_AGENT_SANDBOX = os.environ.get("GRAPE_AGENT_SANDBOX", "on").lower() in ("1", "true", "on", "yes")
+# 默认关闭（开 = 设 GRAPE_AGENT_SANDBOX=on）；bwrap 沙箱隔离 agent 子进程。
+# 关闭原因：Issue #168 — bwrap 白名单不完整，DevStage worker 三引擎全部失败。
+# 白名单修复由 Issue #156 系统性解决后方可重新开启。
+GRAPE_AGENT_SANDBOX = os.environ.get("GRAPE_AGENT_SANDBOX", "off").lower() in ("1", "true", "on", "yes")
```

### 不改什么

- `install-worker.sh` section 5.5 保留——bwrap 装了不用没有副作用，#156 修复后可直接开
- `sandbox.py` 保留——代码不动，只是 `sandbox_enabled()` 返回 False 导致 `build_bwrap_cmd` 返回 None
- `BWRAP_RO_ENGINE` / `BWRAP_RW_ENGINE` 保留——#156 会重构这些

### 效果

| Worker 类型 | 改动前 | 改动后 |
|------------|--------|--------|
| DevStage 新 Worker（有 bwrap） | sandbox_enabled=True → bwrap 生效 → 引擎失败 | sandbox_enabled=False → build_bwrap_cmd 返回 None → 不套沙箱 → 正常 |
| 老 Worker（无 bwrap） | sandbox_enabled=True 但 _bwrap_bin() 返回空 → 返回 None → 正常 | 不变，正常 |

## 五、验证

1. 语法检查：`python3 -c "import ast; ast.parse(open('src/config.py').read())"` ✅ 通过
2. 行为验证：`sandbox_enabled()` 返回 False → `build_bwrap_cmd` 返回 None
3. 测试：跑基线测试组，passed/failed 与 weekly-03-hc 基线一致（不引入新失败）
4. DevStage 验证：创建新 Worker → 跑任务 → 引擎不再 exit=1

## 六、影响范围

| 维度 | 影响 |
|------|------|
| 现有功能 | 无影响。沙箱关闭 = 回到 2c3f6a74 之前的运行状态 |
| 安全 | 文件系统隔离关闭。但当前网络隔离/cgroup/审计也已 revert，文件系统隔离是唯一残存且不完整的安全层 |
| 后续恢复 | #156 完成后设 `GRAPE_AGENT_SANDBOX=on` 即可重新开启，无需改代码 |
| 兼容性 | 老 Worker 无变化。新 Worker 回到正常状态 |

## 七、关联

- #168（本 Issue，P0 Bug）
- #156（系统性白名单修复，P1，前置条件）
- #64（沙箱隔离父 Issue）
- #139（网络隔离，已 revert，待 #156 完成后恢复）
- `2c3f6a74`（引入 bwrap default-on + auto-install 的 commit）
- `PR #179`（revert #139 网络隔离部分，未覆盖 bwrap 文件系统隔离）
