# gitsync agent-run.sh — 智能体使用说明

`agent-run.sh` 是给 multica 智能体的一键执行脚本：自动准备运行环境、验证前置条件、执行完整同步（git 同步 + ClawHub 发布 + DB 回写），并返回结构化结果。

## 一键调用

```bash
# 在 gitsync-platform-sync 技能目录内执行(脚本位于 scripts/ 下, 自动推导工具链根)
cd <gitsync-platform-sync 目录>
bash scripts/agent-run.sh \
  --multica-issue <multica_issue_id>
```

**参数**

| 参数 | 必填 | 说明 |
|---|---|---|
| `--multica-issue <uuid>` | 是 | multica 需求 ID（数据库 `t_skills_dev_requirement.multica_issue_id`） |
| `--config <path>` | 否 | gitsync 配置文件（默认 `gitsync/config.yaml`） |
| `--timeout <sec>` | 否 | `gitsync run` 执行超时上限（默认 **600 秒**；超时则退出码 2） |
| `--verbose` | 否 | 输出详细 JSON（默认输出紧凑 JSON） |
| `--force` | 否 | 允许非快进/全量重写（异常恢复用，日常不需要） |

**退出码**

| 码 | 含义 |
|---|---|
| `0` | 成功 / 无待处理 |
| `1` | 有差异 / db 回写问题（如 multica ID 不存在） |
| `2` | 守卫失败 / 环境错误 / **执行超时** |

## 脚本做了什么（5 步）

1. **运行时环境**：检查 python3(≥3.9)、git、node/npm
2. **gitsync 依赖**：自动安装 PyYAML
3. **clawhub CLI**：检查/安装 `clawhub`，验证登录态（`whoami`）
4. **git 远端**：`git ls-remote` 验证 gitcode/github 可达
5. **执行 gitsync run**：sync → publish → db-update

> 前置条件：SSH 密钥（gitcode/github）已在环境配置；clawhub 首次需手动 `clawhub login` 一次（生成 token）。

## 输出结构

脚本打印 `gitsync run` 的 JSON 摘要，**并写入固定路径** `data/products/run-result.json`（智能体直接读文件即可，不受日志干扰）：

```json
{
  "sync": {
    "status": "ok",
    "hop1_cherrypicked": [],
    "targets": {
      "github_master": {"status": "ok", "new_commits": 0, "message": ""},
      "ai_shell":      {"status": "ok", "new_commits": 0, "message": ""},
      "downstream":    {"status": "ok", "new_commits": 0, "message": ""}
    }
  },
  "publish":   {"status": "ok"},
  "db_update": {"status": "failed", "exit": 1}
}
```

## 智能体 subprocess 调用示例

```python
import subprocess, json, os

proc = subprocess.run(
    ["bash", "scripts/agent-run.sh",
     "--multica-issue", multica_id],
    capture_output=True, text=True,
    cwd="<gitsync-platform-sync 目录>",
)
rc = proc.returncode

# 从固定文件读取结构化结果（最可靠，不受日志干扰）
result_path = "<gitsync-platform-sync 目录>/data/products/run-result.json"
summary = json.load(open(result_path))

if rc != 0:
    failed = [k for k, v in summary.items() if v.get("status") == "failed"]
    print("告警：以下步骤失败", failed)
```

## 注意事项

- **multica_issue_id 必须真实存在**于数据库，否则 `db_update` 报错（退出码 1）——这是设计行为，避免误写
- **publish 较慢**（ClawHub 发布单次 ~30s + 版本冲突重试），完整 run 可能需 1-3 分钟，脚本不要设过短超时
- **幂等**：重复执行安全，内容未变则 publish 跳过、sync 无操作
- 脚本日志前缀 `[gitsync-agent]`，便于过滤；最后的 JSON 是机器可读结果
