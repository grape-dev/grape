# Plan: Issue #955

## 方案
写侧 Pydantic 路由层 400 拦截 + 读侧 id 优先匹配兼容存量，双管齐下；角色全集单一来源
（`engine_select._KNOWN_AGENT_ROLES`，运行时 import 避免循环依赖）。

## 实现路径
1. `scenes.py`：新增 `_validate_scene_node_roles(nodes)`——节点 id（角色名）枚举校验，
   非法值 HTTPException(400)（message 含非法值 + 合法全集）；挂载到 `create_scene`
   （`nodes_json` 组装后）与 `update_scene`（engine 校验前）。
   选择路由层 400 而非 Pydantic validator：全局 RequestValidationError handler 会将
   validator 错误转 422，与验收「非法 400」冲突。
2. `scenes.py` `_validate_scene_engine_ids`：
   - 角色取值改为 id 优先（`node.get("id") or node.get("tag")`，兜底兼容历史仅含 tag 的纯函数调用与 SRE 守卫测试）
   - 能力标签查询去掉 `OR tag='all'` 通配
3. `pipeline_config.py`：
   - `get_scene_engine_binding`：节点匹配 `n.get("id") == agent_type or n.get("tag") == agent_type`
   - `get_node_by_tag`：同源改造（id 优先、tag 兜底）
   - `get_next_node`：本就按 id 匹配，无需改动
4. 测试：`tests/test_issue955_scene_node_role_validation.py`（TDD：先写用例后实现）。

## 风险与缓解
| 风险 | 缓解 |
|------|------|
| 读侧 id 优先改变匹配行为 | 保留 tag 兜底；653/524/658 等既有绑定测试回归通过 |
| 去 `tag='all'` 通配影响存量保存 | 校验目标为节点 id 角色；test_524/653/658 等路由场景回归通过 |
| 角色全集重复定义漂移 | 单一引用 `_KNOWN_AGENT_ROLES`，并有单一来源测试锚定 |

## 验证
- 新增 17 用例全过（非法 400 / 中文 tag 放行 / 绑定恢复 / 合成派发 / 通配堵漏 / SRE 兜底）
- 既有相关测试回归：653 / 250 / sre-relax / 658 / 657 / 524 / 655 / 568 / 253 / 415 /
  227 / 372 / 486 / 544 / event_pipeline / a2a_scene / chat_task / pr_subscription 等
  共 238+ 通过（test_scene_sync 空目录 hash 失败为环境预存问题，与本改动无关，stash 对照验证）
