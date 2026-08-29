# 官方技能目录分类方案（Skill Directory Categorization Plan）

> 版本：v1.0
> 状态：方案（未实施）
> 范围：`skills/` 目录下官方技能重分类，对齐 `.grape/skills` 的组织方式
> 约束：仅一级 agent_type 分类目录，skill 直接平铺其下；暂不改动代码

## 1. 背景与目标

当前 `skills/` 目录中官方技能的组织方式不统一：

- 24 个技能平铺在 `skills/` 根目录；
- 部分技能按业务域分组（`iac/`、`media-operate/`、`os/`）嵌套存放；
- `custom/`、`repo/`、`_template/` 属于运行时目录，不属于官方技能。

参考 `.grape/skills` 的既有组织方式（`<agent_type>/<skill_name>/SKILL.md`，例如
`coding/implementation/`、`issue/issue-advance/`、`sre/cloud-deploy/`），
将 `skills/` 重分类为 **一层 agent_type 目录 + 平铺 skill** 的结构。

## 2. 分类标准

以 `configs/skill-description.json` 中每个官方技能的 `agent_type` 字段为唯一分类依据。

| agent_type | 含义（agent-description.json） |
|---|---|
| `vod` | VOD Agent：开发者声音分析 |
| `issue` | Issue Agent：Issue 运营 |
| `coding` | Coding Agent：代码实现 |
| `qa` | QA Agent：审查验证 |
| `sre` | SRE Agent：构建部署/运维 |
| `chat` | 对话/IM 通知（不在 5 个 Agent 枚举内，见 §5） |

## 3. 目标结构

```
skills/
├── vod/<skill>/...
├── issue/<skill>/...
├── coding/<skill>/...
├── qa/<skill>/...
├── sre/<skill>/...
├── chat/<skill>/...        # ✅ 已决策：新建，见 §5.1
├── custom/                 # 运行时：用户技能（按 user_id），保持不动
├── repo/                   # 运行时：仓库技能，保持不动
└── _template/              # 模板，保持不动
```

要点：

1. 仅引入一层 agent_type 目录；
2. `iac/`、`media-operate/`、`os/` 等业务域分组目录拆散，其下技能按 `agent_type`
   直接平铺到对应目录，不保留二级业务域子目录；
3. 每个技能目录内容（SKILL.md + references/ + scripts/ 等）整体随迁，内部结构不变。

## 4. 全量技能归类清单（37 个官方技能）

### vod（5）
| skill | 当前路径 |
|---|---|
| vod | `vod/` |
| repo-onboarding | `repo-onboarding/` |
| event-dialogue | `event-dialogue/` |
| blog-vod | `media-operate/blog-vod` |
| iac-vod | `iac/iac-vod` |

### issue（5）
| skill | 当前路径 |
|---|---|
| issue | `issue/` |
| issue-advance | `issue-advance/` |
| skill-issue | `skill-issue/` |
| blog-issue | `media-operate/blog-issue` |
| iac-issue | `iac/iac-issue` |

### coding（7）
| skill | 当前路径 |
|---|---|
| implementation | `implementation/` |
| skill-creator | `skill-creator/` |
| rest-api-skill-creator | `rest-api-skill-creator/` |
| iac-coding | `iac/iac-coding` |
| wan-video-generation | `media-operate/wan-video-generation` |
| qwen-image-generation | `media-operate/qwen-image-generation` |
| ai-blog-generation | `media-operate/ai-blog-generation` |

### qa（6）
| skill | 当前路径 |
|---|---|
| qa | `qa/` |
| pr | `pr/` |
| skill-pr | `skill-pr/` |
| skill-tester | `skill-tester/` |
| rest-api-skill-tester | `rest-api-skill-tester/` |
| iac-qa | `iac/iac-qa` |

### sre（12）
| skill | 当前路径 |
|---|---|
| cloud-deploy | `cloud-deploy/` |
| build-debug | `build-debug/` |
| deploy-guard | `deploy-guard/` |
| changelog-gen | `changelog-gen/` |
| incident-triage | `incident-triage/` |
| skill-publish | `skill-publish/` |
| huawei-cloud | `huawei-cloud/` |
| runtime-install | `runtime-install/` |
| blog-publish | `media-operate/blog-publish` |
| blog-manager | `media-operate/blog-manager` |
| iac-sre | `iac/iac-sre` |
| os-disk-list | `os/os-disk-list` |

### chat（2）✅ 已决策
| skill | 当前路径 |
|---|---|
| chat | `chat/` |
| im-notify | `im-notify/` |

## 5. 边界与待决策项

1. **chat 目录**：`chat`/`im-notify` 的 `agent_type=chat`，但该类型不在
   `agent-description.json` 的 5 个 Agent 枚举中，`.grape/skills` 也无 chat 目录。
   ✅ **已决策：方案 A** —— 新建 `chat/` 目录纳入这两个技能。
2. **业务域信息丢失**：拆散 `iac/`、`media-operate/`、`os/` 后，业务域归属仅能从
   skill 名称后缀识别（`iac-*`、`blog-*`、`os-*`），文件系统层面不再保留域层级。
3. **`.grape/skills` 是否同步**：`.grape/skills` 为本地/local 目录（非 git 跟踪）。
   ✅ **已决策：无需同步** —— 保持现状，与代码库 `skills/` 结构脱钩。

4. **skill-description.json 去留评估（结论：保留）**：目录分类后该配置文件**仍需保留**，
   但职责可简化为"官方技能注册表"，而非分类依据。理由：
   - 它承担的不只是分类：是 **DB `skill` 表（scope='official'）的种子/同步源**
     （`db_setup.py:_sync_official_skills`、`POST /api/skills/sync-official`），为每个技能提供
     `agent_type`、`path`、`display_name`、`description` 四个字段。其中后两者
     **无法从目录结构**获得——SKILL.md frontmatter 虽有 `name`/`description`，但与配置中的
     中文精选版**已知不一致**（见 `skill-description-sync.md`），且无 `agent_type` 字段。
   - 运行时依赖的是同步机制而非文件布局：前端官方技能页签每次加载前调用
     `/skills/sync-official`（`skills.js:16-22`）从 JSON 增量同步；前端列表、agent 路由、
     `agent-skill-mapping` 均读 DB 而非扫目录；测试 `test_qa_skill.py:70`、
     `test_vod_skill.py:56` 直接读取该文件。
   - 若彻底移除需改造 `db_setup.py`、`routes/skills.py` 的 seed/init/sync 为
     "扫目录 + 解析 frontmatter"，并约定 agent_type=父目录名、补齐 frontmatter 规范——
     改动面大且产生新一致性风险。
   - **结论**：目录负责文件的物理组织，配置文件保留为官方技能的权威注册表（DB 种子源、
     元信息权威点）。目录分类后可删的是 `path` 字段的冗余信息（可选保留用于一致性校验）；
     `agent_type`、`display_name`、`description` 仍需由其提供。

## 6. 代码/配置联动影响（实施时需同步修改，本次不做）

| 位置 | 影响 |
|---|---|
| `configs/skill-description.json` | 全部 37 条 `path` 字段需更新为新相对路径 |
| `src/api/db_setup.py:602/665` | 从 skill-description.json 的 `path` 刷入 DB `skill_path`，随配置自动更新 |
| `src/user/db.py:751` | 官方技能路径拼接 `SKILLS_DIR/<sp>/SKILL.md`（若 `skill_path` 已含新路径则无需改） |
| `src/prompt_builder.py` | `get_skill_capabilities()` 遍历 `SKILLS_DIR` 顶层目录；`chat`/`event-dialogue` 硬编码引用 |
| `src/api/routes/chat.py:723`、`src/api/routes/events.py:605` | `SKILLS_DIR/chat/SKILL.md`、`SKILLS_DIR/event-dialogue/SKILL.md` 硬编码路径 |
| `src/runtime/service.py`、`src/api/onboarding.py` | 按 skill 名拼 `SKILLS_DIR/<name>/SKILL.md` 的兜底逻辑 |

> 说明：DB `skill_path` 来自 skill-description.json 的 `path` 字段，因此只要配置文件
> 同步更新，`get_skill_path` 等读 DB 的逻辑即可正确解析；但 `prompt_builder.py`、
> `chat.py`、`events.py` 中 **硬编码** 的 `SKILLS_DIR/<name>/SKILL.md` 需要人工改为
> `SKILLS_DIR/<agent_type>/<name>/SKILL.md`（或在配置中补一层映射）。

## 7. 实施前检查清单（备忘）

- [x] chat 归类决策：新建 `chat/` 目录（§5.1 方案 A）
- [x] `.grape/skills` 不同步（§5.3）
- [ ] 列出全部需移动的目录与目标路径（可依据 §4 生成移动清单）
- [ ] 更新 `configs/skill-description.json` 的 `path`
- [ ] 更新硬编码 skill 路径的代码引用（§6）
- [ ] 验证 37 个 skill 目录 `SKILL.md` 均存在且路径可解析
- [ ] 运行 `db_setup` 重新 seed 官方技能，确认 `skill_path` 正确