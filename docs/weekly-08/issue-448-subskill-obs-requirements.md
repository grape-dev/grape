# Issue #448 需求文档：子技能端点支持 OBS 存储的 user/repo 技能（references/ zip 提取）

- Issue: https://gitcode.com/grape-dev/grape/issues/448
- 分支：`fix/issue-448-subskill-obs-extract`（基线 `origin/weekly-08` @ 5456fb041）
- 状态：已实现（PR #652，待评审合并）
- 类型：bug 修复（#444 全链路扫描 backlog F2）

## 1. 背景与问题

子技能三端点只读本地 `SKILLS_DIR` 磁盘目录：

- `GET /api/skills/{skill_id}/sub`（列表）
- `GET /api/skills/{skill_id}/sub/{sub_name}`（详情）
- `GET /api/skills/package-sub/{pkg_id}/{sub_name}`（包详情，同 detail 形状）

user/repo 技能自 v2.4 起存 OBS 对象（`file_path` 为对象键，本地无目录）→
列表返回**空列表假象**（200 + `sub_skills: []`，Issue 原文写"一律 404"仅对两个详情端点成立），
详情 404。来源：Issue #444 backlog F2（`issue-444-skill-cache-sync-requirements.md` §4.4 / UC6-3）。

## 2. 需求项

| 编号 | 需求 | 来源 |
|---|---|---|
| R32 | 三端点在本地 references/ 未命中时从 OBS zip 按 entry 提取子技能（列表含 frontmatter 解析与文件计数；详情含二级 references 顶层附件，各截 2000 字符） | Issue 验收 1 |
| R33 | official 技能本地目录优先，行为零改动（三端点全覆盖，含"官方技能转集群后有对象键"的顺序保证） | Issue 验收 2 |
| R34 | 复用既有两级缓存：zip 磁盘缓存命中不回源 OBS（storage.read_bytes 计数不增） | Issue 验收 3 |
| R35 | 有对象键但存储未启用 → 503（对齐 detail/convert 端点口径）；无对象键（历史数据未迁移）→ 沿用既有 404/空列表口径，不引入新失败面 | Spec 口径 |

### 不在范围

- 不改 official 技能同步/双机目录逻辑；不改 zip 上传规范化（`_normalize_skill_zip`）。
- 不做子技能独立内存缓存（复用 `zip_cache`，失效链路 `invalidate_skill_caches` 不扩）。
- 不修前端页面；不修 locales error_code 键嵌套问题（已知独立 bug，另单）。
- 不做 `references/{sub}` 目录名与 skill_name 不一致时的兜底（对齐 `extract_skill_md` 常规技能精确匹配语义）。

## 3. 场景与场景用例（5 个场景 / 11 个用例）

本节 5 个场景的角色：S1 核心修复（修复本身）、S2 兼容/回归（防回归守卫——验证当前正确的机制不被破坏）、S3 性能口径（缓存行为记录）、S4 降级边界（历史数据/存储未启用的口径确认）、S5 安全守卫（输入校验）。每个 UC 的可执行测试代码见 §6，UC↔测试映射与状态见 §7。

### S1 核心修复：user/repo 技能从 OBS zip 提取子技能

环境：OBS 启用（FakeStorage），user 技能行 `file_path` 指向对象键，zip 内含 `references/sub1`（带二级 references 附件）与 `references/sub2`。
- UC1 列表：调 `GET /{skill_id}/sub` → 返回 sub1/sub2，frontmatter 的 name/description 正确解析，file_count 按前缀计数（sub1 = SKILL.md + extra.md + nested/deep.md = 3）。
- UC2 详情：调 `GET /{skill_id}/sub/sub1` → 返回 content + 二级 references 顶层文件（`extra.md`），`nested/` 非顶层不读。
- UC3 包详情：调 `GET /package-sub/{pkg_id}/sub1` → 同 detail 形状（type=package-sub，display_name/description 从 frontmatter 解析）。

### S2 兼容/回归（防回归守卫）：official 技能行为不变

环境：OBS 启用，official 技能（skill_name 命中 `resolve_official_skill_path` → `skills/sre/app-release/huawei-cloud/`）**同时挂上对象键**（模拟官方技能转集群），且把 OBS 里的 zip 换成垃圾字节——若误走 zip 分支即失败。
- UC4a 列表本地优先：`list_sub_skills` 返回非空子技能列表，且 `storage.read_bytes` 计数为 0。
- UC4b 详情本地优先：`read_sub_skill` / `package_sub_skill` 读本地 `references/huawei-ecs/SKILL.md`，read_bytes 计数为 0。

### S3 性能口径：缓存命中不回源

环境：同 S1，zip 磁盘缓存（`zip_cache`）随首次读取填充。
- UC5 两次列表只回源一次：连续调两次 `list_sub_skills`，`storage.read_bytes` 计数 == 1。

### S4 降级边界：历史数据与存储未启用

- UC6 有对象键但 storage=None → 503（对齐 detail/convert 端点口径）。
- UC7 历史数据无对象键（`file_path`/`skill_path` 均无对象键特征）→ 沿用空列表口径（不 503、不 404）。

### S5 安全守卫：sub_name 输入

- UC8a read 端点穿越：`sub_name="../{skill_name}"` → 404（entry 精确匹配天然免疫，不读 zip 内主技能 SKILL.md）。
- UC8b package-sub 端点穿越：同上 → 404。
- UC9 zip 内不存在的子技能 → 404。

## 4. 实现要点

- `src/storage/__init__.py` 新增 `list_zip_sub_skills(zip_bytes, skill_name)` 与
  `read_zip_sub_skill(zip_bytes, skill_name, sub_name)`：entry 精确匹配
  `{skill_name}/references/{sub}/SKILL.md`，namelist 全量枚举后比对，免疫 `../` 穿越；
  附件仅读 `{sub}/references/` 顶层文件（对齐本地 `os.listdir` 行为）。
- `src/api/routes/skills.py` 新增 `_get_sub_skill_zip(skill_id, skill_name)` 共用辅助：
  `get_skill_object_key` → 无键返回 None（沿用旧口径）；有键且 storage=None → 503；
  否则 `get_skill_zip_bytes`（磁盘缓存 → OBS 回源）。三端点在本地目录/文件未命中处插入该分支。
- 顺序保证：本地（`resolve_official_skill_path` / SKILLS_DIR 拼接）命中优先，OBS 仅作 fallback——
  official 技能（含转集群后 file_path 有对象键的）行为不变。

## 5. 实测核验

- `tests/test_issue_448_sub_skill_obs.py` 11 用例全绿。
- 家族回归：`test_skills_obs / test_skill_local_storage / test_skill_sync_obs /
  test_issue_406_skill_display_name / test_scene_skill_market` 共 31 failed / 85 passed，
  与基线（git stash 后重跑）**完全一致**——既有失败（含 `test_sub_skill_path.py` 4 例，
  其断言的 `skills/sre/huawei-cloud` 已迁移至 `skills/sre/app-release/huawei-cloud`，路径过期），零新增回归。

## 6. 场景用例代码

### 6.0 共享测试基座（mock 替身 / fixture / seed，节选）

```python
# tests/test_issue_448_sub_skill_obs.py — 测试基座

SKILL = "obs-sub-test-skill"                    # 测试技能名（fixture 自建行、自清理）
OBJECT_KEY = "skills/user/1001/obs-sub-test-skill.zip"

class FakeStorage(StorageBackend):
    """内存 OBS 模拟：read_bytes 带计数（断言缓存命中不回源）。"""
    def __init__(self):
        self.objects = {}                       # 对象键 → bytes
        self.read_count = 0                     # 回源计数（UC4/UC5 断言目标）
    def read_bytes(self, key):
        self.read_count += 1
        return self.objects.get(key)
    # save_bytes/exists/list_keys/delete/temporary_url/copy/... 其余方法省略

def _make_pkg_zip() -> bytes:
    """构造带子技能的技能包 zip：references/sub1、sub2，sub1 带二级 references 附件。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{SKILL}/SKILL.md", f"---\nname: {SKILL}\ndescription: pkg\n---\n\n## 主技能")
        zf.writestr(f"{SKILL}/references/sub1/SKILL.md",
                    "---\nname: 子技能一\ndescription: sub one\n---\n\nsub1 content")
        zf.writestr(f"{SKILL}/references/sub1/references/extra.md", "extra doc")
        zf.writestr(f"{SKILL}/references/sub1/references/nested/deep.md", "not top-level")
        zf.writestr(f"{SKILL}/references/sub2/SKILL.md",
                    "---\nname: sub-two\ndescription: sub two\n---\n\nsub2 content")
    return buf.getvalue()

def _insert_skill(skill_name, scope, owner_id, skill_path, file_path):
    """插入测试技能行，返回 id（共享测试 SQLite，fixture 前后 DELETE 自清理）。"""
    with db_lock:
        db.execute("INSERT INTO skill (skill_name, display_name, description, agent_type,"
                   " skill_path, file_path, scope, owner_id, is_active)"
                   " VALUES (?,?,?,?,?,?,?,?,1)",
                   (skill_name, "", "", "", skill_path, file_path, scope, owner_id))
        db.commit()
        row = db.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=?",
                         (skill_name, scope, owner_id)).fetchone()
    return row[0]

@pytest.fixture()
def obs_env(monkeypatch):
    """FakeStorage + 干净的测试技能行 + zip 磁盘缓存失效（按对象键隔离）。"""
    # DELETE 测试行（含 official 哨兵 owner_id='issue-448-test'，避开真实 official 行）
    zip_cache_invalidate(OBJECT_KEY)
    fake = FakeStorage()
    fake.objects[OBJECT_KEY] = _make_pkg_zip()
    monkeypatch.setattr("src.storage.get_skill_storage", lambda: fake)  # 替换技能域存储
    yield fake
    # teardown：DELETE 测试行 + 失效 zip 缓存（零残留）
```

Mock 策略：DB 用共享测试 SQLite（`DEVOPS_DB_PATH=data/test_sessions.db`），每个用例自建技能行、fixture 前后 DELETE 清理；存储后端用内存 `FakeStorage`（继承 `StorageBackend`，带 `read_count` 计数用于回源断言），不触真实 OBS；zip 磁盘缓存在 fixture 中按对象键 `zip_cache_invalidate` 隔离，防跨用例污染。端点为同步函数，直接调用并传 `user={...}` 满足 `Depends` 默认值。UC4 的 official 行用哨兵 `owner_id='issue-448-test'` 绕开 `UNIQUE(skill_name, scope, owner_id)` 与真实 official 行冲突。

### 6.1 S1 核心修复

```python
def test_list_sub_skills_from_obs(obs_env):
    """UC1：user 技能（OBS 存储）子技能列表返回 sub1/sub2，含 frontmatter 解析与文件计数。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    result = list_sub_skills(sid, user={"id": "1001"})
    names = [s["name"] for s in result["sub_skills"]]
    assert names == ["sub1", "sub2"]                      # 两个子技能均可见（修复前为空列表假象）
    sub1 = result["sub_skills"][0]
    assert sub1["display_name"] == "子技能一"              # frontmatter name 解析
    assert sub1["description"] == "sub one"                # frontmatter description 解析
    assert sub1["file_count"] == 3                        # SKILL.md + extra.md + nested/deep.md

def test_read_sub_skill_from_obs(obs_env):
    """UC2：user 技能子技能详情返回 content + 二级 references 顶层文件（各截 2000 字符）。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    result = read_sub_skill(sid, "sub1", user={"id": "1001"})
    assert "sub1 content" in result["content"]            # 修复前此处 404
    refs = {r["name"]: r["content"] for r in result["references"]}
    assert refs == {"extra.md": "extra doc"}              # 只读二级 references 顶层文件

def test_package_sub_from_obs(obs_env):
    """UC3：package-sub 端点返回与 detail 同构结果（type=package-sub）。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    result = package_sub_skill(sid, "sub1", user={"id": "1001"})
    assert result["type"] == "package-sub"                # 形状与本地路径一致
    assert result["display_name"] == "子技能一"
    assert result["description"] == "sub one"
    assert "sub1 content" in result["content"]
```

### 6.2 S2 兼容/回归（防回归守卫）

```python
def test_official_local_dir_priority(obs_env):
    """UC4a：official 技能本地目录优先——有对象键也不走 zip。"""
    obs_env.objects[OBJECT_KEY] = b"not-a-zip"            # zip 换垃圾字节：误走 zip 即失败
    sid = _insert_skill("huawei-cloud", "official", "issue-448-test",
                        "sre/app-release/huawei-cloud", OBJECT_KEY)
    result = list_sub_skills(sid, user={"id": "1"})
    assert result["sub_skills"], "official local sub-skills should be listed"
    assert obs_env.read_count == 0                        # 本地命中绝不回源 OBS

def test_official_read_and_package_sub_local_priority(obs_env):
    """UC4b：read/package-sub 两个详情端点同样本地优先。"""
    obs_env.objects[OBJECT_KEY] = b"not-a-zip"
    sid = _insert_skill("huawei-cloud", "official", "issue-448-test",
                        "sre/app-release/huawei-cloud", OBJECT_KEY)
    result = read_sub_skill(sid, "huawei-ecs", user={"id": "1"})
    assert "description:" in result["content"]            # 读的是本地 references/huawei-ecs/SKILL.md
    pkg = package_sub_skill(sid, "huawei-ecs", user={"id": "1"})
    assert pkg["type"] == "package-sub" and "description:" in pkg["content"]
    assert obs_env.read_count == 0
```

### 6.3 S3 性能口径

```python
def test_zip_cache_hit_no_refetch(obs_env):
    """UC5：磁盘 zip 缓存命中不回源——两次列表只触发一次 storage.read_bytes。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    list_sub_skills(sid, user={"id": "1001"})             # 首次读取 → 回源并写 zip_cache
    list_sub_skills(sid, user={"id": "1001"})             # 二次读取 → 磁盘缓存命中
    assert obs_env.read_count == 1
```

### 6.4 S4 降级边界

```python
def test_storage_disabled_503(monkeypatch):
    """UC6：有对象键但存储未启用 → 503（对齐 detail/convert 端点口径）。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    monkeypatch.setattr("src.storage.get_skill_storage", lambda: None)
    with pytest.raises(HTTPException) as exc:
        list_sub_skills(sid, user={"id": "1001"})
    assert exc.value.status_code == 503

def test_legacy_user_skill_without_object_key_empty(monkeypatch):
    """UC7：历史 user 技能无对象键 → 沿用空列表口径（不 503、不 404）。"""
    sid = _insert_skill("obs-sub-test-legacy", "user", "1001", "some/local/path", "")
    monkeypatch.setattr("src.storage.get_skill_storage", lambda: None)
    result = list_sub_skills(sid, user={"id": "1001"})
    assert result == {"skill_name": "obs-sub-test-legacy", "sub_skills": []}
```

### 6.5 S5 安全守卫

```python
def test_sub_name_traversal_404(obs_env):
    """UC8a：read 端点 sub_name 穿越（../）→ 404，不读 zip 内主技能 SKILL.md。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    with pytest.raises(HTTPException) as exc:
        read_sub_skill(sid, "../" + SKILL, user={"id": "1001"})
    assert exc.value.status_code == 404

def test_package_sub_traversal_404(obs_env):
    """UC8b：package-sub 端点 sub_name 穿越 → 404（entry 精确匹配免疫）。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    with pytest.raises(HTTPException) as exc:
        package_sub_skill(sid, "../" + SKILL, user={"id": "1001"})
    assert exc.value.status_code == 404

def test_sub_not_in_zip_404(obs_env):
    """UC9：zip 内不存在的子技能 → 404。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    with pytest.raises(HTTPException) as exc:
        read_sub_skill(sid, "nope", user={"id": "1001"})
    assert exc.value.status_code == 404
```

## 7. UC↔测试映射与覆盖汇总

| 场景 | UC | 测试函数 | 状态 |
|---|---|---|---|
| S1 核心修复 | UC1 | `test_list_sub_skills_from_obs` | ✅ |
| S1 核心修复 | UC2 | `test_read_sub_skill_from_obs` | ✅ |
| S1 核心修复 | UC3 | `test_package_sub_from_obs` | ✅ |
| S2 兼容/回归 | UC4a | `test_official_local_dir_priority` | ✅ |
| S2 兼容/回归 | UC4b | `test_official_read_and_package_sub_local_priority` | ✅ |
| S3 性能口径 | UC5 | `test_zip_cache_hit_no_refetch` | ✅ |
| S4 降级边界 | UC6 | `test_storage_disabled_503` | ✅ |
| S4 降级边界 | UC7 | `test_legacy_user_skill_without_object_key_empty` | ✅ |
| S5 安全守卫 | UC8a | `test_sub_name_traversal_404` | ✅ |
| S5 安全守卫 | UC8b | `test_package_sub_traversal_404` | ✅ |
| S5 安全守卫 | UC9 | `test_sub_not_in_zip_404` | ✅ |
| 部署验收 | D1 真实 OBS 上传含 references/ 的 user 技能 → 三端点走通 | （目标环境执行，见 Issue 验收清单） | ⏳部署后验收 |
| 部署验收 | D2 双进程缓存一致性（:3000 与 :8080 或独立进程读同一对象键） | （目标环境执行） | ⏳部署后验收 |

说明：D1/D2 属部署类用例，非单测材料——需真实 OBS 与多进程环境，按 Issue 验收清单在目标环境执行；仓库不含验收脚本（验收步骤以清单形式记录，不入库）。

Refs #448
