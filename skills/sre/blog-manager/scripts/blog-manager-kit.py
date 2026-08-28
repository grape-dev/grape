#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""blog-manager-kit — Blog System REST API 管理 skill

能力（由 API 文档解析生成）:
  A. health-check — 检查 API 可达性
  B. list-articles — 分页查询文章列表
  C. create-article — 发布新文章
  D. get-article — 查询单篇文章详情（含评论）
  E. update-article — 更新文章
  F. delete-article — 删除文章（软删除，可恢复）
  G. hard-delete-article — 硬删除文章（不可逆，需二次确认）
  H. restore-article — 恢复软删除的文章
  I. top-articles — 获取热门文章 Top N
  J. list-labels — 获取所有标签
  K. create-label — 创建标签
  L. list-users — 获取用户列表
  M. create-user — 创建用户
  N. create-comment — 发表评论
  O. list-comments — 获取文章的评论列表
  P. delete-comment — 删除评论（软删除）
  Q. list-messages — 获取留言列表（含回复）
  R. create-message — 发表留言
  S. reply-message — 回复留言
  T. delete-message — 删除留言（软删除）
  U. list-moods — 获取说说列表
  V. create-mood — 发布说说
  W. delete-mood — 删除说说
  X. upload-single — 上传单个文件
  Y. upload-batch — 批量上传文件
  Z. list-uploads — 列出所有已上传文件
  AA. delete-upload — 删除已上传文件
  AB. capability-list — 列出本 skill 所有能力项

认证: none（公开 API，无认证）
退出码: 0=成功; 2=参数错误; 3=缺少配置（地址）; 4=API 调用失败
"""

import argparse
import json
import os
import sys


# ---------------------------------------------------------------------------
# Credentials（前缀由 skill name 推导，认证方式为 none）
# ---------------------------------------------------------------------------

_CRED_PREFIX = "BLOG_MANAGER_KIT"  # skill name 转大写下划线
_AUTH_TYPE = "none"                # 无认证（公开 API）
_DEFAULT_BASE_URL = "http://123.249.19.227:18080"  # 默认地址（Spec 指定）


def _load_credentials():
    """获取配置——4 级优先级（与 base_url 一致）。

    1. 项目知识：递归扫描 .project-info/ 下所有 JSON 文件（config.{PREFIX}_BASE_URL）
    2. 环境变量：扫描 {PREFIX}_* 开头变量（项目知识缺失时回退）
    3. 当前上下文：A2A context 已注入的环境变量（已包含在步骤 2）
    4. 默认值：以上都无时使用默认地址
    """
    creds = {}
    # 1. 项目知识优先
    creds.update(_load_from_project_knowledge())
    # 2. 环境变量回退：项目知识缺失的字段从环境变量补充
    for k, v in os.environ.items():
        u = k.upper()
        if u.startswith(_CRED_PREFIX):
            if 'BASE_URL' in u:
                creds.setdefault('base_url', v)
    return creds


def _load_from_project_knowledge():
    """从 .project-info/ 目录递归查找 JSON 配置文件，读取 config.{PREFIX}_BASE_URL 字段。

    不固定文件名——扫描所有 .json 文件，检查是否含 config 字段。
    """
    import glob
    creds = {}
    for pattern in ['.project-info/**/*.json',
                    '../.project-info/**/*.json',
                    '../../.project-info/**/*.json']:
        for filepath in glob.glob(pattern, recursive=True):
            try:
                with open(filepath) as f:
                    data = json.load(f)
                config = data.get('config', {})
                prefix = _CRED_PREFIX + '_'
                for key, val in config.items():
                    if key.upper().startswith(prefix) and 'BASE_URL' in key.upper():
                        creds.setdefault('base_url', val)
            except Exception:
                continue
    return creds


def _get_base_url():
    """获取 API base URL——4 级优先级（与凭据读取一致）。"""
    creds = _load_credentials()
    base_url = creds.get('base_url', '').rstrip('/')
    if not base_url:
        # 无认证模式：环境变量/项目知识缺失时使用默认地址（Spec 指定）
        base_url = _DEFAULT_BASE_URL
    return base_url


# ---------------------------------------------------------------------------
# API client（无认证：公开 API）
# ---------------------------------------------------------------------------

def _build_auth(creds):
    """根据认证方式构建请求认证参数（无认证返回空 dict）。"""
    if _AUTH_TYPE == "none":
        return {}
    print("错误：未知认证方式 %s" % _AUTH_TYPE, file=sys.stderr)
    sys.exit(2)


def _api_request(method, path, payload=None, params=None, files=None):
    """调用目标系统 REST API。"""
    import requests

    base_url = _get_base_url()
    creds = _load_credentials()
    auth_result = _build_auth(creds)
    auth_kwargs = auth_result
    url = f"{base_url}{path}"

    try:
        if method == 'GET':
            resp = requests.get(url, params=params, timeout=30, **auth_kwargs)
        elif method == 'POST':
            resp = requests.post(url, json=payload, files=files, timeout=30, **auth_kwargs)
        elif method == 'PUT':
            resp = requests.put(url, json=payload, files=files, timeout=30, **auth_kwargs)
        elif method == 'PATCH':
            resp = requests.patch(url, json=payload, timeout=30, **auth_kwargs)
        elif method == 'DELETE':
            resp = requests.delete(url, params=params, timeout=30, **auth_kwargs)
        else:
            print(f"错误：不支持的方法 {method}", file=sys.stderr)
            sys.exit(2)

        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}
    except requests.exceptions.HTTPError as e:
        print(f"错误：API 调用失败 {resp.status_code}: {e}", file=sys.stderr)
        sys.exit(4)
    except requests.exceptions.RequestException as e:
        print(f"错误：网络请求失败: {e}", file=sys.stderr)
        sys.exit(4)
    except ValueError:
        print("错误：API 返回非 JSON 格式（可能返回 HTML 错误页）", file=sys.stderr)
        sys.exit(4)


# ---------------------------------------------------------------------------
# Subcommand implementations（由 API 文档解析动态生成）
# ---------------------------------------------------------------------------

def cmd_health_check(args):
    """能力 A：检查 API 可达性。"""
    # GET /health
    return _api_request('GET', '/health')


def cmd_list_articles(args):
    """能力 B：分页查询文章列表。"""
    # GET /api/articles
    params = {'page': args.page, 'size': args.size, 'lid': args.lid}
    if args.keyword is not None:
        params['keyword'] = args.keyword
    return _api_request('GET', '/api/articles', params=params)


def cmd_create_article(args):
    """能力 C：发布新文章。"""
    # POST /api/articles
    payload = {'title': args.title, 'content': args.content,
               'uid': args.uid, 'lid': args.lid, 'heat': args.heat}
    if args.img is not None:
        payload['img'] = args.img
    return _api_request('POST', '/api/articles', payload=payload)


def cmd_get_article(args):
    """能力 D：查询单篇文章详情（含评论）。"""
    # GET /api/articles/{article_id}
    return _api_request('GET', f'/api/articles/{args.article_id}')


def cmd_update_article(args):
    """能力 E：更新文章。"""
    # PUT /api/articles/{article_id}
    payload = {}
    if args.title is not None:
        payload['title'] = args.title
    if args.content is not None:
        payload['content'] = args.content
    if args.lid is not None:
        payload['lid'] = args.lid
    if args.img is not None:
        payload['img'] = args.img
    if args.heat is not None:
        payload['heat'] = args.heat
    return _api_request('PUT', f'/api/articles/{args.article_id}', payload=payload)


def cmd_delete_article(args):
    """能力 F：删除文章（软删除，可恢复）。"""
    # DELETE /api/articles/{article_id}?soft=true
    return _api_request('DELETE', f'/api/articles/{args.article_id}',
                        params={'soft': True})


def cmd_hard_delete_article(args):
    """能力 G：硬删除文章（不可逆，需二次确认）。"""
    # DELETE /api/articles/{article_id}?soft=false
    if not args.yes:
        print("⚠️  警告：硬删除不可逆，文章将被永久删除且无法恢复！", file=sys.stderr)
        print(f"     目标文章 ID：{args.article_id}", file=sys.stderr)
        try:
            answer = input("确认执行硬删除？输入 yes 继续，其他任意输入取消: ").strip().lower()
        except EOFError:
            answer = ''
        if answer != 'yes':
            print("操作已取消，未执行硬删除。", file=sys.stderr)
            sys.exit(2)
    return _api_request('DELETE', f'/api/articles/{args.article_id}',
                        params={'soft': False})


def cmd_restore_article(args):
    """能力 H：恢复软删除的文章。"""
    # POST /api/articles/{article_id}/restore
    return _api_request('POST', f'/api/articles/{args.article_id}/restore')


def cmd_top_articles(args):
    """能力 I：获取热门文章 Top N。"""
    # GET /api/articles/heat/top
    return _api_request('GET', '/api/articles/heat/top', params={'limit': args.limit})


def cmd_list_labels(args):
    """能力 J：获取所有标签。"""
    # GET /api/lables（API 路径拼写为 lables，子命令用正确拼写 labels）
    return _api_request('GET', '/api/lables')


def cmd_create_label(args):
    """能力 K：创建标签。"""
    # POST /api/lables（API 路径拼写为 lables，子命令用正确拼写 labels）
    payload = {'lname': args.lname}
    return _api_request('POST', '/api/lables', payload=payload)


def cmd_list_users(args):
    """能力 L：获取用户列表。"""
    # GET /api/users
    return _api_request('GET', '/api/users')


def cmd_create_user(args):
    """能力 M：创建用户。"""
    # POST /api/users
    payload = {'uname': args.uname}
    if args.phone is not None:
        payload['phone'] = args.phone
    if args.pwd is not None:
        payload['pwd'] = args.pwd
    if args.email is not None:
        payload['email'] = args.email
    if args.img is not None:
        payload['img'] = args.img
    return _api_request('POST', '/api/users', payload=payload)


def cmd_create_comment(args):
    """能力 N：发表评论。"""
    # POST /api/comments
    payload = {'uid': args.uid, 'aid': args.aid, 'content': args.content}
    return _api_request('POST', '/api/comments', payload=payload)


def cmd_list_comments(args):
    """能力 O：获取文章的评论列表。"""
    # GET /api/comments/{aid}
    return _api_request('GET', f'/api/comments/{args.aid}')


def cmd_delete_comment(args):
    """能力 P：删除评论（软删除）。"""
    # DELETE /api/comments/{comment_id}
    return _api_request('DELETE', f'/api/comments/{args.comment_id}')


def cmd_list_messages(args):
    """能力 Q：获取留言列表（含回复）。"""
    # GET /api/messages
    return _api_request('GET', '/api/messages')


def cmd_create_message(args):
    """能力 R：发表留言。"""
    # POST /api/messages
    payload = {'uid': args.uid, 'content': args.content}
    return _api_request('POST', '/api/messages', payload=payload)


def cmd_reply_message(args):
    """能力 S：回复留言。"""
    # POST /api/messages/reply
    payload = {'uid': args.uid, 'mid': args.mid, 'content': args.content}
    return _api_request('POST', '/api/messages/reply', payload=payload)


def cmd_delete_message(args):
    """能力 T：删除留言（软删除）。"""
    # DELETE /api/messages/{message_id}
    return _api_request('DELETE', f'/api/messages/{args.message_id}')


def cmd_list_moods(args):
    """能力 U：获取说说列表。"""
    # GET /api/moods
    return _api_request('GET', '/api/moods')


def cmd_create_mood(args):
    """能力 V：发布说说。"""
    # POST /api/moods
    payload = {'content': args.content}
    if args.title is not None:
        payload['title'] = args.title
    if args.src is not None:
        payload['src'] = args.src
    return _api_request('POST', '/api/moods', payload=payload)


def cmd_delete_mood(args):
    """能力 W：删除说说。"""
    # DELETE /api/moods/{mood_id}
    return _api_request('DELETE', f'/api/moods/{args.mood_id}')


def cmd_upload_single(args):
    """能力 X：上传单个文件。"""
    # POST /api/upload (multipart/form-data, 字段名 file)
    with open(args.file, 'rb') as f:
        return _api_request('POST', '/api/upload', files={'file': f})


def cmd_upload_batch(args):
    """能力 Y：批量上传文件。"""
    # POST /api/upload/multiple (multipart/form-data, 字段名 files)
    files = [open(fp, 'rb') for fp in args.files]
    try:
        return _api_request('POST', '/api/upload/multiple',
                            files=[('files', f) for f in files])
    finally:
        for f in files:
            f.close()


def cmd_list_uploads(args):
    """能力 Z：列出所有已上传文件。"""
    # GET /api/uploads/list
    return _api_request('GET', '/api/uploads/list')


def cmd_delete_upload(args):
    """能力 AA：删除已上传文件。"""
    # DELETE /api/uploads/{filename}
    return _api_request('DELETE', f'/api/uploads/{args.filename}')


# ---------------------------------------------------------------------------
# Capability list
# ---------------------------------------------------------------------------

def cmd_capability_list(args):
    """能力 AB：列出本 skill 所有能力项。"""
    return {
        'capability': 'capability-list',
        'skill': 'blog-manager-kit',
        'version': '0.1.0',
        'subcommand_count': 28,
        'capabilities': [
            {'name': 'health-check', 'description': '检查 API 可达性', 'command': 'health-check'},
            {'name': 'list-articles', 'description': '分页查询文章列表', 'command': 'list-articles --page 1 --size 10'},
            {'name': 'create-article', 'description': '发布新文章', 'command': 'create-article --title <t> --content <c>'},
            {'name': 'get-article', 'description': '查询单篇文章详情（含评论）', 'command': 'get-article --article-id <id>'},
            {'name': 'update-article', 'description': '更新文章', 'command': 'update-article --article-id <id> [--title <t>]'},
            {'name': 'delete-article', 'description': '删除文章（软删除，可恢复）', 'command': 'delete-article --article-id <id>'},
            {'name': 'hard-delete-article', 'description': '硬删除文章（不可逆，需二次确认）', 'command': 'hard-delete-article --article-id <id> [--yes]'},
            {'name': 'restore-article', 'description': '恢复软删除的文章', 'command': 'restore-article --article-id <id>'},
            {'name': 'top-articles', 'description': '获取热门文章 Top N', 'command': 'top-articles [--limit 5]'},
            {'name': 'list-labels', 'description': '获取所有标签', 'command': 'list-labels'},
            {'name': 'create-label', 'description': '创建标签', 'command': 'create-label --lname <name>'},
            {'name': 'list-users', 'description': '获取用户列表', 'command': 'list-users'},
            {'name': 'create-user', 'description': '创建用户', 'command': 'create-user --uname <name>'},
            {'name': 'create-comment', 'description': '发表评论', 'command': 'create-comment --uid <id> --aid <id> --content <c>'},
            {'name': 'list-comments', 'description': '获取文章的评论列表', 'command': 'list-comments --aid <id>'},
            {'name': 'delete-comment', 'description': '删除评论（软删除）', 'command': 'delete-comment --comment-id <id>'},
            {'name': 'list-messages', 'description': '获取留言列表（含回复）', 'command': 'list-messages'},
            {'name': 'create-message', 'description': '发表留言', 'command': 'create-message --uid <id> --content <c>'},
            {'name': 'reply-message', 'description': '回复留言', 'command': 'reply-message --uid <id> --mid <id> --content <c>'},
            {'name': 'delete-message', 'description': '删除留言（软删除）', 'command': 'delete-message --message-id <id>'},
            {'name': 'list-moods', 'description': '获取说说列表', 'command': 'list-moods'},
            {'name': 'create-mood', 'description': '发布说说', 'command': 'create-mood --content <c> [--title <t>]'},
            {'name': 'delete-mood', 'description': '删除说说', 'command': 'delete-mood --mood-id <id>'},
            {'name': 'upload-single', 'description': '上传单个文件', 'command': 'upload-single --file <path>'},
            {'name': 'upload-batch', 'description': '批量上传文件', 'command': 'upload-batch --files <p1> <p2> ...'},
            {'name': 'list-uploads', 'description': '列出所有已上传文件', 'command': 'list-uploads'},
            {'name': 'delete-upload', 'description': '删除已上传文件', 'command': 'delete-upload --filename <name>'},
            {'name': 'capability-list', 'description': '列出本 skill 所有能力项', 'command': 'capability-list'},
        ],
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_md(payload):
    """将 JSON 输出转为 Markdown 表格（可选）。"""
    cap = payload.get('capability', '')
    if cap == 'capability-list':
        lines = [f"## 能力清单（{payload.get('skill', '')} v{payload.get('version', '')}）", "",
                 "| 能力 | 说明 | 命令 |", "|---|---|---|"]
        for c in payload.get('capabilities', []):
            lines.append(f"| {c['name']} | {c['description']} | `{c['command']}` |")
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog='blog-manager-kit',
        description='Blog System REST API 管理 skill（无认证）')

    def add_common_args(p):
        p.add_argument('--format', choices=['json', 'md'], default='json',
                       help='输出格式，默认 json')

    sub = parser.add_subparsers(dest='command', help='能力命令')

    # health-check
    p_hc = sub.add_parser('health-check', help='检查 API 可达性')
    add_common_args(p_hc)

    # list-articles
    p_la = sub.add_parser('list-articles', help='分页查询文章列表')
    p_la.add_argument('--page', type=int, default=1, help='页码（默认 1）')
    p_la.add_argument('--size', type=int, default=10, help='每页数量（默认 10，最大 100）')
    p_la.add_argument('--lid', type=int, default=0, help='标签 ID 过滤（默认 0=不过滤）')
    p_la.add_argument('--keyword', default='', help='关键词搜索')
    add_common_args(p_la)

    # create-article
    p_ca = sub.add_parser('create-article', help='发布新文章')
    p_ca.add_argument('--title', required=True, help='文章标题（必填）')
    p_ca.add_argument('--content', required=True, help='文章内容（必填）')
    p_ca.add_argument('--uid', type=int, default=1, help='作者用户 ID（默认 1）')
    p_ca.add_argument('--lid', type=int, default=1, help='标签 ID（默认 1）')
    p_ca.add_argument('--img', default=None, help='封面图 URL')
    p_ca.add_argument('--heat', type=int, default=0, help='热度（默认 0）')
    add_common_args(p_ca)

    # get-article
    p_ga = sub.add_parser('get-article', help='查询单篇文章详情（含评论）')
    p_ga.add_argument('--article-id', type=int, required=True, help='文章 ID（必填）')
    add_common_args(p_ga)

    # update-article
    p_ua = sub.add_parser('update-article', help='更新文章')
    p_ua.add_argument('--article-id', type=int, required=True, help='文章 ID（必填）')
    p_ua.add_argument('--title', default=None, help='文章标题')
    p_ua.add_argument('--content', default=None, help='文章内容')
    p_ua.add_argument('--lid', type=int, default=None, help='标签 ID')
    p_ua.add_argument('--img', default=None, help='封面图 URL')
    p_ua.add_argument('--heat', type=int, default=None, help='热度')
    add_common_args(p_ua)

    # delete-article (软删除)
    p_da = sub.add_parser('delete-article', help='删除文章（软删除，可恢复）')
    p_da.add_argument('--article-id', type=int, required=True, help='文章 ID（必填）')
    add_common_args(p_da)

    # hard-delete-article (硬删除，需二次确认)
    p_hda = sub.add_parser('hard-delete-article', help='硬删除文章（不可逆，需二次确认）')
    p_hda.add_argument('--article-id', type=int, required=True, help='文章 ID（必填）')
    p_hda.add_argument('--yes', action='store_true', help='跳过二次确认（自动化场景使用）')
    add_common_args(p_hda)

    # restore-article
    p_ra = sub.add_parser('restore-article', help='恢复软删除的文章')
    p_ra.add_argument('--article-id', type=int, required=True, help='文章 ID（必填）')
    add_common_args(p_ra)

    # top-articles
    p_ta = sub.add_parser('top-articles', help='获取热门文章 Top N')
    p_ta.add_argument('--limit', type=int, default=5, help='返回数量（默认 5）')
    add_common_args(p_ta)

    # list-labels
    p_ll = sub.add_parser('list-labels', help='获取所有标签')
    add_common_args(p_ll)

    # create-label
    p_cl = sub.add_parser('create-label', help='创建标签')
    p_cl.add_argument('--lname', required=True, help='标签名称（必填）')
    add_common_args(p_cl)

    # list-users
    p_lu = sub.add_parser('list-users', help='获取用户列表')
    add_common_args(p_lu)

    # create-user
    p_cu = sub.add_parser('create-user', help='创建用户')
    p_cu.add_argument('--uname', required=True, help='用户名（必填）')
    p_cu.add_argument('--phone', default=None, help='手机号')
    p_cu.add_argument('--pwd', default=None, help='密码')
    p_cu.add_argument('--email', default=None, help='邮箱')
    p_cu.add_argument('--img', default=None, help='头像（默认 img/moren.jpg）')
    add_common_args(p_cu)

    # create-comment
    p_cc = sub.add_parser('create-comment', help='发表评论')
    p_cc.add_argument('--uid', type=int, required=True, help='评论者用户 ID（必填）')
    p_cc.add_argument('--aid', type=int, required=True, help='文章 ID（必填）')
    p_cc.add_argument('--content', required=True, help='评论内容（必填）')
    add_common_args(p_cc)

    # list-comments
    p_lc = sub.add_parser('list-comments', help='获取文章的评论列表')
    p_lc.add_argument('--aid', type=int, required=True, help='文章 ID（必填）')
    add_common_args(p_lc)

    # delete-comment
    p_dc = sub.add_parser('delete-comment', help='删除评论（软删除）')
    p_dc.add_argument('--comment-id', type=int, required=True, help='评论 ID（必填）')
    add_common_args(p_dc)

    # list-messages
    p_lm = sub.add_parser('list-messages', help='获取留言列表（含回复）')
    add_common_args(p_lm)

    # create-message
    p_cm = sub.add_parser('create-message', help='发表留言')
    p_cm.add_argument('--uid', type=int, required=True, help='留言者用户 ID（必填）')
    p_cm.add_argument('--content', required=True, help='留言内容（必填）')
    add_common_args(p_cm)

    # reply-message
    p_rm = sub.add_parser('reply-message', help='回复留言')
    p_rm.add_argument('--uid', type=int, required=True, help='回复者用户 ID（必填）')
    p_rm.add_argument('--mid', type=int, required=True, help='被回复的留言 ID（必填）')
    p_rm.add_argument('--content', required=True, help='回复内容（必填）')
    add_common_args(p_rm)

    # delete-message
    p_dm = sub.add_parser('delete-message', help='删除留言（软删除）')
    p_dm.add_argument('--message-id', type=int, required=True, help='留言 ID（必填）')
    add_common_args(p_dm)

    # list-moods
    p_lmo = sub.add_parser('list-moods', help='获取说说列表')
    add_common_args(p_lmo)

    # create-mood
    p_cmo = sub.add_parser('create-mood', help='发布说说')
    p_cmo.add_argument('--content', required=True, help='说说内容（必填）')
    p_cmo.add_argument('--title', default=None, help='标题')
    p_cmo.add_argument('--src', default=None, help='来源/链接')
    add_common_args(p_cmo)

    # delete-mood
    p_dmo = sub.add_parser('delete-mood', help='删除说说')
    p_dmo.add_argument('--mood-id', type=int, required=True, help='说说 ID（必填）')
    add_common_args(p_dmo)

    # upload-single
    p_us = sub.add_parser('upload-single', help='上传单个文件')
    p_us.add_argument('--file', required=True, help='本地文件路径（必填）')
    add_common_args(p_us)

    # upload-batch
    p_ub = sub.add_parser('upload-batch', help='批量上传文件')
    p_ub.add_argument('--files', nargs='+', required=True, help='本地文件路径列表（必填，至少 1 个）')
    add_common_args(p_ub)

    # list-uploads
    p_lup = sub.add_parser('list-uploads', help='列出所有已上传文件')
    add_common_args(p_lup)

    # delete-upload
    p_dup = sub.add_parser('delete-upload', help='删除已上传文件')
    p_dup.add_argument('--filename', required=True, help='文件名（必填）')
    add_common_args(p_dup)

    # capability-list
    p_clist = sub.add_parser('capability-list', help='列出本 skill 所有能力项')
    add_common_args(p_clist)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(2)

    dispatch = {
        'health-check': cmd_health_check,
        'list-articles': cmd_list_articles,
        'create-article': cmd_create_article,
        'get-article': cmd_get_article,
        'update-article': cmd_update_article,
        'delete-article': cmd_delete_article,
        'hard-delete-article': cmd_hard_delete_article,
        'restore-article': cmd_restore_article,
        'top-articles': cmd_top_articles,
        'list-labels': cmd_list_labels,
        'create-label': cmd_create_label,
        'list-users': cmd_list_users,
        'create-user': cmd_create_user,
        'create-comment': cmd_create_comment,
        'list-comments': cmd_list_comments,
        'delete-comment': cmd_delete_comment,
        'list-messages': cmd_list_messages,
        'create-message': cmd_create_message,
        'reply-message': cmd_reply_message,
        'delete-message': cmd_delete_message,
        'list-moods': cmd_list_moods,
        'create-mood': cmd_create_mood,
        'delete-mood': cmd_delete_mood,
        'upload-single': cmd_upload_single,
        'upload-batch': cmd_upload_batch,
        'list-uploads': cmd_list_uploads,
        'delete-upload': cmd_delete_upload,
        'capability-list': cmd_capability_list,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        print(f"错误：未知命令 {args.command}", file=sys.stderr)
        sys.exit(2)

    try:
        payload = handler(args)
        if args.format == 'md':
            print(render_md(payload))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(4)


if __name__ == '__main__':
    main()
