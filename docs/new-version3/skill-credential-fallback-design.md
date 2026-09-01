# Skill 凭据读取改造方案

## 一、背景

### 问题
1. grape 注入的 AK/SK 环境变量名为 HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK
2. skill-creator 生成的 hcs-* 脚本写死 os.environ.get("HWCLOUD_AK")，变量名不一致
3. creator/tester/publish 只能从环境变量读凭据，缺失时无法回退
4. 之前测试"通过"是因为全部走 mock 模式，从未真实调用过 API

### 解决思路
1. 生成的脚本改为动态扫描环境变量（参考 tools/huawei-cloud-skill-creator 的方式），不写死变量名
2. creator/tester/publish 自己加 JSON 回退——环境变量缺失时从 .project-info/ 下 repo-credentials.json 读取，读到后 export 到环境变量
3. JSON 回退是 grape 流程内能力，不塞到生成的 skill 脚本里

## 二、改动文件清单

| 文件 | 位置 | 改动项数 |
|------|------|---------|
| skill-creator/SKILL.md | /home/yangaiwu/grape/skills/skill-creator/SKILL.md | 3 处 |
| skill-tester/SKILL.md | /home/yangaiwu/grape/skills/skill-tester/SKILL.md | 1 处 |
| skill-publish/SKILL.md | /home/yangaiwu/grape/skills/skill-publish/SKILL.md | 5 处 |

## 三、职责边界

| 角色 | 职责 |
|------|------|
| creator/tester/publish | 从环境变量或 JSON 回退获取凭据，export 到环境变量 |
| 生成的 skill 脚本 | 只从环境变量动态扫描读取 AK/SK，不读 JSON |

## 四、凭据读取统一逻辑（creator/tester/publish 共用）

```
1. 先扫描环境变量：HUAWEI/HW/HWC 开头 + 含 ACCESS_KEY/_AK/SECRET_KEY/_SK 的键值对，动态匹配
2. 环境变量缺失时，递归扫描 .project-info/ 目录，找名为 repo-credentials.json 的文件
3. 找到后从 secrets 字段读凭据，从 config 字段读发布配置
4. 读到后 export 到环境变量（用 HUAWEI 开头的变量名，如 HUAWEI_AK / HUAWEI_SK）
5. 环境变量和 JSON 都缺失，按现有逻辑提示用户/终止
```

## 五、repo-credentials.json 约定

- 文件名固定：repo-credentials.json
- 位置不固定：可在 .project-info/ 根目录或任意子目录下
- 递归扫描 .project-info/ 目录查找
- JSON 结构：
  ```json
  {
    "version": 1,
    "secrets": {
      "CLAWHUB_TOKEN": "...",
      "GITHUB_TOKEN": "...",
      "GITCODE_TOKEN": "...",
      "HUAWEI_AK": "...",
      "HUAWEI_SK": "..."
    },
    "config": {
      "GITHUB_PUBLISH_REPO": "yangaiwu/grape_dev",
      "GITCODE_PUBLISH_REPO": "signway/grape_skill",
      "CLAWHUB_OWNER_HANDLE": ""
    }
  }
  ```

## 六、各文件具体改动

### 6.1 skill-creator/SKILL.md

#### 改动 1：Step 2 技术调研（第 120-123 行）

改前：
```
- AK/SK 扫描方式：自动扫描 HUAWEI/HW/HWC 开头 + 含 ACCESS_KEY/_AK/SECRET_KEY/_SK 的环境变量（如 HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK）
- ⚠️ 变量存在但值为空也算缺失——必须检查值非空
- AK/SK 缺失或为空 → 提示用户提供；不提供 → 终止流程（严禁跳过——不生成文件，保证"生成物必须可执行"）
```

改后：
```
- AK/SK 获取方式：优先扫描 HUAWEI/HW/HWC 开头 + 含 ACCESS_KEY/_AK/SECRET_KEY/_SK 的环境变量，动态匹配（不依赖固定变量名）；环境变量缺失时，递归扫描 .project-info/ 目录找 repo-credentials.json，从 secrets.HUAWEI_AK / secrets.HUAWEI_SK 读取，读到后 export 到环境变量（用 HUAWEI 开头的变量名，如 HUAWEI_AK / HUAWEI_SK）
- ⚠️ 变量/JSON 值存在但值为空也算缺失——必须检查值非空
- AK/SK 均缺失或为空 → 提示用户提供；不提供 → 终止流程（严禁跳过——不生成文件，保证"生成物必须可执行"）
```

#### 改动 2：Step 3 骨架生成——新增"生成的脚本凭据读取规范"（第 151 行后插入）

新增内容：
```
- **生成的脚本凭据读取规范**：生成的脚本不得写死固定环境变量名（如 HWCLOUD_AK），必须使用动态扫描逻辑读取 AK/SK：
  ```python
  def _load_credentials():
      """动态扫描环境变量获取 AK/SK，不依赖固定变量名。"""
      ak, sk = '', ''
      for k, v in os.environ.items():
          u = k.upper()
          if not (u.startswith('HUAWEI') or u.startswith('HW') or u.startswith('HWC')):
              continue
          if 'ACCESS_KEY' in u or u.endswith('_AK') or u == 'AK':
              ak = v or ak
          if 'SECRET_KEY' in u or u.endswith('_SK') or u == 'SK':
              sk = v or sk
      return ak, sk
  ```
  - 环境变量缺失则退出码 3
```

注意：生成的脚本只扫描环境变量，不读 JSON。JSON 回退是 creator/tester/publish 自己的职责。

#### 改动 3：边界情况（第 231 行）

改前：
```
- AK/SK 环境变量缺失或为空（HUAWEI/HW/HWC 开头 + AK/SK 键）→ 提示用户提供；用户不提供 → 终止流程（严禁跳过——不生成文件，保证"生成物必须可执行"不被绕过）
```

改后：
```
- AK/SK 环境变量缺失或为空（HUAWEI/HW/HWC 开头 + AK/SK 键），且 .project-info/ 下 repo-credentials.json 中也无 secrets.HUAWEI_AK / secrets.HUAWEI_SK → 提示用户提供；用户不提供 → 终止流程（严禁跳过——不生成文件，保证"生成物必须可执行"不被绕过）
```

### 6.2 skill-tester/SKILL.md

#### 改动 1：功能验证 AK/SK 检测（第 79-82 行）

改前：
```
- AK/SK 检测：自动扫描 HUAWEI/HW/HWC 开头 + 含 ACCESS_KEY/_AK/SECRET_KEY/_SK 的环境变量（如 HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK）
  - ⚠️ 变量存在但值为空也算缺失——必须检查值非空
  - AK/SK 缺失或为空 → ⛔ 终止测试，输出"AK/SK missing or empty, cannot execute tests"
  - AK/SK 可用（值非空）→ 继续真实 API 验证
```

改后：
```
- AK/SK 检测：动态扫描 HUAWEI/HW/HWC 开头 + 含 ACCESS_KEY/_AK/SECRET_KEY/_SK 的环境变量（不依赖固定变量名，与 skill 脚本扫描逻辑一致）；环境变量缺失时，递归扫描 .project-info/ 目录找 repo-credentials.json，从 secrets.HUAWEI_AK / secrets.HUAWEI_SK 读取，读到后 export 到环境变量（用 HUAWEI 开头的变量名，如 HUAWEI_AK / HUAWEI_SK，确保 skill 脚本扫描能匹配到）
  - ⚠️ 变量/JSON 值存在但值为空也算缺失——必须检查值非空
  - AK/SK 均缺失或为空 → ⛔ 终止测试，输出"AK/SK missing or empty, cannot execute tests"
  - AK/SK 可用（值非空）→ 继续真实 API 验证
```

### 6.3 skill-publish/SKILL.md

#### 改动 1：安全硬约束第 1 条

改前：
```
1. **令牌只读取进程环境变量注入**：$CLAWHUB_TOKEN / $GITHUB_TOKEN / $GITCODE_TOKEN / $CLAWHUB_OWNER_HANDLE
```

改后：
```
1. **令牌获取方式**：优先读环境变量 $CLAWHUB_TOKEN / $GITHUB_TOKEN / $GITCODE_TOKEN / $CLAWHUB_OWNER_HANDLE；环境变量缺失时，递归扫描 .project-info/ 目录找 repo-credentials.json，从 secrets 字段读令牌（CLAWHUB_TOKEN / GITHUB_TOKEN / GITCODE_TOKEN），从 config 字段读发布配置（GITHUB_PUBLISH_REPO / GITCODE_PUBLISH_REPO / CLAWHUB_OWNER_HANDLE），读到后 export 到环境变量
```

#### 改动 2：安全硬约束第 2 条

改前：
```
2. **禁止直连业务数据库**（repo_platform_tokens 等）：token 由上层业务注入环境变量，本技能严禁查询数据库/配置文件/日志/sessions.db 寻找任何 token
```

改后：
```
2. **禁止直连业务数据库**（repo_platform_tokens 等）：token 由上层业务注入环境变量或项目知识库 JSON，本技能严禁查询数据库/配置文件/日志/sessions.db 寻找任何 token
```

#### 改动 3：安全硬约束第 3、5 条

改前：
```
3. **严禁遍历系统目录考古**（/usr/local/lib/hermes-agent、/opt/grape 等）——只读注入的环境变量
...
5. token 绝不写入任何文件、日志、评论、脚本
```

改后：
```
3. **严禁遍历系统目录考古**（/usr/local/lib/hermes-agent、/opt/grape 等）——只读注入的环境变量或项目知识库 repo-credentials.json
...
5. token 绝不写入任何文件、日志、评论、脚本（项目知识库 JSON 由上层业务维护，本技能只读不写）
```

#### 改动 4：硬规则表格

改前：
```
| 令牌只读取进程环境变量注入 | ✅ **必须做** |
```

改后：
```
| 令牌优先读环境变量，缺失时回退 repo-credentials.json | ✅ **必须做** |
```

#### 改动 5：发布流程第 2 步 + 下架流程第 1 步

改前：
```
2. 读取各平台令牌：
   - 环境变量缺失 → 报告「未配置 {平台} 令牌，请到设置页配置」→ 跳过该平台
...
1. 读取各平台令牌；令牌缺失/401/403 → 跳过该平台并输出提示
```

改后：
```
2. 读取各平台令牌（优先环境变量，缺失时回退 .project-info/ 下 repo-credentials.json）：
   - 环境变量和 JSON 均缺失 → 报告「未配置 {平台} 令牌，请到设置页配置」→ 跳过该平台
...
1. 读取各平台令牌（优先环境变量，缺失时回退 .project-info/ 下 repo-credentials.json）；令牌缺失/401/403 → 跳过该平台并输出提示
```

## 七、不涉及的部分

- hcs-* 脚本代码不动（已有 6 个脚本下次重新生成时自然带上新逻辑）
- skill-creator 生成的脚本不内嵌 JSON 读取逻辑（JSON 回退是 grape 流程内能力，不是 skill 的职责）

## 八、已完成

所有改动已实施完毕。

凭据安全检查策略（对齐 tools 方式）：
- 不检查脚本用什么 API 读环境变量（os.environ.get / os.getenv 都允许）
- 硬编码凭据值检查交给 gitleaks 安全扫描（skill-tester 流程第 3 步）
- spec_check.py 只保留 check_no_mock（mock 模式检查）
