# Issue #870 任务拆解（task）

1. [x] 新增 `src/im/engine/metrics.py`：EngineMetrics / CountingSender / snapshot
2. [x] Supervisor `_run` 挂接重连与异常分类计数（metrics 可选注入，向后兼容）
3. [x] ImEngine.health() 补 5 字段（缺失给 0）；`_feishu_factory` 包 inbound_handler 与 sender
4. [x] 测试：两态字段集合一致 / 收发计数 +1 / 失败 +1 / 重连 reconnects+1 / flap 累计
5. [x] `pytest tests/im` 全绿 + 全量相关自审查
