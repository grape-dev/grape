# 测试用例匹配规则

## 目标

找到覆盖变更符号的测试用例，评估测试兜底情况。

## 三层叠加策略

对每个变更符号，依次用三层策略找匹配测试，取并集，每条匹配标注证据来源。

### 第一层：命名约定

按语言匹配测试文件名/测试函数名：

| 语言 | 测试文件命名 | 测试函数命名 |
|------|------------|------------|
| Python pytest | `test_*.py` / `*_test.py` | `def test_<fn>()` / `def test_<class>()` |
| Java JUnit | `*Test.java` / `*Tests.java` / `Test*.java` | `@Test void <method>()` / `void test<Method>()` |
| JS/TS Vitest/Jest | `*.spec.{js,ts}` / `*.test.{js,ts}` | `it('<name>')` / `test('<name>')` / `describe('<name>')` |
| Vue | `*.spec.vue` / 组件同目录 `__tests__/` | 同 JS |

匹配规则：
- 变更符号 `run_pipeline` → 找 `test_run_pipeline` 函数（精确匹配优先）
- 变更符号 `EventPipeline` 类 → 找 `test_event_pipeline.py` 或 `EventPipelineTest.java`
- 模糊匹配：`test_run_pipeline_*`、`test_*run_pipeline*`（降权，标注 `fuzzy`）

证据来源标注：`naming`。

### 第二层：测试体引用

测试文件内 import/调用被测符号：

1. 用 GitCode code search 或 `codegraph_explore --query "import {symbol}"` 找引用文件
2. 过滤出位于测试目录的文件（`tests/`、`test/`、`__tests__/`、`src/test/`、`*Test.java` 同包）
3. LLM 读测试文件，判断是否真的调用/断言变更符号

匹配判定：
- `from src.api.event_pipeline import run_pipeline` + 测试体内调用 `run_pipeline(...)` → 匹配
- 仅 import 未调用 → 降权（标注 `import_only`）
- 通过 fixture/工厂间接调用 → 标注 `indirect`，仍算匹配

证据来源标注：`reference` + 引用位置（文件:行号）。

### 第三层：codegraph 反向边

用 codegraph 查测试 → 被测的调用边：

```bash
codegraph_callers --symbol "run_pipeline" --depth 1
```

在结果中筛选位于测试目录/测试文件的调用方：

- 调用方文件路径含 `tests/`/`test/`/`__tests__/` 或匹配测试文件命名 → 测试用例
- 调用方符号以 `test_` 开头或带 `@Test` → 测试用例

证据来源标注：`codegraph_edge` + 边信息。

## 并集与去重

三层结果取并集，按 `(test_file, test_symbol)` 去重：

- 同一测试被多层匹配：合并证据来源，如 `naming + reference + codegraph_edge`
- 优先级：`codegraph_edge` > `reference` > `naming`（前者证据更强）

## 匹配质量评级

| 证据来源 | 质量 |
|---------|------|
| `codegraph_edge` | 高（结构化边） |
| `reference` | 高（直接引用） |
| `naming` 精确 | 中（命名约定） |
| `naming` 模糊 | 低（可能误匹配） |
| `import_only` | 低（未实际调用） |

模糊匹配需用第二层二次确认，否则标注 `low_confidence`。

## 测试覆盖率计算

```
覆盖率 = 有匹配测试的变更符号数 / 变更符号总数
```

按符号级统计，不按行级。未匹配测试的符号列入"无测试覆盖"清单。

## 输出结构

```json
{
  "tests": [
    {
      "test_file": "tests/test_event_pipeline.py",
      "test_symbol": "test_run_pipeline",
      "covers": "run_pipeline",
      "evidence_source": "naming + reference + codegraph_edge",
      "evidence_detail": "命名匹配 + tests/test_event_pipeline.py:12 import + codegraph 边",
      "quality": "high"
    },
    {
      "test_file": "tests/test_runtime.py",
      "test_symbol": "test_dispatch",
      "covers": "scheduler.dispatch",
      "evidence_source": "naming",
      "evidence_detail": "模糊命名匹配 test_dispatch ~ dispatch",
      "quality": "low",
      "low_confidence": true
    }
  ],
  "uncovered_symbols": ["scheduler.dispatch", "worker.claim"],
  "coverage_rate": 0.6
}
```

## 边界情况

- **无测试目录**：仓库无 `tests/`/`test/` → 覆盖率 0，标注 `no_tests_dir`
- **测试文件本身变更**：变更符号在测试文件内 → 不匹配（测试不是被测对象）
- **集成测试**：`*IntegrationTest`/`e2e/`/`playwright` 匹配降权（运行成本高，不算单元兜底）
- **多语言混合**：Python 变更只找 Python 测试，不跨语言匹配（Java 测试不覆盖 Python 符号）