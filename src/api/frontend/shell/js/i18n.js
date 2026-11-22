/**
 * i18n.js — Grape Internationalization Core Module
 *
 * IIFE pattern (not self-executing). Call I18N.init(cacheVer) from index.html
 * after the script loads, before app.js init.
 *
 * Features:
 * - SUPPORTED = ['zh', 'en'], DEFAULT_LANG = 'zh'
 * - URL pathname prefix detection (/en → en, else → zh)
 * - localStorage + cookie dual-write for language persistence
 * - Async fetch of 16 module JSON language packs
 * - Inline minimal packs for synchronous first paint (common + app.login + app.repo)
 * - {{name}} variable interpolation
 * - Fallback to default language (zh) when key missing in current language
 * - DOM element replacement via [data-i18n] attributes
 *
 * Usage:
 *   I18N.init(cacheVer)  // sync init with inline packs, async load full packs
 *   I18N.t('common.loading')
 *   I18N.t('app.login.username')
 *   I18N.setLang('en')  // switches language + reloads with /en prefix
 *   I18N.getLang()      // returns 'zh' or 'en'
 */
var I18N = (function () {
  'use strict';

  var SUPPORTED = ['zh', 'en'];
  var DEFAULT_LANG = 'zh';
  var STORAGE_KEY = 'grapeLang';

  // 16 module names — each corresponds to a JSON file in locales/<lang>/
  var MODULES = [
    'common', 'app', 'settings', 'knowledge', 'skills',
    'chat', 'events', 'agents', 'pipeline', 'runtimes',
    'tasks', 'issues', 'repos', 'dashboard', 'observability', 'templates',
    'organizations', 'autopilot', 'admin', 'plugins', 'sandbox'
  ];

  var _currentLang = DEFAULT_LANG;
  var _packs = {};          // { zh: {common: {...}, app: {...}, ...}, en: {...} }
  var _loadedLangs = {};    // { zh: true, en: false } — whether full packs loaded
  var _cacheVer = '';

  // ── Inline minimal packs for synchronous first paint ──
  // Covers common.* + app.login.* + app.repo.* — enough for login screen + repo selection
  var _INLINE_PACKS = {
    zh: {
      common: {
        loading: '加载中...',
        load_failed: '加载失败',
        save: '保存',
        cancel: '取消',
        confirm: '确认',
        delete: '删除',
        login: '登录',
        logout: '登出',
        search: '搜索',
        no_data: '暂无数据',
        error: '错误',
        success: '成功',
        back: '返回',
        create: '创建',
        update: '更新',
        close: '关闭',
        refresh: '刷新',
        server_error: '服务器错误，请稍后重试'
      },
      app: {
        login: {
          gitcode_login: 'GitCode',
          github_login: 'GitHub',
          gitee_login: 'Gitee',
          huawei_login: '华为账号',
          beta_apply: '申请内测',
          admin: '管理后台',
          username: '用户名',
          password: '密码',
          login_btn: '登录',
          register: '注册账号',
          forgot_password: '忘记密码',
          back_to_login: '返回登录',
          send_code: '发送验证码',
          resend_code: '重新发送',
          confirm_new_password_placeholder: '再次输入新密码',
          register_btn: '注册',
          reset_password_btn: '重置密码',
          or_oauth: '或使用第三方登录',
          failed: '登录失败',
          oauth_not_configured: '系统未配置 {name} OAuth，{name} 登录暂不可用',
          oauth_not_configured_hint: '请使用用户名+密码注册登录；管理员可按帮助文档配置 {name} OAuth',
          oauth_view_config_help: '查看配置帮助',
          oauth_needs_config: '{name} 登录需要管理员先配置 OAuth 后才能使用',
          oauth_needs_config_generic: '部分第三方登录需要管理员先配置 OAuth 后才能使用',
          oauth_title: '使用 {name} 账号登录',
          oauth_title_huawei: '使用华为账号登录',
          oauth_request_failed: '登录请求失败，请稍后重试',
          oauth_exchange_failed: 'OAuth登录失败，请重试',
          empty_input: '请输入用户名和密码',
          register_title: '注册 Grape 账号',
          register_sub: 'AI Native DevOps',
          username_placeholder: '3-32字符，字母、数字、下划线',
          email: '邮箱',
          email_placeholder: 'you@example.com',
          password_placeholder: '至少8个字符',
          code_placeholder: '6位验证码',
          have_account: '已有账号？',
          forgot_title: '重置密码',
          forgot_sub: '输入注册邮箱获取验证码',
          remember: '想起来了？',
          associate_existing: '关联已有账号',
          register_new: '注册新账号',
          associate_btn: '关联并登录',
          account_or_email: '用户名或邮箱',
          title_login: '登录 — Grape',
          title_register: '注册 — Grape',
          title_forgot: '重置密码 — Grape',
          pwd_strength: '密码强度',
          pwd_strength_weak: '弱',
          pwd_strength_medium: '中',
          pwd_strength_strong: '强',
          pwd_req_length: '至少8个字符',
          pwd_req_alnum: '包含字母和数字',
          pwd_req_not_match: '不与用户名/邮箱相同',
          pwd_req_blocklist: '非常见弱密码',
          confirm_password: '确认密码',
          confirm_new_password: '确认新密码',
          err_username_required: '请输入用户名',
          err_username_length: '用户名长度3-32字符',
          err_username_format: '仅允许字母、数字、下划线',
          err_email_required: '请输入邮箱',
          err_email_format: '邮箱格式不正确，例如 you@example.com',
          err_password_required: '请输入密码',
          err_password_length: '密码至少8个字符',
          err_password_blocklist: '该密码过于常见，请更换为更独特的密码',
          err_confirm_required: '请再次输入密码',
          err_confirm_mismatch: '两次密码输入不一致',
          err_code_required: '请输入验证码',
          err_code_format: '验证码为6位数字',
          err_new_password_required: '请输入新密码',
          err_confirm_new_required: '请再次输入新密码',
          err_identifier_required: '请输入用户名或邮箱',
          toast_code_sent: '验证码已发送到',
          toast_code_sent_hint: '如果该邮箱可注册，验证码已发送，请查收邮件',
          code_not_received_hint: '没收到验证码？如果你已注册，请',
          or: '或',
          toast_code_resent: '验证码已重新发送',
          toast_send_failed: '发送失败',
          toast_register_failed: '注册失败',
          toast_associate_failed: '关联失败',
          toast_reset_failed: '重置失败',
          toast_register_success: '注册成功',
          toast_associate_success: '关联成功',
          toast_logging_in: '正在登录...',
          toast_reset_success: '密码重置成功',
          toast_use_new_password: '请使用新密码登录',
          toast_password_reset_notice: '密码已重置，请使用新密码登录',
          back_to_edit_email: '← 返回修改邮箱',
          back_to_edit_info: '← 返回修改信息',
          lang_switch_en: 'EN',
          lang_switch_zh: '中文',
          step_fill_info: '填写信息',
          step_verify_code: '验证码',
          step_success: '完成',
          step_email: '邮箱',
          step_code_new_pass: '验证码 + 新密码',
          show_password: '显示密码',
          hide_password: '隐藏密码',
          verify_code: '验证码',
          code_sent_to_email: '验证码将发送到您的邮箱',
          new_password: '新密码',
          select_tab_hint: '选择注册新账号或关联已有账号',
          confirm_password_placeholder: '再次输入密码',
          associate_title: '关联 {provider} 账号',
          oauth_not_associated: '你的 {name}（{login}）尚未关联 Grape 账号',
          step_label_info: '填写信息',
          step_label_verify: '验证码',
          step_label_new_pass: '新密码',
          step_label_done: '完成',
          step_label_email: '邮箱',
          step_next: '下一步'
        },
        repo: {
          select: '选择仓库',
          load_failed: '无法加载仓库列表，请确认 GitCode Token 是否有效'
        },
        help: {
          drawer_title: '📖 使用帮助 & FAQ',
          loading: '加载中...',
          fullscreen: '全屏',
          exit_fullscreen: '退出全屏',
          open_new_tab: '在新标签页打开'
        },
        loading: '加载中...'
      },
      admin: {
        title: 'Grape Admin — 配置中心',
        login: { subtitle: '系统管理 · 配置中心', admin_account: '管理员账号', password: '登录密码', login_btn: '登 录', enter_credentials: '请输入账号和密码', back_to_app: '← 返回用户界面' },
        nav: { config_center: '配置中心', credentials: '平台凭据', user_management: '用户管理', user_list: '用户列表', whitelist: '注册白名单', login_whitelist: '登录白名单', change_password: '修改密码', logout: '退出登录', channels: '登录渠道', platforms: '仓库平台' },
        page: {
          bots_all_inactive: "bot 全部不可用（回落用户 PAT）",

          bots_none_short: "bot 未配置（回落用户 PAT）",

          bot_username: "bot 用户名",
          bot_token: "bot token（加密入库）",
          bots_hint: "一个平台一个 bot；token 留空=不改。无 bot 时托管动作回落用户 PAT",
          bot_none: "（未配置 bot 账号）",
          self_domain: "本平台（同域天然可接入，无需配对）",

          channels_list_sub: "登录渠道负责 OAuth 登录/绑定；点一行查看凭据与「可接入的代码托管平台（跨名配对）」。",
          platforms_list_sub: "仓库平台负责托管与代理行为；点一行查看定义字段、bot 账号与 token 健康。",
          sec_state: "状态",
          sec_credential: "凭据",
          sec_definition: "定义字段（只读 · 来自配置）",
          keep_hint: "留空 = 保持现有值不变",

          back: "返回列表",
          col_name: "名称",
          col_provider: "标识",
          col_enabled: "状态",
          col_health: "bot token 健康",
          col_drive: "可接入的代码托管平台（跨名配对）",
          col_bots: "bot 账号数",
          channels_empty: "还没有在册的登录渠道（配置导入后自动出现）",
          platforms_empty: "还没有在册的仓库平台（配置导入后自动出现）",
          domain_note_channel: "登录渠道卡：OAuth 登录/绑定凭据，与仓库平台卡解耦",
          domain_note_platform: "托管平台卡：仓库托管与代理行为，与登录渠道卡解耦",
          health_healthy: "健康",
          health_degraded: "降级",
          health_unhealthy: "不健康",
          bot_username_ph: "bot 用户名",
          bot_token_ph: "bot token（加密入库）",
          bot_save_token: "用上面的 token 轮换",
          bot_need_username: "请填 bot 用户名",
          bot_add_failed: "添加 bot 失败",
          bot_save_failed: "保存 token 失败",
          bot_del_failed: "删除 bot 失败",
          bot_clear: "清除 token",
          bot_save: "保存",
          cancel: "取消",
          disable_confirm_title: "关闭平台确认",
          disable_confirm: "关闭 {provider} 后，它在用户面将完全不可见：{repos} 个仓库、{users} 位用户、{managed} 个托管仓库不再展示（{pats} 位用户的平台凭据仍保留）。确认关闭？",
          disable_confirm_btn: "确认关闭",

          channels_title: "登录渠道",
          values_label: "取值：",
          platforms_title: "代码托管平台",
          channels_hint: "定义字段（端点/scope/字段映射）来自配置文件，这里只读；本页只写凭据与启用开关。",
          platforms_hint: "方言/端点/能力位来自平台卡与 repo-credentials.json（只读）；本页只写启用开关与 bot 账号。",
          readonly_tag: "只读 · 来自配置",
          enabled: "启用",
          save_creds: "保存凭据",
          save: "保存",
          configured_keep: "已配置（留空不改）",
          config_overridden: "该平台由配置文件定义，本次改动已按管理面覆盖生效：{fields}；改回文件值即解除覆盖",
          card_taken_over: "该平台卡已由管理面接管（定义落表）：配置文件之后的改动不会再自动覆盖",
          card_taken_over_fields: "；本次改动字段：{fields}",
          config_same: "该平台由配置文件定义，本次提交与文件一致，未产生覆盖",
          add_card: "新增平台卡",
          add_card_desc: "为自建实例新建一张卡（如 gitlab-corp）：克隆现有同族卡后改 id / host / API 基址",
          add_from: "基于",
          add_id_ph: "gitlab-corp（小写字母开头，字母数字下划线短横）",
          add_name: "显示名",
          add_bad_id: "provider id 非法：小写字母开头，仅字母/数字/下划线/短横，≤40 字符",
          add_exists: "该 provider id 已存在",
          add_no_template: "模板卡不可用（所选平台没有平台卡）",
          sec_card: "平台卡（高级）",
          card_what: "平台卡的完整定义：私有 CA（ca_bundle，PEM 绝对路径，空=系统 CA）、PAT 创建页地址、下载基址、存储字段等都在这里；私有 CA 只对走 HTTPS 校验的实例有意义。",
          card_note: "保存时以整卡回写，未展示的字段不会丢。改 host/api_base 前先确认实例可达；配置文件定义的平台，改动会落成管理面覆盖。",
          ca_ph: "/path/to/corp-ca.pem（空=系统 CA）",
          edit: "编辑",
          card_missing: "该平台没有平台卡（仅表投影），高级配置不可编辑",
          not_configured: "未配置",
          loading: "加载中…",
          load_failed: "加载失败",
          save_failed: "保存失败",
          toggle_failed: "开关失败",
          requires_scope: "所需 scope",
          no_scope_check: "（不校验）",
          bot_support: "bot",
          bot_supported: "支持",
          bot_unsupported: "不支持",
          bot_accounts: "bot",
          bot_default: "默认",
          bot_normal: "普通",
          bot_none: "（未配置 bot 账号；无 bot 时托管动作回落用户 PAT）",
          empty_val: "（空）",
          need_one: "client_id 与 client_secret 至少填一项",
          client_id: "client_id",
          client_id: {"what": "OAuth 应用 client_id（写入加密列，只回布尔）。", "values": "平台应用页复制", "note": "⚠ 留空＝保持现值；保存后不可再读回明文。"},
          client_secret: "client_secret",
          page_help_title: "这一页能改什么",
          no_doc: "暂无说明（可在 i18n 或配置文件 _fields 补充）",
          bot_no: "不支持",
          unset: "未配置",
          channels_note: "⚠ 定义字段请改 configs/oauth-providers.json 后重启或重新导入。",
          bot_yes: "支持",
          scope_none: "（不校验）",
          switch: "启用",
          pill_on: "启用",
          platforms_note: "⚠ 定义字段改动请改配置文件并重新导入；enabled 归数据库，配置文件不会覆盖它。",
          bots_note: "⚠ 响应永不含 token 值；仓库级绑定优先于这里的默认账号。",
          help_title: "这一页能改什么",
          pill_off: "停用",
          keep: "已配置（留空不改）",
          scope_need: "所需 scope",
          empty: "（空）",
          bots_what: "平台上的操作身份与其 token（加密存储）。is_default 每平台至多一个。",
          bots: "bot 账号",
          bot: "bot",
        },
          field: {
          type: {"what": "协议模式：决定登录流程与用户信息取法。", "values": "oauth2 | oidc | saml", "note": "⚠ 只有 oidc 支持 id_token claims 兜底；oauth2 拿不到用户字段就是登录失败。"},
          auth_url: {"what": "授权页地址（用户点击后跳转的入口）。", "values": "https://…/authorize", "note": "三件套之一，缺任一整卡都会被拒绝加载。"},
          token_url: {"what": "用 code 换 token 的端点。", "values": "https://…/token", "note": "三件套之一；端点错会表现为换 token 失败而非登录页报错。"},
          user_url: {"what": "拉取用户信息的端点。", "values": "https://…/user（可含 {access_token} 占位）", "note": "⚠ 华为等平台需把 token 放查询参数，用 {access_token} 占位。"},
          emails_endpoint: {"what": "单独拉邮箱的端点（可选）。", "values": "https://…/emails 或空", "note": "空=该渠道不提供邮箱补抓，注册时以主登录名走。"},
          scope: {"what": "登录时申请的权限范围（最小权限原则）。", "values": "空格分隔，如 user_info / repo", "note": "⚠ ①改这里会影响该渠道的**所有**关联；②Gitee 只能取官方合法集，且请求范围要与用户上次授权一致，否则每次弹授权页；③范围不足会导致仓库操作返回 SCOPE_INSUFFICIENT。"},
          pkce_enabled: {"what": "OAuth PKCE 校验开关。", "values": "true | false", "note": "⚠ Gitee 必须 false，否则每次登录都会重新弹授权页。"},
          id_field: {"what": "用户信息里唯一 id 的字段名。", "values": "如 id / openID", "note": "⚠ 华为只有 openID，没有 login 名。"},
          login_field: {"what": "登录名取哪个字段。", "values": "如 login / preferred_username / openID", "note": "取不到会回落到外部 id，展示名会变成一串编号。"},
          name_field: {"what": "显示名取哪个字段。", "values": "如 name / nickname", "note": "空则该渠道显示名回落登录名。"},
          avatar_field: {"what": "头像 URL 取哪个字段。", "values": "如 avatar_url", "note": "空=按平台默认规则派生。"},
          redirect_uri: {"what": "回调地址。", "values": "https://…/api/auth/callback/<provider> 或空", "note": "空=由 server_base_url 自动派生；手填错会报 redirect_uri_mismatch。"},
          accept_header: {"what": "请求用户信息时的 Accept 头。", "values": "如 application/vnd.github+json", "note": "GitHub 不设会拿到旧版字段结构。"},
          repo_links: {"what": "该渠道允许驱动的仓库平台（只读展示，由仓库侧配置）。", "values": "[{\"repo_provider\":\"gitee\"}]", "note": "配了这一对=该登录渠道的令牌可接入该平台（登录即建用户↔平台关系）；没有档位字段，删掉配对即不再可接入。"},
          bot_username: {"what": "该平台唯一的 bot 账号名。", "values": "平台登录名或空", "note": "⚠ 平台行是事实来源；凭据在 bot_token_enc（加密）。"},
          enabled: {"what": "该对象是否启用（DB 权威）。", "values": "true | false", "note": "⚠ 配置文件不会覆盖它；导入永不改该列。"},
          dialect: {"what": "API 方言家族。", "values": "github | gitlab", "note": "⚠ gitcode/gitee/github 都是 github —— 看起来不同不代表需要新方言。"},
          host: {"what": "网页域名。", "values": "如 gitee.com", "note": "不要在这里填 API 基址。"},
          api_base: {"what": "API 基址。", "values": "如 https://gitee.com/api/v5", "note": "不要在这里填网页域名。"},
          api_auth: {"what": "token 承载位置。", "values": "query_token | token_header | private_token", "note": "选错表现为 401，且日志无线索。"},
          webhook_path: {"what": "webhook 接收路径。", "values": "/webhook/<provider>", "note": "必须与平台侧的 webhook 配置一致。"},
          event_header: {"what": "事件头名称。", "values": "如 X-GitHub-Event", "note": "用于分派 webhook 处理器。"},
          webhook_verify: {"what": "webhook 校验方式。", "values": "hmac | token_compare", "note": "⚠ hmac=验签头；token_compare=比对 URL token，选错会 401。"},
          webhook_dialect: {"what": "webhook 载荷形态。", "values": "github_web | gitee_v5", "note": "⚠ 必须显式声明，未声明时托管侧 fail-closed。"},
          clone_auth: {"what": "克隆/推送凭据形态。", "values": "basic_password | x_access_token | oauth2", "note": "影响 git remote URL 构造。"},
          issue_write_form: {"what": "建 Issue 端点形态。", "values": "repo_path | owner_param", "note": "⚠ Gitee 必须 owner_param，否则 404。"},
          branch_create_form: {"what": "建分支请求形态。", "values": "form | refs_sha", "note": "各平台请求体不同。"},
          pr_head_filter: {"what": "PR 列表 head 参数形态。", "values": "owner_ref | bare_ref", "note": "⚠ 形态发错是静默空列表，不报错。"},
          issue_search: {"what": "是否支持 issue 搜索。", "values": "true | false", "note": "驱动 issue 选择器。"},
          git_tree_family: {"what": "代码树 API 家族。", "values": "v5 | github_api", "note": "平台专属端点。"},          client_id: {"what": "OAuth 应用 client_id（写入加密列，只回布尔）。", "values": "平台应用页复制", "note": "⚠ 留空＝保持现值；保存后不可再读回明文。"},
          client_id: {"what": "OAuth 应用 client_id（写入加密列，只回布尔）。", "values": "平台应用页复制", "note": "⚠ 留空＝保持现值；保存后不可再读回明文。"},
          client_secret: {"what": "OAuth 应用 client_secret（写入加密列，只回布尔）。", "values": "平台应用页复制", "note": "⚠ 留空＝保持现值；轮换时先填新值再保存。"},

          },
      }
    },
    en: {
      common: {
        loading: 'Loading...',
        load_failed: 'Failed to load',
        save: 'Save',
        cancel: 'Cancel',
        confirm: 'Confirm',
        delete: 'Delete',
        login: 'Login',
        logout: 'Logout',
        search: 'Search',
        no_data: 'No data',
        error: 'Error',
        success: 'Success',
        back: 'Back',
        create: 'Create',
        update: 'Update',
        close: 'Close',
        refresh: 'Refresh',
        server_error: 'Server error, please try again later'
      },
      app: {
        login: {
          gitcode_login: 'GitCode',
          github_login: 'GitHub',
          gitee_login: 'Gitee',
          huawei_login: 'HUAWEI ID',
          beta_apply: 'Apply for Beta',
          admin: 'Admin Console',
          username: 'Username',
          password: 'Password',
          login_btn: 'Sign In',
          register: 'Register',
          forgot_password: 'Forgot Password',
          back_to_login: 'Back to Login',
          send_code: 'Send Code',
          resend_code: 'Resend',
          confirm_new_password_placeholder: 'Enter new password again',
          register_btn: 'Register',
          reset_password_btn: 'Reset Password',
          or_oauth: 'or sign in with',
          failed: 'Login failed',
          oauth_not_configured: 'OAuth for {name} is not configured on this server; {name} sign-in is unavailable',
          oauth_not_configured_hint: 'Please register with username + password; the admin can configure {name} OAuth via the help guide',
          oauth_view_config_help: 'View setup guide',
          oauth_needs_config: '{name} sign-in requires the admin to configure OAuth first',
          oauth_needs_config_generic: 'Some third-party sign-ins require the admin to configure OAuth first',
          oauth_title: 'Sign in with {name}',
          oauth_title_huawei: 'Sign in with Huawei ID',
          oauth_request_failed: 'Sign-in request failed, please retry later',
          oauth_exchange_failed: 'OAuth login failed, please retry',
          empty_input: 'Please enter username and password',
          register_title: 'Create Grape Account',
          register_sub: 'AI Native DevOps',
          username_placeholder: '3-32 chars, letters, digits, underscore',
          email: 'Email',
          email_placeholder: 'you@example.com',
          password_placeholder: 'At least 8 characters',
          code_placeholder: '6-digit code',
          have_account: 'Already have an account?',
          forgot_title: 'Reset Password',
          forgot_sub: 'Enter your registered email to get a code',
          remember: 'Remembered?',
          associate_existing: 'Link Existing Account',
          register_new: 'Register New Account',
          associate_btn: 'Link & Sign In',
          account_or_email: 'Username or Email',
          title_login: 'Sign In — Grape',
          title_register: 'Register — Grape',
          title_forgot: 'Reset Password — Grape',
          pwd_strength: 'Password Strength',
          pwd_strength_weak: 'Weak',
          pwd_strength_medium: 'Medium',
          pwd_strength_strong: 'Strong',
          pwd_req_length: 'At least 8 characters',
          pwd_req_alnum: 'Contains letters and numbers',
          pwd_req_not_match: 'Not same as username/email',
          pwd_req_blocklist: 'Not a common weak password',
          confirm_password: 'Confirm Password',
          confirm_new_password: 'Confirm New Password',
          err_username_required: 'Please enter username',
          err_username_length: 'Username must be 3-32 characters',
          err_username_format: 'Only letters, digits, and underscore allowed',
          err_email_required: 'Please enter email',
          err_email_format: 'Invalid email format, e.g. you@example.com',
          err_password_required: 'Please enter password',
          err_password_length: 'Password must be at least 8 characters',
          err_password_blocklist: 'This password is too common — pick something more unique',
          err_confirm_required: 'Please enter password again',
          err_confirm_mismatch: 'Passwords do not match',
          err_code_required: 'Please enter verification code',
          err_code_format: 'Code must be 6 digits',
          err_new_password_required: 'Please enter new password',
          err_confirm_new_required: 'Please enter new password again',
          err_identifier_required: 'Please enter username or email',
          toast_code_sent: 'Code sent to',
          toast_code_sent_hint: 'If this email is available, a verification code has been sent. Please check your inbox.',
          code_not_received_hint: 'Didn\'t receive the code? If you\'re already registered, please',
          or: 'or',
          toast_code_resent: 'Code resent',
          toast_send_failed: 'Send failed',
          toast_register_failed: 'Registration failed',
          toast_associate_failed: 'Link failed',
          toast_reset_failed: 'Reset failed',
          toast_register_success: 'Registration successful',
          toast_associate_success: 'Account linked',
          toast_logging_in: 'Logging in...',
          toast_reset_success: 'Password reset successful',
          toast_use_new_password: 'Please sign in with your new password',
          toast_password_reset_notice: 'Password has been reset. Please sign in with your new password.',
          back_to_edit_email: '← Back to edit email',
          back_to_edit_info: '← Back to edit info',
          lang_switch_en: 'EN',
          lang_switch_zh: '中文',
          step_fill_info: 'Info',
          step_verify_code: 'Verify',
          step_success: 'Done',
          step_email: 'Email',
          step_code_new_pass: 'Code + New Password',
          show_password: 'Show password',
          hide_password: 'Hide password',
          verify_code: 'Verification Code',
          code_sent_to_email: 'A verification code will be sent to your email',
          new_password: 'New Password',
          select_tab_hint: 'Register a new account or link an existing one',
          confirm_password_placeholder: 'Enter password again',
          associate_title: 'Link {provider} Account',
          oauth_not_associated: 'Your {name} ({login}) is not yet linked to a Grape account',
          step_label_info: 'Info',
          step_label_verify: 'Verify',
          step_label_new_pass: 'New Password',
          step_label_done: 'Done',
          step_label_email: 'Email',
          step_next: 'Next'
        },
        repo: {
          select: 'Select Repository',
          load_failed: 'Failed to load repository list. Please check your GitCode Token.'
        },
        help: {
          drawer_title: '📖 Help & FAQ',
          loading: 'Loading...',
          fullscreen: 'Fullscreen',
          exit_fullscreen: 'Exit fullscreen',
          open_new_tab: 'Open in new tab'
        },
        loading: 'Loading...'
      },
      admin: {
        title: 'Grape Admin — Config Center',
        login: { subtitle: 'System Admin · Config Center', admin_account: 'Admin account', password: 'Password', login_btn: 'Sign In', enter_credentials: 'Please enter account and password', back_to_app: '← Back to app' },
        nav: { config_center: 'Config Center', credentials: 'Credentials', user_management: 'User Management', user_list: 'Users', whitelist: 'Signup Whitelist', login_whitelist: 'Login Whitelist', change_password: 'Change Password', logout: 'Sign Out', channels: 'OAuth Channels', platforms: 'Repo Platforms' },
        page: {
          bots_all_inactive: "all bots inactive (falls back to your PAT)",

          bots_none_short: "no bot (falls back to your PAT)",

          bot_username: "Bot username",
          bot_token: "Bot token (encrypted at rest)",
          bots_hint: "One bot per platform; leave the token blank to keep it. Without a bot, hosting falls back to the user's PAT",
          bot_none: "(no bot account configured)",
          self_domain: "This platform (same-domain, usable without a pair)",

          channels_list_sub: "Login channels handle OAuth login and binding. Tap a row for credentials and the repo platforms it drives.",
          platforms_list_sub: "Repo platforms handle hosting and proxy behaviour. Tap a row for definition fields, bot accounts and token health.",
          sec_state: "State",
          sec_credential: "Credentials",
          sec_definition: "Definition fields (read-only · from config)",
          keep_hint: "Leave blank to keep the current value",

          back: "Back to list",
          col_name: "Name",
          col_provider: "Key",
          col_enabled: "Status",
          col_health: "Bot token health",
          col_drive: "Repo platforms it drives",
          col_bots: "Bot accounts",
          channels_empty: "No OAuth login channel registered yet (they appear after config import)",
          platforms_empty: "No repo platform registered yet (they appear after config import)",
          domain_note_channel: "Login channel card: OAuth login/binding credentials, decoupled from repo platforms",
          domain_note_platform: "Hosting platform card: repo hosting and proxy behavior, decoupled from login channels",
          health_healthy: "healthy",
          health_degraded: "degraded",
          health_unhealthy: "unhealthy",
          bot_username_ph: "bot username",
          bot_token_ph: "bot token (encrypted at rest)",
          bot_save_token: "Rotate with the token above",
          bot_need_username: "bot username is required",
          bot_add_failed: "Add bot failed",
          bot_save_failed: "Save token failed",
          bot_del_failed: "Delete bot failed",
          bot_clear: "Clear token",
          bot_save: "Save",
          cancel: "Cancel",
          disable_confirm_title: "Turn this platform off",
          disable_confirm: "Turning off {provider} hides it from users entirely: {repos} repositories, {users} users and {managed} hosted repositories stop being listed (platform credentials of {pats} users are kept). Continue?",
          disable_confirm_btn: "Turn off",

          channels_title: "OAuth Channels",
          values_label: "Values: ",
          platforms_title: "Code Hosting Platforms",
          channels_hint: "Definition fields come from the config file (read-only). This page writes credentials and the enable switch only.",
          platforms_hint: "Dialect/endpoints/capabilities come from platform cards and repo-credentials.json (read-only). This page writes the enable switch and bot accounts only.",
          readonly_tag: "Read-only · from config",
          enabled: "Enabled",
          save_creds: "Save credentials",
          save: "Save",
          configured_keep: "configured (leave empty to keep)",
          config_overridden: "This platform is defined by its config file; this change takes effect as an admin override: {fields}. Restore the file value to drop the override.",
          card_taken_over: "This platform card is now taken over by the admin console (stored in DB): later config-file changes no longer apply automatically.",
          card_taken_over_fields: "; fields changed this time: {fields}",
          config_same: "This platform is defined by its config file; this submission matches the file, so no override was recorded.",
          add_card: "Add a platform card",
          add_card_desc: "Create a card for a self-hosted instance (e.g. gitlab-corp): clone a card of the same family, then change the id, host and API base",
          add_from: "Based on",
          add_id_ph: "gitlab-corp (lowercase letter first; letters, digits, underscore or dash)",
          add_name: "Display name",
          add_bad_id: "Invalid provider id: start with a lowercase letter; letters, digits, underscore or dash only; 40 chars max",
          add_exists: "That provider id already exists",
          add_no_template: "No template card available for the selected platform",
          sec_card: "Platform card (advanced)",
          card_what: "Full definition of the platform card: private CA (ca_bundle, absolute PEM path, empty = system CA), PAT creation URL, download base and storage fields. The private CA only matters for instances whose HTTPS is validated.",
          card_note: "Saved as a whole card, so fields not shown here are preserved. Verify the instance before changing host/api_base; for platforms defined by a config file the change is stored as an admin override.",
          ca_ph: "/path/to/corp-ca.pem (empty = system CA)",
          edit: "Edit",
          card_missing: "No platform card for this platform (table projection only); advanced settings are read-only",
          not_configured: "not configured",
          loading: "Loading…",
          load_failed: "Failed to load",
          save_failed: "Failed to save",
          toggle_failed: "Failed to toggle",
          requires_scope: "Required scope",
          no_scope_check: "(not enforced)",
          bot_support: "bot",
          bot_supported: "supported",
          bot_unsupported: "not supported",
          bot_accounts: "Bot",
          bot_default: "default",
          bot_normal: "regular",
          bot_none: "(no bot account; hosted actions fall back to user PAT)",
          empty_val: "(empty)",
          need_one: "Fill at least one of client_id / client_secret",
          client_id: "client_id",
          client_secret: "client_secret",
          page_help_title: "What you can change here",
          no_doc: "No description yet (add to i18n or config _fields)",
          bot_no: "not supported",
          unset: "not configured",
          channels_note: "⚠ Edit configs/oauth-providers.json then restart or re-import.",
          bot_yes: "supported",
          scope_none: "(not enforced)",
          switch: "Enable",
          pill_on: "enabled",
          platforms_note: "⚠ Edit config files and re-import; 'enabled' is DB-owned and never overridden by config.",
          bots_note: "⚠ Responses never contain token values; repo-level bindings win over the default account.",
          help_title: "What you can change here",
          pill_off: "disabled",
          keep: "configured (leave empty to keep)",
          scope_need: "Required scope",
          empty: "(empty)",
          bots_what: "Operating identity and its token (encrypted). At most one is_default per platform.",
          bots: "Bot accounts",
          bot: "bot",
        },
          field: {
          type: {"what": "OAuth protocol mode.", "values": "oauth2 | oidc | saml", "note": "⚠ Only oidc has id_token claims fallback."},
          auth_url: {"what": "Authorization page URL.", "values": "https://…/authorize", "note": "All three endpoints are required; a card missing one is rejected."},
          token_url: {"what": "Code-to-token endpoint.", "values": "https://…/token", "note": "Wrong endpoint shows up as token exchange failure."},
          user_url: {"what": "User info endpoint.", "values": "https://…/user (may contain {access_token})", "note": "⚠ Huawei needs the token as a query parameter."},
          emails_endpoint: {"what": "Optional email endpoint.", "values": "https://…/emails or empty", "note": "Empty means this channel provides no email."},
          scope: {"what": "Scopes requested at login (least privilege).", "values": "space separated, e.g. user_info / repo", "note": "⚠ Changing it affects every link of this channel; Gitee requires the exact scope set and a matching previous grant; insufficient scope makes repo calls return SCOPE_INSUFFICIENT."},
          pkce_enabled: {"what": "PKCE toggle.", "values": "true | false", "note": "⚠ Gitee must keep this false, otherwise the consent page shows every login."},
          id_field: {"what": "Field holding the unique id.", "values": "e.g. id / openID", "note": "⚠ Huawei only exposes openID."},
          login_field: {"what": "Field used as login name.", "values": "e.g. login / preferred_username", "note": "Falls back to external id when missing."},
          name_field: {"what": "Field used as display name.", "values": "e.g. name / nickname", "note": "Empty falls back to the login name."},
          avatar_field: {"what": "Field holding avatar URL.", "values": "e.g. avatar_url", "note": "Empty uses the platform default rule."},
          redirect_uri: {"what": "OAuth callback URL.", "values": "https://…/api/auth/callback/<provider> or empty", "note": "Empty derives from server_base_url; a wrong value gives redirect_uri_mismatch."},
          accept_header: {"what": "Accept header for user info.", "values": "e.g. application/vnd.github+json", "note": "GitHub returns legacy fields without it."},
          repo_links: {"what": "Code hosting platforms this channel may access (read-only).", "values": "[{\"repo_provider\":\"…\",\"relation\":\"identity_only|identity_and_credentials\"}]", "note": "A configured pair lets the login channel's token access the platform (login writes the user-platform relation). No tier field exists; deleting the pair removes that access."},
          bot_username: {"what": "The single bot account of this platform.", "values": "platform login or empty", "note": "⚠ The platform row is the source of truth; the token lives in bot_token_enc (encrypted)."},
          enabled: {"what": "Whether the object is enabled (DB authoritative).", "values": "true | false", "note": "⚠ Config files never override it; import never touches this column."},
          dialect: {"what": "API dialect family.", "values": "github | gitlab", "note": "⚠ Same family covers several platforms (gitcode/gitee/github all use github)."},
          host: {"what": "Web host.", "values": "e.g. gitee.com", "note": "Do not put the API base here."},
          api_base: {"what": "API base URL.", "values": "e.g. https://gitee.com/api/v5", "note": "Do not put the web host here."},
          api_auth: {"what": "How the token is sent.", "values": "query_token | token_header | private_token", "note": "Wrong mode shows up as 401."},
          webhook_path: {"what": "Webhook receiving path.", "values": "/webhook/<provider>", "note": "Must match the platform webhook config."},
          event_header: {"what": "Event header name.", "values": "e.g. X-GitHub-Event", "note": "Used to dispatch webhook handlers."},
          webhook_verify: {"what": "Webhook verification mode.", "values": "hmac | token_compare", "note": "⚠ hmac verifies a signature; token_compare compares a URL token."},
          webhook_dialect: {"what": "Webhook payload family.", "values": "github_web | gitee_v5", "note": "⚠ Must be declared; hosting is fail-closed when missing."},
          clone_auth: {"what": "Clone/push credential form.", "values": "basic_password | x_access_token | oauth2", "note": "Affects git remote URL building."},
          issue_write_form: {"what": "Issue creation endpoint form.", "values": "repo_path | owner_param", "note": "⚠ Gitee needs owner_param, otherwise 404."},
          branch_create_form: {"what": "Branch creation form.", "values": "form | refs_sha", "note": "Platform-specific request body."},
          pr_head_filter: {"what": "PR list head filter form.", "values": "owner_ref | bare_ref", "note": "⚠ Wrong form returns an empty list silently."},
          issue_search: {"what": "Whether issue search is supported.", "values": "true | false", "note": "Drives the issue picker."},
          git_tree_family: {"what": "Git tree API family.", "values": "v5 | github_api", "note": "Platform-specific tree endpoint."},          client_id: {"what": "OAuth app client_id (encrypted column, only a boolean is returned).", "values": "copy from the platform app page", "note": "⚠ Empty keeps the current value; plaintext is never readable again."},
          client_id: {"what": "OAuth app client_id (encrypted column, only a boolean is returned).", "values": "copy from the platform app page", "note": "⚠ Empty keeps the current value; plaintext is never readable again."},
          client_secret: {"what": "OAuth app client_secret (encrypted column, only a boolean is returned).", "values": "copy from the platform app page", "note": "⚠ Empty keeps the current value; fill the new one to rotate."},

          },
      }
    }
  };

  // ── detectLang(): detect language from URL pathname or localStorage ──
  // 语言状态传递机制（Issue #275 文档化结论）：
  //   优先级 = URL /en/ 前缀（权威，刷新/分享安全）→ localStorage grapeLang（工作台自身
  //   状态）→ localStorage grape-lang（官网首页写入的偏好，跨页面桥接）→ 默认 zh。
  //   /en/ 前缀不是语言切换的必要条件：SPA 会话内 _currentLang 由本函数的 localStorage
  //   fallback 维护，URL 无前缀也能正常显示英文；但 F5 刷新后内存状态丢失，会按本
  //   优先级链重新检测——URL 无前缀且 grapeLang 无值时回退默认中文（#275 用户补充现象）。
  //   官网首页与工作台是两个独立页面，首页用 grape-lang key、工作台用 grapeLang key；
  //   grapeLang 未设置时兼容读取首页的 grape-lang，使「进入 Grape」继承首页语言选择。
  //   仅读取不回写：init() 会把检测结果双写 cookie（grapeLang），工作台内 setLang()
  //   正常写 grapeLang——一旦用户在工作台内切换过语言，grapeLang 有值，桥接不再参与
  //   判定，两页面互不干扰。
  function detectLang() {
    var path = window.location.pathname || '';
    // /en/ or /en at start → en
    if (path === '/en' || path.indexOf('/en/') === 0) return 'en';
    // No URL prefix → check localStorage
    try {
      var stored = localStorage.getItem(STORAGE_KEY);
      if (stored && SUPPORTED.indexOf(stored) >= 0) return stored;
      // Issue #275: 官网首页用 grape-lang key——grapeLang 未设置时兼容读取（桥接，
      // 仅初始化时判定，不回写），首页切换语言后新打开的工作台继承该选择
      var siteLang = localStorage.getItem('grape-lang');
      if (siteLang && SUPPORTED.indexOf(siteLang) >= 0) return siteLang;
    } catch (e) {}
    return DEFAULT_LANG;
  }

  // ── loadPacks(lang): async fetch 15 JSON packs for a language ──
  // 注意：hotfix简化实现，部分包失败时标记语言未加载
  // 用户切换到其他语言再切回来时重拉全部15个包；或点击当前语言按钮触发重试（selectLang 早退判断检查 isLangLoaded）
  // 不做单包增量重试；存在极端场景：某个包永久404会反复触发全量请求，后续迭代可优化
  function loadPacks(lang) {
    if (!SUPPORTED.indexOf || SUPPORTED.indexOf(lang) < 0) lang = DEFAULT_LANG;
    if (_loadedLangs[lang]) return Promise.resolve({failed: 0, total: 0, cached: true});

    var base = '/static/locales/' + lang + '/';
    var ver = _cacheVer ? '?v=' + _cacheVer : '';
    var failedCount = 0;
    var total = MODULES.length;

    var promises = MODULES.map(function (mod) {
      // 每个 fetch 加 15 秒超时——弱网下不让单个请求无限挂起
      var controller = new AbortController();
      var timer = setTimeout(function() {
        controller.abort();
      }, 15000);

      return fetch(base + mod + '.json' + ver, { signal: controller.signal })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + mod);
          return r.json();
        })
        .then(function (data) {
          if (!_packs[lang]) _packs[lang] = {};
          _packs[lang][mod] = data;
        })
        .catch(function (e) {
          // 覆盖所有失败路径：fetch reject(网络错误)、!r.ok(HTTP 4xx/5xx)、r.json() parse 失败、超时 abort
          failedCount++;
          console.warn('[i18n] Failed to load pack: ' + lang + '/' + mod + '.json', e.message);
        })
        .finally(function () {
          // 无论成功或失败，都清理定时器——与 api.js _fetchWithTimeout 模式一致
          clearTimeout(timer);
        });
    });

    return Promise.all(promises).then(function () {
      // 只有全部成功才标记为已加载——部分失败保持 false，下次切回会重试缺失包
      if (failedCount === 0) {
        _loadedLangs[lang] = true;
      }
      // Re-apply DOM translations after full packs load
      applyI18nToDOM();
      if (failedCount > 0) {
        console.warn('[i18n] lang=' + lang + ' load packs partial failed ' + failedCount + '/' + total);
      }
      return {failed: failedCount, total: total};
    });
  }

  // ── _lookup(pack, key): dot-separated key lookup in a pack object ──
  function _lookup(pack, key) {
    if (!pack || !key) return undefined;
    var parts = key.split('.');
    var cur = pack;
    for (var i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== 'object') return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  // ── _interpolate(str, vars): replace {name} and {{name}} placeholders ──
  function _interpolate(str, vars) {
    if (!str || typeof str !== 'string' || !vars) return str;
    return str.replace(/\{\{?(\w+)\}?\}/g, function (match, name) {
      return vars.hasOwnProperty(name) ? String(vars[name]) : match;
    });
  }

  // ── t(key, vars, fallback): translate a key with optional variable interpolation ──
  // Lookup order: current lang pack → default lang pack → fallback → key itself
  function t(key, vars, fallback) {
    var val;

    // Try current language
    if (_packs[_currentLang]) {
      val = _lookup(_packs[_currentLang], key);
    }

    // Fallback to default language if not found
    if (val === undefined && _currentLang !== DEFAULT_LANG && _packs[DEFAULT_LANG]) {
      val = _lookup(_packs[DEFAULT_LANG], key);
    }

    // Not found: return fallback (if provided) or key itself
    if (val === undefined || val === null) {
      return fallback !== undefined ? fallback : key;
    }

    // Interpolate variables
    return _interpolate(val, vars);
  }

  // ── setLang(lang): switch language in-place (no page reload) ──
  function setLang(lang) {
    // 通知 Vue 组件重渲染（switch in-place，组件内文案需重新取值）
    try { window.dispatchEvent(new CustomEvent('grape:langchange', { detail: { lang: lang } })); } catch (e) {}

    if (SUPPORTED.indexOf(lang) < 0) lang = DEFAULT_LANG;
    if (lang === _currentLang && _loadedLangs[lang]) return Promise.resolve({failed: 0, total: 0, skipped: true});

    // 记录发起切换时的 view——loadPacks 完成后若用户已导航到其他页面，跳过整页重建
    // 仅做 applyI18nToDOM 更新 data-i18n 元素，不打断用户在新页面的操作
    var prevView = (window.history.state && window.history.state.view) || '';
    var prevHash = window.location.hash || '';

    // 状态变更（_currentLang/localStorage/cookie/URL）放在 loadPacks 成功之后
    // 从源头避免"状态已改、视图未改"的中间态——全部失败时无需回滚
    return loadPacks(lang).then(function (result) {
      // 全部成功(failed<total)、部分成功(failed<total)、缓存命中(cached) 都提交状态
      // 全部失败(failed===total && !cached) 不提交
      if (result.failed < result.total || result.cached) {
        _currentLang = lang;
        try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) {}
        _setCookie(STORAGE_KEY, lang, 31536000, '/');
        _updateUrlLang(lang);
        applyI18nToDOM();   // 含 document.documentElement.lang 更新
        // 语言包已就位：再派发一次，让 Vue 组件（AdminView / AdminChannels / AdminPlatforms 等
        // 用 langVer 计数的视图）在"新包可用"之后重渲染。开头那次派发发生在 loadPacks 之前，
        // 组件拿到的是旧包 → 表现为"切了语言但页面不变"（2026-11-14 实锤：zh↔en 单向不生效）。
        try { window.dispatchEvent(new CustomEvent('grape:langchange', { detail: { lang: lang } })); } catch (e) {}

        // 检查用户是否在 loadPacks 期间导航到其他页面
        var curView = (window.history.state && window.history.state.view) || '';
        var curHash = window.location.hash || '';
        if (curView === prevView && curHash === prevHash) {
          // 仍在原页面——安全重渲染
          var rendered = _rerenderCurrentPage();
          if (!rendered) {
            // rerender 失败——DOM 未更新，状态已提交（_currentLang 已切换）
            // 返回部分失败而非全部成功，让 selectLang 弹警告而非成功 Toast
            return {failed: 1, total: result.total, rerenderFailed: true};
          }
        }
        // else: 用户已导航——跳过 _rerenderCurrentPage，不打断新页面操作
        // 语言切换已通过 applyI18nToDOM 更新了 data-i18n 元素，新页面下次渲染时自然用新语言
      }
      // 全部失败：不提交任何状态变更，_currentLang/localStorage/cookie/URL 保持原值
      // DOM 不重建，旧按钮仍禁用，selectLang 手动恢复
      return result;
    });
  }

  // ── _updateUrlLang(lang): adjust URL prefix without reload ──
  // Skip URL prefix change on /admin (independent page, no /en/ prefix routing)
  function _updateUrlLang(lang) {
    var path = window.location.pathname || '/';
    // Admin page uses localStorage + applyI18nToDOM only — no URL prefix
    if (path === '/admin' || path.indexOf('/admin/') === 0) return;
    var hash = window.location.hash || '';
    var search = window.location.search || '';

    if (lang === 'en') {
      if (path !== '/en' && path.indexOf('/en/') !== 0) {
        path = '/en' + (path === '/' ? '' : path);
      }
    } else {
      if (path === '/en') {
        path = '/';
      } else if (path.indexOf('/en/') === 0) {
        path = path.substring(3);
      }
    }

    try {
      window.history.replaceState(window.history.state, '', path + search + hash);
    } catch (e) {}
  }

  // ── _rerenderCurrentPage(): re-navigate to current view after lang switch ──
  // 返回 true 表示成功重渲染，false 表示渲染失败（异常被 try/catch 吞掉）
  function _rerenderCurrentPage() {
    // Recover current view from history state or hash
    var state = window.history.state;
    var view = null, param = null;
    if (state && state.view) {
      view = state.view;
      param = state.param;
    } else {
      var hash = window.location.hash;
      if (hash && hash.length > 1) {
        var parts = hash.substring(1).split('/');
        view = parts[0];
        param = parts[1] ? decodeURIComponent(parts[1]) : null;
      }
    }
    if (!view || typeof app === 'undefined' || !app.nav) return true;  // 无视图不算失败

    // For settings page, preserve the active sub-tab across re-render
    if (view === 'settings' && typeof renderSettings === 'function') {
      try {
        var activeTab = document.querySelector('.stabs a.ac');
        var tabId = activeTab ? (activeTab.getAttribute('onclick') || '').match(/'([^']+)'/) : null;
        var savedTab = tabId ? tabId[1] : null;
        renderSettings(savedTab || 'gitcode');
        return true;
      } catch (e) {
        console.warn('[i18n] _rerenderCurrentPage renderSettings failed:', e.message);
        return false;
      }
    }

    // For account page, preserve the active sub-tab across re-render
    if (view === 'account' && typeof renderAccount === 'function') {
      try {
        var acctTab = document.querySelector('.stabs a.ac');
        var acctTabId = acctTab ? (acctTab.getAttribute('onclick') || '').match(/'([^']+)'/) : null;
        var savedAcctTab = acctTabId ? acctTabId[1] : null;
        renderAccount(savedAcctTab || 'profile');
        return true;
      } catch (e) {
        console.warn('[i18n] _rerenderCurrentPage renderAccount failed:', e.message);
        return false;
      }
    }

    // For repos/lobby page, use cached data to re-render without API request
    // 引导页/项目列表页大量文案是 JS 动态拼接（I18N.t），applyI18nToDOM 无法更新
    // 用缓存数据重新调 renderRepoSelection，不发 API 请求、不闪烁 loading 遮罩
    if (view === 'repos' && typeof app !== 'undefined' && app._rerenderReposView) {
      try {
        app._rerenderReposView();
        return true;
      } catch (e) {
        console.warn('[i18n] _rerenderCurrentPage _rerenderReposView failed:', e.message);
        return false;
      }
    }

    try { app.nav(view, param); return true; }
    catch (e) {
      console.warn('[i18n] _rerenderCurrentPage app.nav failed:', e.message);
      return false;
    }
  }

  // ── _setCookie(name, value, maxAge, path) ──
  function _setCookie(name, value, maxAge, path) {
    try {
      document.cookie = name + '=' + value +
        '; max-age=' + (maxAge || 31536000) +
        '; path=' + (path || '/') +
        '; SameSite=Lax';
    } catch (e) {}
  }

  // ── _applyUrlLang(lang): adjust URL prefix and reload ──
  // Skip on /admin (independent page, no /en/ prefix routing)
  function _applyUrlLang(lang) {
    var path = window.location.pathname || '/';
    // Admin page: no URL prefix change, just reload as-is
    if (path === '/admin' || path.indexOf('/admin/') === 0) return;
    var hash = window.location.hash || '';
    var search = window.location.search || '';

    if (lang === 'en') {
      // Add /en prefix (if not already present)
      if (path !== '/en' && path.indexOf('/en/') !== 0) {
        path = '/en' + (path === '/' ? '' : path);
      }
    } else {
      // Strip /en prefix for zh (default)
      if (path === '/en') {
        path = '/';
      } else if (path.indexOf('/en/') === 0) {
        path = path.substring(3); // remove '/en'
      }
    }

    // Reload with new URL (preserve hash + search)
    window.location.href = path + search + hash;
  }

  // ── getLang(): return current language ──
  function getLang() {
    return _currentLang;
  }

  // ── applyI18nToDOM(): set document lang + replace [data-i18n] elements ──
  function applyI18nToDOM() {
    // Set <html lang="...">
    var html = document.documentElement;
    if (html) html.setAttribute('lang', _currentLang === 'en' ? 'en' : 'zh-CN');

    // Replace text content for [data-i18n] elements
    var els = document.querySelectorAll('[data-i18n]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var key = el.getAttribute('data-i18n');
      if (!key) continue;
      // Support data-i18n-vars for JSON variable map
      var varsAttr = el.getAttribute('data-i18n-vars');
      var vars = null;
      if (varsAttr) {
        try { vars = JSON.parse(varsAttr); } catch (e) {}
      }
      var translated = t(key, vars);
      if (translated && translated !== key) {
        el.textContent = translated;
      }
    }

    // Support [data-i18n-placeholder] for input placeholders
    var phEls = document.querySelectorAll('[data-i18n-placeholder]');
    for (var j = 0; j < phEls.length; j++) {
      var phEl = phEls[j];
      var phKey = phEl.getAttribute('data-i18n-placeholder');
      if (!phKey) continue;
      var phTranslated = t(phKey);
      if (phTranslated && phTranslated !== phKey) {
        phEl.setAttribute('placeholder', phTranslated);
      }
    }

    // Support [data-i18n-title] for title attributes
    var titleEls = document.querySelectorAll('[data-i18n-title]');
    for (var k = 0; k < titleEls.length; k++) {
      var titleEl = titleEls[k];
      var titleKey = titleEl.getAttribute('data-i18n-title');
      if (!titleKey) continue;
      // 与 data-i18n 同款变量机制：title 也支持 data-i18n-vars JSON（登录按钮
      // 「使用 {name} 账号登录」即用此通道，无 vars 时维持纯键翻译）。
      var titleVarsAttr = titleEl.getAttribute('data-i18n-vars');
      var titleVars = null;
      if (titleVarsAttr) {
        try { titleVars = JSON.parse(titleVarsAttr); } catch (e) {}
      }
      var titleTranslated = t(titleKey, titleVars);
      if (titleTranslated && titleTranslated !== titleKey) {
        titleEl.setAttribute('title', titleTranslated);
        if (titleEl.hasAttribute('aria-label')) {
          titleEl.setAttribute('aria-label', titleTranslated);
        }
      }
    }

    // Remove i18n loading flag — translations applied, reveal text
    var html = document.documentElement;
    if (html) {
      // New: class-based (i18n-loading)
      html.classList.remove('i18n-loading');
      // Legacy: attribute-based (data-i18n-pending) — for backward compat
      if (html.hasAttribute('data-i18n-pending')) {
        html.removeAttribute('data-i18n-pending');
      }
    }
  }

  // ── _initialized: prevent duplicate init calls ──
  var _initialized = false;

  // ── _deepMerge(target, source): recursively merge source into target ──
  function _deepMerge(target, source) {
    for (var key in source) {
      if (!source.hasOwnProperty(key)) continue;
      if (typeof source[key] === 'object' && source[key] !== null && !Array.isArray(source[key])) {
        if (!target[key] || typeof target[key] !== 'object') target[key] = {};
        _deepMerge(target[key], source[key]);
      } else {
        target[key] = source[key];
      }
    }
  }

  // ── init(cacheVer): synchronous init with inline packs + boost, then async load full packs ──
  function init(cacheVer) {
    if (_initialized) return;
    _initialized = true;
    _cacheVer = cacheVer || '';

    // Detect language
    _currentLang = detectLang();

    // 1. Fill _packs with inline minimal packs (synchronous)
    _packs[DEFAULT_LANG] = _INLINE_PACKS[DEFAULT_LANG] || {};
    if (_currentLang !== DEFAULT_LANG) {
      _packs[_currentLang] = _INLINE_PACKS[_currentLang] || {};
    }
    _loadedLangs[DEFAULT_LANG] = false;
    _loadedLangs[_currentLang] = false;

    // 2. Merge server-injected boost data (synchronous, zero network delay)
    if (typeof window !== 'undefined' && window.__I18N_BOOST__) {
      var boost = window.__I18N_BOOST__;
      for (var lang in boost) {
        if (!boost.hasOwnProperty(lang)) continue;
        if (!_packs[lang]) _packs[lang] = {};
        _deepMerge(_packs[lang], boost[lang]);
      }
    }

    // 3. Apply DOM translations immediately (inline packs + boost cover critical UI)
    applyI18nToDOM();

    // 4. Async load full packs for current language (fills in keys not in boost)
    loadPacks(_currentLang).then(function () {
      // Re-apply after full packs load to cover keys not in boost
      applyI18nToDOM();
      // Also load default language packs for fallback (if different)
      if (_currentLang !== DEFAULT_LANG) {
        loadPacks(DEFAULT_LANG);
      }
    }).catch(function () {});

    // Set cookie to match detected language (ensures server-side consistency)
    _setCookie(STORAGE_KEY, _currentLang, 31536000, '/');
  }

  // ── isLangLoaded(lang): check if language packs are fully loaded ──
  function isLangLoaded(lang) {
    return !!_loadedLangs[lang];
  }

  // ── Public API ──
  return {
    t: t,
    setLang: setLang,
    getLang: getLang,
    init: init,
    detectLang: detectLang,
    applyI18nToDOM: applyI18nToDOM,
    isLangLoaded: isLangLoaded,
    SUPPORTED: SUPPORTED,
    DEFAULT_LANG: DEFAULT_LANG,
    // Expose for auto-init guard (read-only)
    get isInitialized() { return _initialized; }
  };
})();

// ── Auto-init for defer mode: if no one called init() yet, do it now ──
// defer 脚本在 DOM 解析完成后、DOMContentLoaded 前执行
// 此时 body 中所有 [data-i18n] 元素已解析，querySelectorAll 能找到
// 仅在 defer 模式下自动 init（页面设置了 __I18N_CACHE_VER__ 标志）
if (typeof I18N !== 'undefined' && I18N.init && typeof window !== 'undefined' && window.__I18N_CACHE_VER__) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      if (!I18N.isInitialized) I18N.init(window.__I18N_CACHE_VER__ || '');
    });
  } else {
    if (!I18N.isInitialized) I18N.init(window.__I18N_CACHE_VER__ || '');
  }
}
