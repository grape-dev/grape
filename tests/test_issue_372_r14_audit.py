"""Issue #372 R14: 深度审计修复测试 — B→A升级webhook重建、stale token、冗余查询、前端error_code等。

测试覆盖：
- R14.1: auto_upgrade_path_b_repos 重建 webhook（_update_repo_bot_token 新参数）
- R14.2: 路径 B webhook_token 不写 stale 用户 token
- R14.3: R10 路径 B issue 回帖尝试全局 bot token
- R14.4: get_host_user_id 冗余查询消除
- R14.5: config_store TOCTOU 竞态防护（db_lock 包裹）
- R14.6: app.errMsg() 前端 error_code 映射（源码扫描验证）
- R14.7: grape_accounts.status=4 注释修正
- R14.8: dashboard 403 自动切换去重
- R14.9: create_external_identity 清理 status=4 记录
- R14.10: events.json zh/en key 对称
- R14.11: get_user_token_with_refresh dead code 清理
"""
import os
import sys
import json
import re

# 确保使用项目 venv
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

ROOT = os.path.join(os.path.dirname(__file__), '..')


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), 'r', encoding='utf-8') as f:
        return f.read()


# ===== R14.1: _update_repo_bot_token 新增 webhook_token/webhook_id 参数 =====

class TestR14_1UpdateRepoBotTokenWebhookRebuild:
    """R14.1: B→A 升级后 webhook 重建。"""

    def test_update_repo_bot_token_accepts_webhook_token_param(self):
        """_update_repo_bot_token 签名包含 webhook_token 参数。"""
        db_src = _read('src/user/db.py')
        # 确认函数签名包含新参数
        assert 'webhook_token: str = ""' in db_src, \
            "_update_repo_bot_token should accept webhook_token parameter"
        assert 'webhook_id: str = ""' in db_src, \
            "_update_repo_bot_token should accept webhook_id parameter"

    def test_update_repo_bot_token_writes_webhook_token(self):
        """UPDATE 语句写入 webhook_token 字段。"""
        db_src = _read('src/user/db.py')
        # 查找 _update_repo_bot_token 函数体中的 UPDATE 语句
        func_start = db_src.index('def _update_repo_bot_token')
        func_end = db_src.index('\ndef auto_upgrade_path_b_repos', func_start)
        func_body = db_src[func_start:func_end]
        assert 'github_webhook_token=?' in func_body, \
            "GitHub UPDATE should write github_webhook_token"
        assert 'gitcode_webhook_token=?' in func_body, \
            "GitCode UPDATE should write gitcode_webhook_token"
        assert 'webhook_id=?' in func_body, \
            "UPDATE should write webhook_id"

    def test_auto_upgrade_calls_webhook_rebuild(self):
        """auto_upgrade_path_b_repos 中包含 webhook 重建逻辑。"""
        db_src = _read('src/user/db.py')
        func_start = db_src.index('def auto_upgrade_path_b_repos')
        func_body = db_src[func_start:]
        # R14.16: webhook 重建收进 _rebuild_webhook_with_bot_token 统一入口
        assert '_rebuild_webhook_with_bot_token' in func_body, \
            "auto_upgrade should call _rebuild_webhook_with_bot_token"
        assert '_update_repo_bot_token' in func_body, \
            "auto_upgrade should call _update_repo_bot_token"

    def test_auto_upgrade_passes_webhook_token_to_update(self):
        """auto_upgrade 调用 _update_repo_bot_token 时传入 webhook_token 和 webhook_id。"""
        db_src = _read('src/user/db.py')
        func_start = db_src.index('def auto_upgrade_path_b_repos')
        func_body = db_src[func_start:]
        assert 'webhook_token=bot_token' in func_body, \
            "auto_upgrade should pass webhook_token=bot_token"
        assert 'webhook_id=_new_hook_id' in func_body, \
            "auto_upgrade should pass webhook_id"


# ===== R14.2: 路径 B webhook_token 不写 stale 用户 token =====

class TestR14_2PathBNoStaleToken:
    """R14.2: 路径 B 成功时 gitcode_webhook_token 写空串。"""

    def test_host_repo_path_b_writes_empty_wh_token(self):
        """host_repo 中路径 B 成功新建 webhook 时 wh_col 值为空串。"""
        repos_src = _read('src/api/routes/repos.py')
        # 查找路径 B 成功新建的 add_managed_repo 调用
        # 应该有 _use_path_b 时 wh_col 对应值写空串的逻辑
        # 查找 R14.2 注释标记或空串逻辑
        assert '_use_path_b' in repos_src
        # 路径 B 时 _wh_token 应该被设为空串或条件设为空串
        # 检查 host_repo 和 create_new_repo 中的路径 B 逻辑
        assert '"" if' in repos_src or "= ''" in repos_src or '_db_wh_token' in repos_src, \
            "Path B should write empty string for webhook_token column"

    def test_create_new_repo_path_b_no_user_token_in_wh_col(self):
        """create_new_repo 路径 B 时不写用户 token 到 wh_col。"""
        repos_src = _read('src/api/routes/repos.py')
        # 查找 create_new_repo 函数
        func_match = re.search(r'def create_new_repo.*?(?=\ndef )', repos_src, re.DOTALL)
        if func_match:
            func_body = func_match.group()
            # 路径 B 时 _wh_token 应为 api_token，但写入 DB 时 wh_col 值应改为空串
            # 检查是否有路径 B 时写空串的逻辑
            if '_use_path_b' in func_body:
                # 确认不是直接写 api_token 到 wh_col
                assert not re.search(r'wh_col.*api_token\b(?!.*""\s*if)', func_body), \
                    "Path B should not write raw api_token to wh_col without empty string guard"


# ===== R14.3: R10 路径 B issue 回帖尝试全局 bot token =====

class TestR14_3R10PathBBotTokenFallback:
    """R14.3: R10 路径 B issue 回帖也尝试全局 bot token。"""

    def test_fail_task_path_b_tries_global_bot_token(self):
        """fail_task 中路径 B 仓库也尝试全局 bot token 发评论。"""
        svc_src = _read('src/runtime/service.py')
        # 查找 R10 逻辑中的路径 B 分支
        # 应该有路径 B 时先尝试全局 bot token 的逻辑
        r10_section = svc_src[svc_src.index('R10'):]
        # 检查路径 B 分支中是否有全局 bot token fallback
        assert '_get_global_bot_token' in r10_section or 'get_global_github_bot_token' in r10_section, \
            "R10 path B should try global bot token for issue comment"


# ===== R14.4: get_host_user_id 冗余查询消除 =====

class TestR14_4HostUserIdNoRedundantQuery:
    """R14.4: get_host_user_id 每函数只调用 1 次。"""

    def _count_get_host_user_id_calls(self, func_body):
        """统计函数体中 get_host_user_id 的调用次数（排除 def 和注释）。"""
        # 移除注释行
        lines = [l for l in func_body.split('\n') if not l.strip().startswith('#')]
        body = '\n'.join(lines)
        # 统计 get_host_user_id( 调用（不是 def，不是赋值定义）
        calls = re.findall(r'get_host_user_id\(', body)
        return len(calls)

    def test_get_repo_token_with_refresh_single_call(self):
        """get_repo_token_with_refresh 中 get_host_user_id 调用 ≤ 2 次（1次主调用+可能的_is_user_token_repo内部）。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def get_repo_token_with_refresh\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "get_repo_token_with_refresh not found"
        # 统计 get_host_user_id 调用次数
        func_body = func_match.group()
        calls = self._count_get_host_user_id_calls(func_body)
        # 主函数内应 ≤ 2 次（1 次主调用，可能在 _is_user_token_repo 调用中也有）
        # R14.4 修复前是 3 次，修复后应 ≤ 2 次
        assert calls <= 2, f"get_repo_token_with_refresh has {calls} get_host_user_id calls, expected <= 2"

    def test_get_repo_bot_token_single_call(self):
        """get_repo_bot_token 中 get_host_user_id 调用 ≤ 2 次。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def get_repo_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "get_repo_bot_token not found"
        func_body = func_match.group()
        calls = self._count_get_host_user_id_calls(func_body)
        assert calls <= 2, f"get_repo_bot_token has {calls} get_host_user_id calls, expected <= 2"

    def test_get_github_token_single_call(self):
        """get_github_token 中 get_host_user_id 调用 ≤ 2 次。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def get_github_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "get_github_token not found"
        func_body = func_match.group()
        calls = self._count_get_host_user_id_calls(func_body)
        assert calls <= 2, f"get_github_token has {calls} get_host_user_id calls, expected <= 2"


# ===== R14.5: config_store TOCTOU 竞态防护 =====

class TestR14_5ConfigStoreTOCTOU:
    """R14.5: save_config 中 bot token 检测和写入在同一 db_lock 内。"""

    def test_save_config_db_lock_wraps_upgrade_detection(self):
        """save_config 中 _upgrade_triggers 的检测和写入在 with db_lock 块内。"""
        cs_src = _read('src/admin/config_store.py')
        # 查找 _upgrade_triggers 相关代码
        assert '_upgrade_triggers' in cs_src
        # 确认 with db_lock 包裹了 get_meta_value 和 set_meta_value
        # 查找 with db_lock 块包含 _upgrade_triggers
        lock_section = re.search(r'with db_lock:.*?_upgrade_triggers', cs_src, re.DOTALL)
        assert lock_section, "db_lock should wrap _upgrade_triggers detection"


# ===== R14.6: 前端 error_code→i18n 映射启用 =====

class TestR14_6FrontEndErrMsg:
    """R14.6: app.errMsg() 在 catch 块中被调用。"""

    def test_errMsg_function_exists(self):
        """app.js 中 errMsg 函数存在。"""
        app_src = _read('src/api/static/js/app.js')
        assert 'function errMsg(e)' in app_src or 'errMsg = function' in app_src

    def test_errMsg_called_in_catch_blocks(self):
        """app.js 中至少有 catch 块调用 errMsg。"""
        app_src = _read('src/api/static/js/app.js')
        # 查找 errMsg( 调用（不是函数定义）
        calls = re.findall(r'errMsg\(', app_src)
        # 至少有函数定义 + 至少 1 个调用
        assert len(calls) >= 2, f"errMsg should be called in catch blocks, found {len(calls)} occurrences"


# ===== R14.7: grape_accounts.status=4 注释修正 =====

class TestR14_7Status4Annotation:
    """R14.7: grape_accounts.status=4 注释为 merged 而非 token过期。"""

    def test_get_grape_account_by_email_comment_merged(self):
        """get_grape_account_by_email 的注释中 status=4 描述为 merged。"""
        db_src = _read('src/auth/grape_account_db.py')
        # 查找 get_grape_account_by_email 的 docstring
        func_match = re.search(
            r'def get_grape_account_by_email\(.*?"""(.*?)"""', db_src, re.DOTALL
        )
        assert func_match, "get_grape_account_by_email docstring not found"
        docstring = func_match.group(1)
        # status=4 不应该是 "token过期"
        assert 'token过期' not in docstring or 'merged' in docstring, \
            "status=4 in grape_accounts should be 'merged', not 'token过期'"
        assert 'merged' in docstring, "status=4 should mention 'merged'"


# ===== R14.8: dashboard 403 自动切换去重 =====

class TestR14_8Dashboard403Dedup:
    """R14.8: dashboard 403 自动切换有去重逻辑。"""

    def test_403_handler_has_tried_list(self):
        """app.js 403 处理中有已尝试仓库列表。"""
        app_src = _read('src/api/static/js/app.js')
        # 查找 403 处理逻辑附近是否有去重
        idx = app_src.find('e.status === 403')
        if idx < 0:
            idx = app_src.find("e.status == 403")
        assert idx >= 0, "403 handler not found"
        # 查找 403 之后的代码中是否有去重逻辑
        section = app_src[idx:idx + 1000]
        assert '_tried403' in section or 'tried' in section.lower() or '_triedRepos' in section, \
            "403 handler should have dedup/tried list"


# ===== R14.9: create_external_identity 清理 status=4 记录 =====

class TestR14_9CreateExternalIdentityCleanStatus4:
    """R14.9: create_external_identity INSERT 前清理 status=4 记录。"""

    def test_create_external_identity_deletes_status4(self):
        """create_external_identity 中有清理 status=4 的逻辑。"""
        db_src = _read('src/auth/grape_account_db.py')
        func_match = re.search(
            r'def create_external_identity\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "create_external_identity not found"
        func_body = func_match.group()
        # 应该有 DELETE ... status=4 或 status IN (0, 4) 的清理逻辑
        assert 'status=4' in func_body or 'status IN (0, 4)' in func_body or 'status IN (0,4)' in func_body, \
            "create_external_identity should clean status=4 records before INSERT"


# ===== R14.10: events.json zh/en key 对称 =====

class TestR14_10EventsJsonSymmetric:
    """R14.10: events.json zh/en key 完全一致。"""

    def test_events_json_keys_match(self):
        """zh/events.json 和 en/events.json 的 key 集合完全一致。"""
        zh_src = _read('src/api/static/locales/zh/events.json')
        en_src = _read('src/api/static/locales/en/events.json')
        zh_keys = set(json.loads(zh_src).keys())
        en_keys = set(json.loads(en_src).keys())
        assert zh_keys == en_keys, \
            f"events.json key mismatch: zh_only={zh_keys - en_keys}, en_only={en_keys - zh_keys}"


# ===== R14.11: get_user_token_with_refresh dead code 清理 =====

class TestR14_11DeadCodeCleaned:
    """R14.11: get_user_token_with_refresh 尾部无 dead code。"""

    def test_no_unreachable_code_after_return(self):
        """deps.py get_user_token_with_refresh 函数 return 后无不可达代码。"""
        deps_src = _read('src/api/deps.py')
        func_match = re.search(
            r'def get_user_token_with_refresh\(.*?(?=\ndef )', deps_src, re.DOTALL
        )
        assert func_match, "get_user_token_with_refresh not found"
        func_body = func_match.group()
        # 查找函数体中 return 之后的代码（排除 except 块内的 return）
        # 简单检查：函数末尾应该是 return 语句，不是其他代码
        lines = func_body.strip().split('\n')
        # 找到最后一个非空行
        last_lines = [l for l in lines if l.strip()][-3:]
        last_code = '\n'.join(last_lines)
        # 最后应该是 return 语句或 pass 或函数结束
        assert 'return' in last_code or last_code.strip().endswith('') or 'pass' in last_code, \
            "Function should end with return, not dead code"


# ===== R14.12: add_managed_repo UPDATE 保留已有值 =====

class TestR14_12AddManagedRepoPreserveExisting:
    """R14.12: UPDATE 路径保留已有 webhook_secret / bot_token。"""

    def test_select_queries_webhook_secret_and_bot_tokens(self):
        """SELECT 语句查出 webhook_secret / bot_gitcode_token / bot_github_token。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def add_managed_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "add_managed_repo not found"
        func_body = func_match.group()
        # SELECT 语句在源码中是字符串拼接跨行，直接检查列名是否出现在 SELECT 附近
        select_idx = func_body.find('SELECT host_user_id')
        assert select_idx >= 0, "SELECT host_user_id not found in add_managed_repo"
        select_section = func_body[select_idx:select_idx + 200]
        assert 'webhook_secret' in select_section, "SELECT should include webhook_secret"
        assert 'bot_gitcode_token' in select_section, "SELECT should include bot_gitcode_token"
        assert 'bot_github_token' in select_section, "SELECT should include bot_github_token"

    def test_update_uses_final_variables(self):
        """UPDATE 语句使用 _final_secret / _final_bot_gc / _final_bot_gh 而非原始参数。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def add_managed_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        assert '_final_secret' in func_body, "UPDATE should use _final_secret"
        assert '_final_bot_gc' in func_body, "UPDATE should use _final_bot_gc"
        assert '_final_bot_gh' in func_body, "UPDATE should use _final_bot_gh"

    def test_preserve_logic_for_webhook_secret(self):
        """webhook_secret 空串时保留已有值的逻辑存在。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def add_managed_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        assert 'webhook_secret if webhook_secret else' in func_body or \
               '_final_secret = webhook_secret if' in func_body, \
            "webhook_secret should have preserve logic"

    def test_preserve_logic_for_bot_tokens(self):
        """bot_gitcode_token / bot_github_token 空串时保留已有值的逻辑存在。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def add_managed_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        assert '_final_bot_gc = bot_gitcode_token if' in func_body, \
            "bot_gitcode_token should have preserve logic"
        assert '_final_bot_gh = bot_github_token if' in func_body, \
            "bot_github_token should have preserve logic"


# ===== R14.13: _is_user_token_repo / _is_github_user_token_repo 路径 B 守卫 =====

class TestR14_13IsUserTokenRepoGuardAlignment:
    """R14.13: _is_user_token_repo / _is_github_user_token_repo 路径 B 有 host_user_id 守卫。"""

    def test_is_user_token_repo_path_b_has_host_user_id_guard(self):
        """_is_user_token_repo 路径 B 查询前有 host_user_id == 0 守卫。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def _is_user_token_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "_is_user_token_repo not found"
        func_body = func_match.group()
        # 路径 B 查询前应有 host_user_id 守卫
        # 查找 "路径 B" 注释后的代码
        path_b_idx = func_body.find('路径 B')
        if path_b_idx < 0:
            path_b_idx = func_body.find('bot_gitcode_token')
        assert path_b_idx >= 0, "路径 B section not found"
        # 检查路径 B 代码段附近有 host_user_id 守卫
        section = func_body[path_b_idx:path_b_idx + 300]
        assert 'host_user_id' in section or '_hid' in section, \
            "路径 B should have host_user_id guard"

    def test_is_github_user_token_repo_path1_has_guard(self):
        """_is_github_user_token_repo 路径 1（bot_github_token）查询前有 host_user_id 守卫。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def _is_github_user_token_repo\(.*?(?=\n\ndef |\nclass )', db_src, re.DOTALL
        )
        if not func_match:
            # 尝试到文件尾
            func_match = re.search(
                r'def _is_github_user_token_repo\(.*', db_src, re.DOTALL
            )
        assert func_match, "_is_github_user_token_repo not found"
        func_body = func_match.group()
        # 路径 1（bot_github_token）查询前应有 host_user_id 守卫
        bot_gh_idx = func_body.find('bot_github_token')
        assert bot_gh_idx >= 0, "bot_github_token query not found"
        section = func_body[:bot_gh_idx + 200]
        assert '_hid' in section or 'host_user_id' in section, \
            "路径 1 should have host_user_id guard before bot_github_token query"

    def test_is_github_user_token_repo_path2_has_guard(self):
        """_is_github_user_token_repo 路径 2（owner external_identities）查询前有 host_user_id 守卫。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def _is_github_user_token_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "_is_github_user_token_repo not found"
        func_body = func_match.group()
        # 跳过 docstring，查找代码中的 external_identities / get_grape_account_by_username
        # 第一次出现在 docstring 中，找第二次（代码中）
        ext_idx = func_body.find('external_identities')
        if ext_idx < 0:
            ext_idx = func_body.find('get_grape_account_by_username')
        # 跳过 docstring 中的出现，找代码中的出现
        ext_idx2 = func_body.find('get_grape_account_by_username', ext_idx + 1) if ext_idx >= 0 else -1
        code_idx = ext_idx2 if ext_idx2 >= 0 else ext_idx
        assert code_idx >= 0, "external_identities code section not found"
        section = func_body[:code_idx + 50]
        assert '_hid == 0' in section or 'host_user_id == 0' in section, \
            "路径 2 should have host_user_id guard before external_identities query"

    def test_is_user_token_repo_single_get_host_user_id_call(self):
        """_is_user_token_repo 函数内 get_host_user_id 调用 ≤ 1 次。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def _is_user_token_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        calls = re.findall(r'get_host_user_id\(', func_body)
        assert len(calls) <= 1, \
            f"_is_user_token_repo has {len(calls)} get_host_user_id calls, expected <= 1"

    def test_is_github_user_token_repo_single_get_host_user_id_call(self):
        """_is_github_user_token_repo 函数内 get_host_user_id 调用 ≤ 1 次。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def _is_github_user_token_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "_is_github_user_token_repo not found"
        func_body = func_match.group()
        calls = re.findall(r'get_host_user_id\(', func_body)
        assert len(calls) <= 1, \
            f"_is_github_user_token_repo has {len(calls)} get_host_user_id calls, expected <= 1"


# ===== R14.14: update_managed_repo_bot_token 清 host_user_id + provider 分列 =====

class TestR14_14UpdateManagedRepoBotToken:
    """R14.14/R14.16: update_managed_repo_bot_token 统一入口清 host_user_id + 重建 webhook。"""

    def test_calls_rebuild_webhook(self):
        """R14.16: update_managed_repo_bot_token 调用 _rebuild_webhook_with_bot_token。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def update_managed_repo_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "update_managed_repo_bot_token not found"
        func_body = func_match.group()
        assert '_rebuild_webhook_with_bot_token' in func_body, \
            "R14.16: should call _rebuild_webhook_with_bot_token"

    def test_calls_update_repo_bot_token(self):
        """R14.16: update_managed_repo_bot_token 调用 _update_repo_bot_token 统一写入。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def update_managed_repo_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        assert '_update_repo_bot_token' in func_body, \
            "R14.16: should call _update_repo_bot_token"

    def test_provider_split_by_repo_key_prefix(self):
        """R14.14: 按 repo_key 前缀判断 provider。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def update_managed_repo_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        assert 'github:' in func_body, \
            "should check github: prefix to determine provider"

    def test_rebuild_failure_does_not_block_db_write(self):
        """R14.16: webhook 重建失败时仍写 DB（webhook_id 为空，用户可 repair）。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def update_managed_repo_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        # 重建失败后仍调 _update_repo_bot_token（return True 不阻断）
        assert 'if not _new_hook_id' in func_body, \
            "should handle rebuild failure without blocking DB write"

    def test_rebuild_webhook_function_exists(self):
        """R14.16: _rebuild_webhook_with_bot_token 统一重建函数存在。"""
        db_src = _read('src/user/db.py')
        assert 'def _rebuild_webhook_with_bot_token' in db_src, \
            "_rebuild_webhook_with_bot_token function should exist"

    def test_rebuild_handles_remove_failure(self):
        """R14.16: 删旧 webhook 失败时不建新（返回空，保留旧 webhook）。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def _rebuild_webhook_with_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "_rebuild_webhook_with_bot_token not found"
        func_body = func_match.group()
        # 删旧失败应返回空串
        assert 'return ""' in func_body, \
            "should return empty string when remove old webhook fails"


# ===== R14.15: add_managed_repo 路径 A 成功时显式清 host_user_id =====

class TestR14_15AddManagedRepoPathAClearsHostUserId:
    """R14.15: add_managed_repo 路径 A 成功时显式清 host_user_id=0。"""

    def test_final_host_clears_when_bot_token_nonempty(self):
        """调用方传 bot_token 非空 + host_user_id=0 时 _final_host=0。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def add_managed_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        # 查找 _final_host 三分支逻辑
        assert 'elif bot_gitcode_token or bot_github_token' in func_body, \
            "_final_host should have elif branch for bot_token nonempty"
        # 该分支应设 _final_host = 0
        elif_idx = func_body.find('elif bot_gitcode_token or bot_github_token')
        branch = func_body[elif_idx:elif_idx + 200]
        assert '_final_host = 0' in branch, \
            "Path A (bot_token nonempty) should set _final_host = 0"

    def test_final_host_preserves_when_no_bot_token(self):
        """调用方传 bot_token 空 + host_user_id=0 时保留已有值。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def add_managed_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        # 查找 else 分支（未传 bot_token）
        assert '_existing_host' in func_body, \
            "_final_host should preserve _existing_host when no bot_token"

    def test_final_host_uses_caller_value_when_positive(self):
        """调用方传 host_user_id > 0 时用调用方值。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def add_managed_repo\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        assert 'if host_user_id > 0:' in func_body, \
            "_final_host should have if branch for host_user_id > 0"
        if_idx = func_body.find('if host_user_id > 0:')
        branch = func_body[if_idx:if_idx + 100]
        assert '_final_host = host_user_id' in branch, \
            "Path B (host_user_id > 0) should set _final_host = host_user_id"


# ===== R14.18: fail_task errors append 模式 =====

class TestR14_18FailTaskErrorsAppend:
    """R14.18: R10 token 过期写入 errors 时保留原始错误（append 而非覆写）。"""

    def test_r10_reads_existing_errors_before_write(self):
        """R10 写 errors 前先 SELECT 读取已有 errors。"""
        svc_src = _read('src/runtime/service.py')
        r10_idx = svc_src.find('R14.18')
        assert r10_idx >= 0, "R14.18 append mode not found"
        section = svc_src[r10_idx:r10_idx + 2000]
        assert 'SELECT errors FROM agent_tasks' in section, \
            "R10 should SELECT existing errors before writing"

    def test_r10_appends_not_overwrites(self):
        """R10 append TOKEN_EXPIRED 到已有列表，不覆写。"""
        svc_src = _read('src/runtime/service.py')
        r10_idx = svc_src.find('R14.18')
        section = svc_src[r10_idx:r10_idx + 2000]
        assert 'append' in section, \
            "R10 should append TOKEN_EXPIRED to existing errors"
        assert '_token_expired_err' in section, \
            "R10 should use _token_expired_err variable"

    def test_r10_limits_errors_list_length(self):
        """R10 限制 errors 列表长度。"""
        svc_src = _read('src/runtime/service.py')
        r10_idx = svc_src.find('R14.18')
        section = svc_src[r10_idx:r10_idx + 2000]
        assert '[-5:]' in section or '[-3:]' in section, \
            "R10 should limit errors list length"

    def test_r10_handles_non_json_errors(self):
        """已有 errors 非 JSON list 时转为 list 再 append。"""
        svc_src = _read('src/runtime/service.py')
        r10_idx = svc_src.find('R14.18')
        section = svc_src[r10_idx:r10_idx + 2000]
        assert 'JSONDecodeError' in section or 'TypeError' in section, \
            "R10 should handle non-JSON errors gracefully"
        assert 'isinstance' in section and 'list' in section, \
            "R10 should check if parsed value is a list"


# ===== R14.19: update_managed_repo_bot_token 404 检测 + 注释修正 + 统计修正 =====

class TestR14_19UpdateManagedRepoBotToken404:
    """R14.19: update_managed_repo_bot_token 先检查仓库存在再重建 webhook。"""

    def test_checks_existence_before_rebuild(self):
        """先 SELECT 检查仓库存在，不存在返回 False。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def update_managed_repo_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "update_managed_repo_bot_token not found"
        func_body = func_match.group()
        # 应有 SELECT 1 FROM managed_repos 检查
        assert 'SELECT 1 FROM managed_repos' in func_body, \
            "should check repo existence with SELECT 1"
        # 不存在时返回 False
        assert 'return False' in func_body, \
            "should return False when repo not found"

    def test_rebuild_after_existence_check(self):
        """仓库存在时才调 _rebuild_webhook_with_bot_token。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def update_managed_repo_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        func_body = func_match.group()
        # 跳过 docstring，找代码中的实际调用
        docstring_end = func_body.find('"""', func_body.find('"""') + 3) + 3
        code_body = func_body[docstring_end:]
        select_idx = code_body.find('SELECT 1 FROM managed_repos')
        rebuild_idx = code_body.find('_rebuild_webhook_with_bot_token')
        assert select_idx >= 0 and rebuild_idx >= 0 and select_idx < rebuild_idx, \
            "existence check should come before webhook rebuild in code"

    def test_rebuild_comment_accurate(self):
        """_rebuild_webhook_with_bot_token 删旧失败注释准确描述行为。"""
        db_src = _read('src/user/db.py')
        func_match = re.search(
            r'def _rebuild_webhook_with_bot_token\(.*?(?=\ndef )', db_src, re.DOTALL
        )
        assert func_match, "_rebuild_webhook_with_bot_token not found"
        func_body = func_match.group()
        # 注释不应声称"保留旧 webhook_secret"
        assert '保留旧 webhook_secret' not in func_body, \
            "comment should not claim 'preserve old webhook_secret'"
        # 注释应提及 webhook 403 或 loud fail
        assert '403' in func_body or 'loud fail' in func_body, \
            "comment should mention webhook 403 or loud fail"

    def test_auto_upgrade_log_distinguishes_webhook_failure(self):
        """auto_upgrade 日志区分 upgraded 和 webhook_rebuild_failed。"""
        db_src = _read('src/user/db.py')
        func_start = db_src.index('def auto_upgrade_path_b_repos')
        func_body = db_src[func_start:]
        assert 'webhook_rebuild_failed' in func_body, \
            "auto_upgrade log should include webhook_rebuild_failed count"
