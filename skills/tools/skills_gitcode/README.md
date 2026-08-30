# skills_gitcode

GitCode 平台相关工具目录, 承载 GitCode webhook 事件处理的技能与脚本。

## 包含工具

| 目录/文件 | 说明 |
|-----------|------|
| `gitcode-event-handler/` | GitCode webhook 事件处理技能(Hermes 技能, 含 SKILL.md + 核心脚本) |
| `gitcode-event-handler/scripts/gitcode_event_handler.py` | 事件处理主脚本(issue/PR/评论/分发, 联动 Multica 四角色小队与数据库入库) |

## 技能说明

`gitcode-event-handler` 通过 Hermes Gateway 的 webhook 路由(`/webhooks/gitcode-event`)
接收 GitCode 推送的事件, 按固定业务逻辑处理:

- **Issue 创建/更新/评论** → 识别 skill 开发任务, 质量检查、判重、自动打标签,
  创建 Multica issue 分配给开发小队; 非 skill 任务按 codeowner 映射分配开发者
- **PR 创建/合并/评论** → 预校验(仅提醒不阻断) + 审查报告(30 项检测点)
  + 分配测试小队; 合入主分支写版本表并触发分发
- **分发监听** → `distribute_for_github` 分支(cherry-pick)走逻辑7,
  main/master 合入走逻辑8, push 事件按仓库配置走逻辑9(push-sync)
- **测试→验证交接** → `--check-handoff` 轮询自动 @验证小队并转移分配

详细逻辑见 `gitcode-event-handler/SKILL.md`(含处理逻辑 1-9、配置表、Pitfalls、验证)。

## ⚠️ 安全说明(仓库副本已脱敏)

本目录是**归档副本**: `SKILL.md` 与脚本中的敏感默认值
(GITCODE_TOKEN / MULTICA_TOKEN / DB_PASS / webhook secret)已替换为
`REPLACE_WITH_*` 占位符, 避免提交 secrets(对齐仓库 9790802 的约定)。

**部署时必须通过环境变量注入真实凭据:**

```bash
export GITCODE_TOKEN=<真实 token>      # GitCode API (账号 check_bot)
export MULTICA_TOKEN=<真实 token>      # Multica API
export DB_PASS=<真实密码>              # MySQL
python3 gitcode-event-handler/scripts/gitcode_event_handler.py --test   # 自检
```

真实凭据以**本机运行副本**为准:

```
/home/wangliangchu/.hermes/skills/software-development/gitcode-event-handler/
```

本机 webhook 生产链路指向运行副本(config.yaml 路由 prompt),
不指向本归档目录; 归档副本仅用于版本管理与新机器部署参考。
改动代码时以运行副本为准(或修改后同步两份)。

## 常用命令

```bash
# 自检(配置 + 数据库/GitCode/Multica 连通性 + 四小队 ID 校验)
python3 gitcode-event-handler/scripts/gitcode_event_handler.py --test

# 处理 webhook payload 文件 / 从 stdin 读取
python3 gitcode-event-handler/scripts/gitcode_event_handler.py /tmp/payload.json
echo '<json>' | python3 gitcode-event-handler/scripts/gitcode_event_handler.py -

# 测试→验证 交接轮询(cron 每 5-10 分钟)
python3 gitcode-event-handler/scripts/gitcode_event_handler.py --check-handoff
```

依赖: `pymysql`、`multica` CLI。
