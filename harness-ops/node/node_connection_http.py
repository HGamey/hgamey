"""Machine enrollment and outbound transport endpoints, separate from agent auth."""
from contextlib import closing
import json
import sqlite3
from urllib.parse import urlparse, parse_qs

from node_directory import DirectoryError, MAX_RESPONSE
from job_inputs import InputError, MAX_FILE, _source_file
from artifact_io import SafeRoot, describe_file


def handle(handler, method, parts, directory, board, upload_limit, center):
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
        if len(route) == 4 and route[:2] == ['node-connect', 'assets'] and method == 'GET':
            record = center.targets.get(route[2])
            if record is None or record['execution_node_id'] != node_id:
                raise DirectoryError(404, 'asset destination is not assigned to this execution node')
            frozen = record['request'].get('deployment') or {}
            asset = next((asset for asset in frozen.get('manifest', {}).get('assets', [])
                          if asset['sha256'] == route[3]), None)
            if asset is None:
                raise DirectoryError(404, 'asset is not declared by this job')
            with closing(SafeRoot(center.local.games.cache.root)) as store, store.open_file('blobs/' + asset['sha256']) as stream:
                info = describe_file(stream, center.local._sha_cache, center.local._out_guard)
                handler._stream_out(stream, {**info, 'kind': 'file'}, parse_qs(urlparse(handler.path).query))
        elif len(route) == 4 and route[:2] == ['node-connect', 'inputs'] and method == 'GET':
            record = center.targets.get(route[2])
            if record is None or record['execution_node_id'] != node_id:
                raise DirectoryError(404, 'input destination is not assigned to this execution node')
            item = next((item for item in record['request']['spec']['inputs']['files']
                         if item['name'] == route[3]), None)
            if item is None:
                raise DirectoryError(404, 'input is not declared by this job')
            with _source_file(center.local, item['source']) as stream:
                data = stream.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise InputError(413, 'input_too_large', 'input exceeds 8 MiB')
            handler._send(200, data, 'application/octet-stream')
        elif route == ['node-connect', 'report'] and method == 'POST':
            snapshot = document(MAX_RESPONSE)
            observations = snapshot.pop('executions', [])
            events = snapshot.pop('execution_events', [])
            value = directory.report(node_id, snapshot)
            center.report(node_id, observations, events)
            handler._send(200, value)
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
    except InputError as error:
        handler._send(error.status, {'code': error.code, 'error': str(error)})
    except DirectoryError as error:
        handler._send(error.status, {'code': 'node_connection_error', 'error': str(error)})
    except FileNotFoundError as error:
        handler._send(404, {'code': 'source_not_found', 'error': str(error)})
    except sqlite3.Error as error:
        handler._send(503, {'code': 'storage_commit_unconfirmed', 'error': str(error)})
    except KeyError:
        handler._send(404, {'code': 'node_call_not_found', 'error': 'call is no longer waiting for a response'})
    except ValueError as error:
        handler._send(400, {'code': 'invalid_request', 'error': str(error)})
    except (BrokenPipeError, ConnectionResetError):
        pass  # The poll client disconnected; no operation is replayed.
    return True
