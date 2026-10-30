"""Machine enrollment and outbound transport endpoints, separate from agent auth."""
import json

from node_directory import DirectoryError, MAX_RESPONSE


def handle(handler, method, parts, directory, board, upload_limit):
    web = parts[:2] == ['dash', 'api']
    route = parts[2:] if web else parts
    if route[:1] not in (['node-invites'], ['node-connect']):
        return False
    try:
        def body(limit):
            length = int(handler.headers.get('Content-Length', '0'))
            if not 0 <= length <= limit:
                handler.close_connection = True
                raise DirectoryError(413, 'node connection request exceeds the HTTP size limit')
            return handler.rfile.read(length)

        def document(limit=64 << 10):
            value = json.loads(body(limit) or b'{}')
            if not isinstance(value, dict):
                raise DirectoryError(400, 'node connection request must be a JSON object')
            return value

        if route[0] == 'node-invites':
            if web:
                if board is None or not board.authed(handler):
                    handler._send(401, {'code': 'unauthorized', 'error': '需要查看密码'})
                    return True
                if not board.steered(handler) or method != 'GET' and handler.headers.get('X-HG') != '1':
                    handler._send(403, {'code': 'forbidden', 'error': '需要掌舵权限'})
                    return True
            else:
                if not handler._authed():
                    return True
                if handler.agent:
                    handler._send(403, {'code': 'forbidden', 'error': 'node invitations require administrator access'})
                    return True
            if method == 'GET' and len(route) == 1:
                handler._send(200, {'invites': directory.invites()})
            elif method == 'POST' and len(route) == 1:
                value = document()
                handler._send(201, directory.invite(value.get('hours', 24), value.get('note', '')))
            elif method == 'DELETE' and len(route) == 2:
                handler._send(200, directory.revoke_invite(route[1]))
            else:
                handler._send(404, {'error': 'no such node invitation operation'})
            return True

        if route == ['node-connect', 'join'] and method == 'POST':
            handler._send(201, directory.join(document()))
            return True
        token = handler.headers.get('Authorization', '').removeprefix('Bearer ')
        node_id = directory.verify(token)
        if node_id is None:
            handler._send(401, {'code': 'unauthorized', 'error': 'node connection token required'})
            return True
        if route == ['node-connect', 'report'] and method == 'POST':
            handler._send(200, directory.report(node_id, document(MAX_RESPONSE)))
        elif route == ['node-connect', 'poll'] and method == 'POST':
            body(64 << 10)
            directory.report(node_id)
            handler._send(200, {'commands': directory.link.poll(node_id)})
        elif len(route) == 4 and route[:2] == ['node-connect', 'calls']:
            call_id = route[2]
            if route[3] == 'body' and method == 'GET':
                handler._send(200, directory.link.body(node_id, call_id), 'application/octet-stream')
            elif route[3] == 'reply' and method == 'POST':
                response = json.loads(handler.headers['X-Node-Response'])
                directory.link.reply(node_id, call_id, response['status'], response['headers'], body(upload_limit))
                handler._send(200, {'received': True})
            else:
                handler._send(404, {'error': 'no such node call operation'})
        else:
            handler._send(404, {'error': 'no such node connection operation'})
    except DirectoryError as error:
        handler._send(error.status, {'code': 'node_connection_error', 'error': str(error)})
    except KeyError:
        handler._send(404, {'code': 'node_call_not_found', 'error': 'call is no longer waiting for a response'})
    except ValueError as error:
        handler._send(400, {'code': 'invalid_request', 'error': str(error)})
    except (BrokenPipeError, ConnectionResetError):
        pass  # The poll client disconnected; no operation is replayed.
    return True
