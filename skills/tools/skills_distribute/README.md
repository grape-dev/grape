# skills_distribute

技能分发工具目录：GitCode → GitHub / ClawHub / DB 代码同步与 skill 跨平台分发相关的技能与脚本。

## 目录内容

| 路径 | 说明 |
|---|---|
| `gitsync-platform-sync/` | GitCode 源仓按 hop1+hop2 两跳增量镜像到 GitHub / GitCode 内部分支(ai_shell) / 下游仓库，发布 ClawHub，并按 multica issue 回写 DB。幂等可重跑。**含完整工具链源码，可直接运行** |

## gitsync-platform-sync 结构（Hermes skill 规范）

```
gitsync-platform-sync/
├── SKILL.md              技能说明（frontmatter 规范 + Overview/When to Use/命令/护栏/Pitfalls/Verification）
├── pyproject.toml        打包定义（pip install -e . 后可直接用 gitsync 命令）
├── src/gitsync/          25 个 Python 模块（同步引擎/守卫/身份重写/ClawHub/DB 回写）
├── scripts/
│   └── agent-run.sh      agent 一键脚本（自动装依赖/验证前置/超时控制/写 run-result.json）
├── templates/
│   └── config.yaml.example   配置模板（占位符，复制为 config.yaml 后填写真实值）
├── references/
│   ├── agent-run-guide.md    脚本使用说明（参数/退出码/subprocess 调用示例）
│   └── toolchain-readme.md   工具链架构文档
└── tests/                12 个测试文件（49/50 通过，1 个为仓库既有断言问题）
```

## 使用要点

- 运行：`cd gitsync-platform-sync && PYTHONPATH=src python3 -m gitsync.cli --config config.yaml <command>`
- 一键执行: `bash scripts/agent-run.sh --multica-issue <uuid>`
- 首次使用：复制 `templates/config.yaml.example` 为 `config.yaml`，填入真实仓库/DB 配置（config.yaml 含 DB 密码，禁止入库）
- 验证归档副本完整性：`PYTHONPATH=src python3 -m pytest tests/ -q` → 49/50
- 与 `skills_gitcode/gitcode-event-handler` 的区别：后者处理 webhook 事件（Issue/PR/分发），本目录处理代码同步与发布回写
