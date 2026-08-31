# skills_test

技能测试工具目录。

## 包含工具

| 目录/文件 | 说明 |
|-----------|------|
| `huawei-cloud-skill-basictest/` | 华为云 Skill 基本功评测（七阶段流程，无人值守） |
| `huawei-cloud-skill-tester/` | 评测执行引擎（端到端流水线 Phase 0-7） |
| `deploy/deploy-skills-test.sh` | 一键部署脚本（部署到任意 multica 工作区） |
| `deploy/agent_test_instructions.md` | 专业功能测试 agent 指令模板 |
| `deploy/agent_verify_instructions.md` | 评测机制报告 agent 指令模板 |

## 一键部署

```bash
cd tools/skills_test/deploy
bash deploy-skills-test.sh                        # 部署到当前工作区
bash deploy-skills-test.sh --workspace <id|名称>  # 部署到指定工作区
bash deploy-skills-test.sh --dry-run              # 预览将执行的动作
```

部署内容：两个 skill + 专业功能测试 agent + 评测机制报告 agent + 评测小队 squad，
并交互式确认全部环境变量（GITCODE_TOKEN / 华为云 AK-SK / 测试数据库）。
