---
name: gitsync-platform-sync
description: Use when 同步 GitCode 代码到 GitHub/ClawHub/DB 或排查平台内容不一致。全仓库增量镜像工具链, 自动 cherry-pick+身份重写+自校验+DB 回写, 幂等可重跑。
version: 1.2.0
author: Hermes Agent
license: MIT
category: software-development
metadata:
  hermes:
    tags: [gitcode, github, clawhub, sync, git-mirror, db-writeback, skill-publish]
    related_skills: [gitcode-event-handler]
---

# GitCode → GitHub / ClawHub / DB 同步(gitsync 工具链)

## Overview

将 GitCode 源仓(生产分支)的代码按 hop1+hop2 两跳增量镜像到 GitHub、GitCode 内部
分支(ai_shell)、下游仓库, 并把 skill 发布到 ClawHub, 最后按 multica issue 回写
MySQL(t_skills_dev_requirement / t_skills_dev_flow)。幂等, 可安全重跑。

本技能**自带完整工具链源码**(src/gitsync/ 25 模块 + scripts/agent-run.sh),
clone/复制技能目录后即可运行, 不依赖外部安装。与 `platform-skill-sync` 的区别:
后者是单 skill 手工 copy+push 的老流程(分发小队用); 本技能是**全仓库** git
镜像工具链, 自动 cherry-pick + 身份重写 + 自校验, 优先用本技能。

## When to Use

- GitCode 源仓 main 有新 commit 需要镜像到 GitHub/下游
- skill 已合入, 需要发布到 ClawHub 并回写 DB 平台状态
- 排查"GitHub 和 GitCode 内容不一致"类问题
- multica 智能体执行分发(用 scripts/agent-run.sh)

Don't use for: 单 skill 手工 copy+push 分发(用 platform-skill-sync); webhook
事件处理(Issue/PR/分发, 用 gitcode-event-handler)。

## 工具位置与运行

```bash
# 工具链根目录 = 本技能目录(权威运行目录, 含 src/、scripts/、templates/、references/、
# config.yaml、data/)。multica 分发等所有实际运行都在此目录进行, 不要再复制到别处跑。
cd <本技能所在目录>   # 例: ~/.hermes/skills/software-development/gitsync-platform-sync
# 未 pip 安装时用 PYTHONPATH 直接跑:
PYTHONPATH=src python3 -m gitsync.cli --config config.yaml <command>
# 或 pip install -e . 后直接用 gitsync <command>
# 配置也可用环境变量 GITSYNC_CONFIG 指定
# 新环境首次部署: 复制 templates/config.yaml.example 为 config.yaml 并填入真实仓库/DB 配置,
# 并创建 data/ 目录(map.txt / clawhub-manifest.json / products/ 由工具自动生成)
```

**目录即权威**: 本技能目录是唯一运行位置(config.yaml + data/ 已迁入, 路径全部指向技能
目录内)。旧运行副本 `/home/wangliangchu/PublishAgent/gitsync` 已弃用, 仅作历史备份,
勿再手动改动或复制其内容回来(改动一律以技能目录为准)。

前置: git、python3>=3.9、PyYAML(已装)、SSH 密钥(id_ed25519 已配置, gitcode/github
双 SSH)、clawhub CLI(`npm i -g clawhub`, 登录态 `clawhub whoami`)。

## 架构(两跳同步链)

```
gitcode main ──①hop1──▶ gitcode github分支 ──②hop2──▶ github remote main(身份重写)
  (Squash线性)   cherry-pick(无-x,保留作者)        ├──▶ gitcode ai_shell 分支(SHA 原样)
                                                     └──▶ 下游仓库 master(身份重写, SHA 与 github 一致)
```

- **hop1**: main 新 commit 全量 cherry-pick 到 github 分支(在临时非 bare clone 中做,
  合并提交用 `-m 1`, "now empty" 自动 skip; 推回源仓, 并发冲突自动 fetch+rebase 重试≤5次)
- **hop2**: github 分支新 commit 经 commit-tree 重放到各 target, 父提交经 map 文件映射;
  `rewrite_identity: true` 的目标把 author/committer 换成固定身份(config.identity),
  **其余字段(tree/message/时间戳)字节级保留**, 重写是纯函数 → 同输入必同 SHA(幂等根基)
- **配对策略**: `content` = tree+message+author_date+committer_date 签名(身份除外);
  `sha` = SHA 直接相等(仅用于不重写目标)

## 配置(config.yaml, 勿外传: 含真实 DB 密码)

| 段 | 说明 |
|---|---|
| `identity` | 重写后的固定身份(name/email) |
| `remotes` | gitcode / github / gitcode_ai_shell / gitcode_downstream 的 SSH URL(工具不拼接 URL) |
| `branches` | `source_master`(源生产分支, 当前=main)、`source_branch`(中间分支, =github) |
| `targets[]` | name / remote / branch / rewrite_identity / match_strategy(content\|sha) / verify_against / **enabled**(需求2: 目标开关, false 时不参与同步/发布/DB 回写) |
| `map_file` | commit 映射缓存(append-only, 可 `map rebuild` 重建) |
| `acknowledged_commits` | 外来历史白名单(需显式列出才放行) |
| `backup_ref` | force 前自动备份分支前缀 `refs/heads/backup/pre-sync-` |
| `clawhub` | base_url / organization(拼 skill 页 URL, 必须配) / config_path / skills_dir / manifest 双文件 / **exclude_skills**(需求3: 发布黑名单, 精确名或 glob, 命中不 ClawHub 上报) / **publish_mode**(`cli`=官方 CLI 逐文件串行, `native`=内置并发上传器, 默认 cli) / **publish_concurrency**(native 并发文件数, 默认 6) |
| `db` | MySQL + product_dir |

模板见 `templates/config.yaml.example`。当前部署要点: 源仓=developer-skill/
huaweicloud-skills-dev(生产分支 **main**); ai_shell 目标与源仓**同一 URL**, 只是
分支名(ai_shell); 下游=huaweiskilldev/huaweicloud-skillsdev, `verify_against:
github_master`(重写身份相同 → 其 master SHA 必须 == github main SHA)。

## 命令

| 命令 | 用途 | 典型用法 |
|---|---|---|
| `run --multica-issue <uuid>` | 一键: sync→publish→db-update, 失败不中断, 输出 JSON 摘要 | 日常主流程 |
| `sync` | 仅 git 多目标增量同步(hop1+hop2+推送) | 排查/手动同步 |
| `check --json` | 只读幂等/守卫校验(0=已同步, 1=有差异) | 先跑这个看状态 |
| `lookup <sha> --json` | commit 双向回溯(master↔分支↔remote) | 溯源 |
| `bootstrap --dry-run` | 首次初始化探测(EMPTY/incremental/full rewrite 计划), 结构不符需 --force | 新目标 |
| `map rebuild` | 重建 map 缓存 | map 丢失/损坏 |
| `publish` | ClawHub skill 发布(A2: 本地版本号+自愈) | 单独发布 |
| `db-update --multica-issue <uuid> [--target x]` | 按 multica 回写平台状态 | run 内部调用 |

agent 一键脚本(自动装依赖/验证前置/超时控制/写 run-result.json):
```bash
bash scripts/agent-run.sh --multica-issue <uuid> [--force] [--verbose] [--timeout 900]
```
结果写入 `data/products/run-result.json`(固定路径, 智能体直接读)。
脚本使用说明见 `references/agent-run-guide.md`, 工具链架构细节见 `references/toolchain-readme.md`。

## 退出码

- `0` 成功 / 已同步
- `1` 有差异(check)或 db 回写问题(multica 不存在/未命中行, **设计行为防误写**)
- `2` 守卫失败(外来历史/非快进无 --force)/环境错误/超时

## 安全护栏

1. 外来历史守卫: 目标 head 不是 expected 的祖先 → 拒绝(除非 acknowledged_commits 白名单)
2. 非快进守卫: 必须显式 --force; force 前**自动建备份分支** `refs/heads/backup/pre-sync-<时间戳>-<pid>` 并推远端
3. bootstrap 配对失败绝不静默(需人工确认结构)
4. push 后自校验; verify_against 目标 head != 参照目标 head → guard_failed

## 中间产物

- **map.txt**(data/map.txt): 每行 `<github分支sha> <remote sha> <master sha>`, 追加式, 原子写
- **clawhub-manifest.json + .backup.json**(data/): 每 skill 的发布状态机,
  是 in_review 状态的**唯一持有者**(公开 API 不可见), 双副本冗余, 原子写
- **products/latest.json**: 本轮产物, 每 skill 含 `urls{github,gitcode,clawhub}` + `platforms`(本轮成功的平台); `run` 在 db-update **之前**写它
- URL 规则: github 用目标分支(main), gitcode 用中间分支(github), clawhub 用 `<base>/<org>/skills/<name>`

## ClawHub 发布(A2)

- 本地管理版本号, 每段两位数上限: `1.0.99 → 1.1.0`
- content_hash(SHA-256 全文件指纹)未变 → 跳过(幂等快路径)
- 变了 → `clawhub skill publish --dry-run` probe; 已发布(unchanged) → 用 ClawHub 报告的版本重建 manifest(自愈)
- 真变了 → bump patch 后显式 `--version` 发布; "already exists" 冲突 → 再 bump 重试(自愈, 绝不重传同号)
- 发布慢: 每 skill ~30s, 完整 run 1-3 分钟, 脚本超时别设太短

## DB 回写语义

- `multica_issue_id → t_skills_dev_requirement.skill_name`(一对一; 命中多行 → 脏数据拒绝)
- `UPDATE t_skills_dev_flow SET platform_<target>_status='published', platform_<target>_url=..., platform_<target>_time=NOW() WHERE requirement_id=...`
- target 白名单: github|gitcode|clawhub(列名插值前校验)
- 只写 `platforms` 中列出的平台(旧产物无 platforms 字段则回退全写); `platform_huaweicloud_*` 不碰; UPDATE 幂等可重放

## Common Pitfalls

1. **config.yaml 含 DB 明文密码** — 禁止打印/外传/进日志; 入库只用 templates/config.yaml.example
2. **run/db-update 必须真实 multica id**: DB 查不到 → 退出码 1(设计行为); 先确认
   `SELECT * FROM t_skills_dev_requirement WHERE multica_issue_id='<uuid>'`
3. **源仓生产分支是 main**(source_master=main), 旧 master 分支是历史遗留(merge-base);
   github 中间分支名就叫 `github`
4. **ai_shell 与源仓同 URL**(只是不同分支) — 改 remotes 时别当成独立仓库
5. **downstream 的 SHA 必须 == github main**(verify_against), 不一致 = 结构/身份重写配置出错
6. **非快进/外来历史**: 看守卫报错 → 确系故意重写才 `--force`(会自动建备份分支), 或把 head 加入 acknowledged_commits
7. **hop1 push 冲突**: 工具自动 fetch+rebase 重试, 5 次仍失败才报 GuardError; 手工排查时看临时 clone 输出
8. **publish 卡在版本冲突**: 正常, 自愈 bump 上限 10 次; 一直冲突说明 manifest 与 ClawHub 状态分裂 → `clawhub skill publish <dir> --dry-run --json` 人工核对
9. **clawhub 未登录**: `clawhub login`(device 流程)一次; `whoami` 应显示 erickeyhu-hug
10. **本技能目录是权威运行目录, 不是 git 仓库** — 别在里面 git init/commit; 归档到
    skillsopr 仓库时(见 skill-repo-archiving)必须**排除 config.yaml 和 data/**(含 DB 密码
    与 clawhub token), 只归档 src/ scripts/ tests/ templates/ references/ pyproject.toml
11. **本机 pip 直连 pypi 会挂** — 依赖已齐(PyYAML); 确需安装用清华镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple`
12. **空仓库不能 git push 建分支**(GitCode 403) — 用 API `POST /api/v5/repos/{owner}/{repo}/branches`, refs 必须指向已存在 commit; 一般 bootstrap 会先探测
13. **远端 backup 分支可删可留**: force 后产生的 backup/pre-sync-* 是安全网, 确认无误后可清理
14. **hop1 判定用 patch-id 不用 tree**: 曾有"测试提交"(b9d15a0)的 tree 与分支另一 commit 巧合相同 → 被误判已同步, 其后的 commit 因 tree 错位被永久误报 pending。`hop1_pending` 已改用 `git patch-id`(相对 first-parent 的净变更)判定, 与 cherry-pick -m 1 语义一致; 排查"永远 pending 但内容实际已同步"时先确认部署版本 ≥1.1.1
15. **publish 413 (Request Entity Too Large)**: 某 skill 目录过大(如 terraform-generator 11M assets/reference)超 ClawHub 上传上限 → 该 skill 发布失败, 报错已带 skill 名; **解决: 把它加进 config.yaml 的 `clawhub.exclude_skills` 黑名单**(工具已过滤, 不再 probe/publish), 或瘦身内容后从黑名单移除
16. **并发安全(v1.2.0)**: map.txt append 与 manifest save 均加了 flock 文件锁(多进程并发不再丢条目/覆盖); `agent-run.sh` 入口有 flock **排队锁**(后到者等待 TIMEOUT_SEC 秒而非直接失败 —— 因为 run 幂等, 前一个完成后后一个拿锁继续, 两个任务各自成功; 可用 `LOCK_WAIT_SEC` 覆盖等待时长; 等待超时才 exit 2); publish 各 skill 并行(默认 3 workers, 环境变量 `GITSYNC_PUBLISH_WORKERS` 可调, ClawHub 限流敏感); db-update 各 target 并行(每线程独立 MySQL 连接, 行锁+幂等重放保证同一行并发安全)
17. **目标开关(需求2)**: `targets[].enabled: false` 可禁用某个同步目标, 禁用后不参与 sync/check/publish/DB 回写; 改配置即生效, 无需删条目
18. **发布黑名单(需求3)**: `clawhub.exclude_skills` 支持精确名与 glob(如 `terraform-*`); 黑名单 skill 不 probe 不发布不进 manifest, 修好后从列表移除即恢复

## Verification Checklist

- [ ] `check --json` → `in_sync: true`, hop1_pending=0
- [ ] `lookup <任意端 head> --json` → master/branch/remote/targets 全链路解析
- [ ] 对拍远端: github main SHA == 下游 master SHA == lookup 的 remote; ai_shell SHA == github 分支 SHA
      (`git ls-remote --heads git@github.com:huaweicloud-dev/huaweicloud-skills-dev.git` 等)
- [ ] products/latest.json 中目标 skill 的 `platforms` 含期望平台
- [ ] DB: `SELECT platform_github_status,... FROM t_skills_dev_flow WHERE requirement_id=<id>` 为 published
- [ ] 归档副本完整性: `PYTHONPATH=src python3 -m pytest tests/ -q` → 49/50(1 个为仓库既有断言问题)
