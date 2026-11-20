# Tasks: Issue #2153

- [x] 需求/方案文档（spec.md / plan.md）
- [x] `tests/integration/conftest.py`：独立 tmp_path SQLite、TestClient、固定身份/token、VCS adapter mock
- [x] `tests/integration/test_org_pipeline_e2e.py`：happy path + partial_failed + 幂等 + cancel
- [x] `pytest.ini`：注册 `integration` marker，默认回归排除
- [x] CI（`.github/workflows/windows-smoke.yml`）纳入 `pytest -m integration`
- [x] 本地验证 `pytest -m integration` 全绿 + 现有套件冒烟不受影响
