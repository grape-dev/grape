# 调用链追溯策略

## 目标

对每个变更符号，向上追溯调用方（谁会受影响），输出受影响的符号集合。

## 优先级：codegraph → LLM 兜底

### 第一优先：codegraph 产物

#### 1. 读 project-graph.md

grape 的 `src/project_info/builder.py` 已为托管仓库生成 `project-info/project-graph.md`（项目知识基线）。检查该文件是否存在：

```bash
GET /repos/{owner}/{repo}/contents/project-info/project-graph.md?ref={branch}
```

若存在，解析其中的调用关系段，查找变更符号的调用方。

#### 2. 调 codegraph CLI

若 project-graph.md 不含细粒度调用边，直接调 codegraph CLI：

**影响范围分析（推荐，一步到位）**：
```bash
codegraph_impact --target "src/api/event_pipeline.py:run_pipeline" --depth 2
```
或按符号名：
```bash
codegraph_impact --target "run_pipeline" --depth 2
```

**向上追溯调用方（精细控制）**：
```bash
codegraph_callers --symbol "run_pipeline" --depth 2
```

输出结构化调用边：`{caller, callee, file, line, hops}`。

#### 3. codegraph 索引前置检查

codegraph 命令依赖 `.codegraph/` 索引。检查索引是否存在：
- 若仓库已由 grape builder 初始化过（project-graph.md 存在），索引大概率可用
- 若无索引，尝试 `codegraph init`（耗时 1-3 分钟，大仓库可能超时，超时则降级 LLM）
- 索引损坏：删除 `.codegraph/` 重建（见 `codegraph-commands.md` 异常处理）

### 第二优先：LLM 兜底

当 codegraph 不可用（无索引/查询失败/超时/符号找不到）时，LLM 推理：

1. 用 `codegraph_explore --query "调用 {symbol} 的代码"` 或 GitCode code search 找候选调用文件
2. LLM 读候选文件，判断是否真的调用变更符号
3. **必须标注 `observed=false`**，证据写 `LLM 推断 + 候选文件路径`

LLM 兜底 prompt 模板：
```
已知变更符号：{symbol}（{file}:{lines}）
候选调用文件：{file1}, {file2}, ...
请判断这些文件中哪些调用了 {symbol}，给出调用位置（文件:行号）。
仅报告有明确调用证据的，不要推断"可能调用"。
```

## 多跳传播

默认深度 2 跳（可配置）：

- **1 跳**：直接调用变更符号的函数
- **2 跳**：调用"1 跳函数"的函数
- **3 跳及以上**：一般不追溯（噪声大于信号），除非变更触及核心入口

每跳标注证据：
- 1 跳：codegraph 边或 LLM 观察
- 2 跳：同上，但标注 `via={1跳符号}`

## 入口点识别

标记受影响符号是否为公共入口点（影响面权重更高）：

| 语言 | 入口点模式 |
|------|-----------|
| Python FastAPI | `@app.get/.post/...`、`@router.get/.post/...`、`def main`、`if __name__ == "__main__"` |
| Python CLI | `argparse`/`click` 命令函数、`def main` |
| Java Spring | `@RestController`/`@RequestMapping`/`@GetMapping`/`@PostMapping`、`public static void main` |
| Java JUnit | `@Test`（测试入口，降权） |
| Vue | 路由配置 `routes`、`<router-view>` 引用的组件、`App.vue` |
| Node.js | `module.exports` 顶层、`express().get/.post` |

入口点判定证据：读符号所在文件的装饰器/注解/路由配置，标注 `is_entrypoint=true` + 证据位置。

## 跨语言断点

grape 是 Python（主服务）+ Java（Admin）+ Vue（前端）多语言。跨语言调用链 codegraph 可能不覆盖：

- Java Admin 调 Python API（HTTP）：codegraph 不追踪 HTTP 调用
- Vue 前端调后端 API（HTTP）：同上

处理：
- 检测到跨语言调用（API URL 字符串匹配）时，标注 `cross_language=true` + `observed=unobserved`
- 不强行追溯，在报告中提示"跨语言调用需人工确认"

## 输出结构

```json
{
  "impacted": [
    {
      "symbol": "webhook.handle",
      "file": "src/api/routes/webhook.py",
      "via": "run_pipeline",
      "hops": 1,
      "evidence": "codegraph_callers 边: webhook.py:11",
      "observed": true,
      "is_entrypoint": true,
      "entrypoint_evidence": "@router.post /webhook/gitcode"
    },
    {
      "symbol": "scheduler.dispatch",
      "file": "src/runtime/service.py",
      "via": "webhook.handle",
      "hops": 2,
      "evidence": "LLM 推断 + service.py:45",
      "observed": false,
      "is_entrypoint": false
    }
  ]
}
```

## 性能限制

- 单 PR 变更符号 >50 个时，仅追溯签名变更的符号 + 入口点符号，其余只记录变更面
- codegraph 查询总时长 >30s 时，剩余符号降级 LLM 或跳过
- LLM 兜底候选文件 >20 个时，仅读前 20 个（按 GitCode code search 排序）