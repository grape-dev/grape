# Plan: Issue #1070

## 设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 跨技能引用校验时机 | 物化期排除、运行时按需获取 | 跨技能引用本就不在包内，强校验必然误判 |
| 前缀判定口径 | 前缀最后一段目录名 ≠ 本技能目录名 → 跳过 | 覆盖 `other-skill/references/..` 与 `docs/references/..`；前缀==自身目录视为自有（repo 视角写法） |
| 数据修复方式 | 幂等一次性脚本（DDL 特征 + content_hash 为空双条件） | 582 行规模一致，条件收敛防误伤正常行 |

## 实现
1. `src/runtime/skill_materialize.py`：
   - 新增 `_extract_cross_skill_prefix`；
   - `parse_skill_md_references(skill_md_path, skill_name="")`：跨技能前缀引用跳过并计数，一次性 INFO 日志（进程级去重集合）；`skill_name` 缺省取 SKILL.md 所在目录名（与物化 tmp 目录名一致，调用方无需改签名）。
   - `validate_skill_dir` 判败逻辑不变。
2. `scripts/fix_skill_content_hash_package.py`：扫描→清洗（可经 OBS 对象重算 sha256 回写）→重建 index.json（备份 .bak-1070）→终检。
3. `scripts/scan_skill_cross_skill_refs.py`：全量扫描 skills_root，输出跨技能引用清单与自有内容缺失清单。
4. 测试：`tests/test_issue1070_skill_cross_skill_refs.py`（解析排除/一次性日志/zip+files 物化成功/缺自有文件判败/前缀提取）。
