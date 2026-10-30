"""Manage joined computers through the same center and token as other pf-node commands."""
import argparse
import http.client
import json
import urllib.error
from urllib.parse import quote

from target_client import TargetError


def main(args, target=None):
    parser = argparse.ArgumentParser(prog='pf-node nodes', description='执行节点的加入邀请、最近快照和备注。')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('list')
    show = commands.add_parser('show')
    show.add_argument('name')
    show.add_argument('--devices', action='store_true', help='明确请求该节点重新采样手机')
    invite = commands.add_parser('invite')
    invite.add_argument('--hours', type=float, default=24)
    invite.add_argument('--note', default='')
    invitations = commands.add_parser('invites')
    invitations.add_argument('action', choices=['revoke'], nargs='?')
    invitations.add_argument('id', nargs='?')
    update = commands.add_parser('update')
    update.add_argument('name')
    update.add_argument('--revision', type=int, required=True)
    update.add_argument('--rename', help='管理员修改电脑名称，保留原身份、连接和任务')
    update.add_argument('--note')
    update.add_argument('--tag', action='append')
    update.add_argument('--clear-tags', action='store_true')
    options = parser.parse_args(args)
    method, path, body = 'GET', '/nodes', None
    if options.command == 'show':
        path += '/' + quote(options.name, safe='') + ('?devices=1' if options.devices else '')
    elif options.command == 'invite':
        method, path, body = 'POST', '/node-invites', {'hours': options.hours, 'note': options.note}
    elif options.command == 'invites':
        path = '/node-invites'
        if options.action:
            if not options.id:
                parser.error('invites revoke requires an invitation ID')
            method, path = 'DELETE', path + '/' + quote(options.id, safe='')
    elif options.command == 'update':
        method, path = 'POST', '/nodes/' + quote(options.name, safe='')
        body = {'revision': options.revision}
        if options.rename is not None:
            body['name'] = options.rename
        if options.note is not None:
            body['note'] = options.note
        if options.clear_tags or options.tag is not None:
            body['tags'] = options.tag or []
    try:
        request = target.request(method, path, json.dumps(body).encode() if body is not None else None,
                                 {'Content-Type': 'application/json'})
        with target.opener.open(request, timeout=30) as response:
            value = json.load(response)
    except TargetError as error:
        value = {'error': {'code': error.code, 'message': str(error)}}
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        value = {'error': {'code': 'http_error', 'message': f'HTTP {status}; 请求未重发'}}
    except (OSError, ValueError, http.client.HTTPException):
        value = {'error': {'code': 'unavailable' if method == 'GET' else 'write_result_unknown',
                           'message': '未收到完整回执；请查询原记录，请求未重发'}}
    print(json.dumps(value, ensure_ascii=False, indent=1))
    return 2 if 'error' in value else 0
