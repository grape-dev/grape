# Plan: Issue #865

## 模块改动
| # | 文件 | 改动 |
|---|------|------|
| 1 | src/runtime/engine_model.py（新增） | 共享配置解析：detect_engine_model(name)；hermes ~/.hermes/config.yaml model.default；opencode ~/.config/opencode/opencode.json model；codearts CODEARTS_MODEL 优先 → ~/.codeartsdoer/codearts_cli.json model。pyyaml 缺失时正则兜底；任何异常返回 ""（仅模型名，不读 base_url/api_key） |
| 2 | src/api/routes/runtime.py | RuntimeInfo 增 model: str = ""；register handler 写 model_name/model_source='worker_report'/model_reported_at（空值不覆盖既有值，同 #658 role_tags 模式） |
| 3 | src/api/db_setup.py | agent_runtimes DDL 增 model_name/model_source/model_reported_at；幂等迁移（SQLite/MySQL，同 #658）+ legacy 冻结清单附手工 ALTER |
| 4 | src/runtime/agent.py | _discover_local_runtimes 输出增 model=detect_engine_model(name) |
| 5 | src/user/db.py | get_agent_runtimes SELECT/_rows_to_runtimes 增三列 |
| 6 | src/api/routes/settings.py | _recent_models_by_runtime：单条 GROUP BY engine_id（IN 占位符）取 top3（count desc, last_used desc）；列表（页切片/全量）与详情 attach；本机引擎 cid=0 且 model_name 空时服务端 detect_engine_model 兜底（model_source='local_config'） |
| 7 | src/api/static/js/pages/runtimes.js + locales/zh,en runtimes.json | 卡片「模型」行（配置模型 → 最近模型 → —，title=model_source）；详情弹窗「配置模型」行 + 「最近使用模型」top3 块（模型名 + 次数 + 最后使用时间）；i18n 中英文 |

## 测试（tests/test_engine_model_issue865.py）
- detect_engine_model 三引擎 tmp HOME 解析正确；解析失败/缺文件 → ""（不影响注册）
- /api/runtime/register 带 model：DB 落 model_name/model_source；空 model 不覆盖已有值
- _recent_models_by_runtime：单条 SQL（execute 调用计数=1，无 N+1）、top3 排序、空 ids 返回空
- i18n zh/en key 一致

## 回归
pytest tests/（现有套件全过）
